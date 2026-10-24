import unittest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

from cloud_video_studio.media import make_demo_project, compile_timeline, render_project
from cloud_video_studio.plugins import run_plugin, PluginError, PLUGIN
from cloud_video_studio.project import Asset, Effect


class MediaTests(unittest.TestCase):
    def test_real_topology_and_rational_graph(self):
        project = make_demo_project()
        project.validate()
        sequence = project.sequences[0]
        self.assertEqual([track.kind for track in sequence.tracks], ['video', 'video', 'audio', 'audio'])
        compiled = compile_timeline(project)
        self.assertEqual(compiled['duration_frames'], 120)
        self.assertEqual(compiled['fps'], '30000/1001')
        self.assertEqual(compiled['inputs'], ['red', 'blue', 'green', 'tone440', 'tone880'])
        for fragment in ['trim=', 'xfade=', 'overlay=', 'atrim=', 'adelay=', 'amix=']:
            self.assertIn(fragment, compiled['filtergraph'])

    def test_trim_is_compiled_from_model(self):
        project = make_demo_project()
        project.sequences[0].tracks[0].clips[0].source_in_frame = 20
        self.assertIn('start_frame=20', compile_timeline(project)['filtergraph'])

    def test_unknown_effect_not_interpreted_as_filter(self):
        project = make_demo_project()
        project.sequences[0].tracks[0].clips[0].effects.append(
            Effect('movie', {'url': 'https://example.com'})
        )
        with self.assertRaises(ValueError):
            compile_timeline(project)

    def test_nonfinite_volume_rejected(self):
        project = make_demo_project()
        project.sequences[0].tracks[2].clips[0].effects[0].parameters['gain'] = float('nan')
        with self.assertRaises(ValueError):
            compile_timeline(project)

    def test_render_refuses_source_overwrite_and_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = make_demo_project()
            for asset in project.assets:
                (root / asset.uri).write_bytes(b'sentinel')
            with patch('cloud_video_studio.media.subprocess.run') as runner:
                for output in [root / project.assets[0].uri, root / '..' / 'escape.mp4']:
                    with self.assertRaises(ValueError):
                        render_project(project, root, output)
                project.assets[0].uri = '../escape.mp4'
                with self.assertRaises(ValueError):
                    render_project(project, root, root / 'out.mp4')
                runner.assert_not_called()

    def test_plugin_manifest_and_unknown_plugin(self):
        self.assertEqual(PLUGIN['id'], 'builtin.proxy')
        self.assertEqual(PLUGIN['version'], '1.0.0')
        self.assertIn('video.proxy', PLUGIN['capabilities'])
        with self.assertRaises(PluginError):
            run_plugin('remote.untrusted', Path('a'), Path('b'))

    def test_plugin_timeout_is_classified(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'in.mp4'
            source.write_bytes(b'sentinel')
            with patch('cloud_video_studio.plugins.subprocess.run',
                       side_effect=subprocess.TimeoutExpired('ffmpeg', 0.01)):
                with self.assertRaisesRegex(PluginError, 'TIMEOUT'):
                    run_plugin('builtin.proxy', source, Path(directory) / 'out.mp4', timeout=0.01)
            self.assertEqual(source.read_bytes(), b'sentinel')

    def test_plugin_rejects_input_output_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'in.mp4'
            source.write_bytes(b'sentinel')
            with self.assertRaises(PluginError):
                run_plugin('builtin.proxy', source, source)


class IndependentRegressionTests(unittest.TestCase):
    def test_frame_edit_and_sample_delay(self):
        for frame in [0, 1, 36, 60]:
            project = make_demo_project()
            project.sequences[0].tracks[1].clips[0].start_frame = frame
            project.sequences[0].tracks[3].clips[0].start_frame = frame
            graph = compile_timeline(project)['filtergraph']
            self.assertIn('asetpts=N/SR/TB', graph)
            self.assertIn('S', graph)

    def test_volume_resource_bounds(self):
        project = make_demo_project()
        project.sequences[0].tracks[2].clips[0].effects[0].parameters['gain'] = 1.5
        with self.assertRaises(ValueError):
            compile_timeline(project)
        for timeout, threads in [(float('nan'), 2), (float('inf'), 2), (31, 2), (30, 3)]:
            with self.assertRaises(PluginError):
                run_plugin('builtin.proxy', Path('a'), Path('b'), timeout=timeout, threads=threads)

    def test_color_title_and_audio_treatments_compile(self):
        project = make_demo_project()
        video = project.sequences[0].tracks[0].clips[0]
        video.effects.extend([
            Effect('color', {'brightness': 0.1, 'contrast': 1.2, 'saturation': 0.8}),
            Effect('title', {'text': "Hi: 50%", 'x': 10, 'y': 12, 'font_size': 24,
                             'color': '#ffffff', 'start_frame': 2, 'duration_frames': 20}),
        ])
        audio = project.sequences[0].tracks[2].clips[0]
        audio.effects.extend([
            Effect('mute', {'enabled': True}),
            Effect('fade', {'in_frames': 10, 'out_frames': 10}),
        ])
        graph = compile_timeline(project)['filtergraph']
        self.assertIn('eq=brightness=0.1:contrast=1.2:saturation=0.8', graph)
        self.assertIn('drawtext=', graph)
        self.assertIn('volume=0', graph)
        self.assertIn('afade=t=in', graph)
        self.assertIn('afade=t=out', graph)
        self.assertIn('alimiter=limit=0.95', graph)

    def test_srt_effect_uses_registered_project_asset_and_rejects_traversal(self):
        project = make_demo_project()
        project.assets.append(Asset('captions', 'assets/files/0123456789abcdef0123456789abcdef.srt', 150,
                                  {'version': 2, 'kind': 'subtitle'}))
        video = project.sequences[0].tracks[0].clips[0]
        video.effects.append(Effect('subtitle', {
            'asset_id': 'captions', 'start_frame': 0, 'duration_frames': 40,
            'position': 'bottom', 'font_size': 24, 'color': '#ffffff',
        }))
        compiled = compile_timeline(project)
        self.assertIn('subtitles=filename=', compiled['filtergraph'])
        self.assertIn('captions', compiled['inputs'])
        project.assets[-1].uri = '../captions.srt'
        with self.assertRaises(ValueError):
            compile_timeline(project)

    def test_title_text_and_filter_ranges_are_bounded(self):
        project = make_demo_project()
        project.sequences[0].tracks[0].clips[0].effects.append(Effect('title', {
            'text': "x';movie=bad", 'x': 0, 'y': 0, 'font_size': 24,
            'color': '#ffffff', 'start_frame': 0, 'duration_frames': 10,
        }))
        compiled = compile_timeline(project)
        graph = compiled['filtergraph']
        self.assertNotIn("movie=bad", graph)
        self.assertIn("x';movie=bad", compiled['title_texts'].values())
        self.assertIn("textfile='__CVS_TITLE_", graph)
        self.assertIn('expansion=none', graph)
        self.assertIn("font='Sans'", graph)
        self.assertIn(r"enable='gte(t\,0)*lt(t\,0.333667)'", graph)
        project.sequences[0].tracks[0].clips[0].effects[0].parameters['font_size'] = 1000
        with self.assertRaises(ValueError):
            compile_timeline(project)

    def test_subtitle_uses_libass_timing_and_a_timed_overlay(self):
        project = make_demo_project()
        project.assets.append(Asset('captions', 'assets/files/0123456789abcdef0123456789abcdef.srt', 150,
                                  {'version': 2, 'kind': 'subtitle'}))
        project.sequences[0].tracks[0].clips[0].effects.append(Effect('subtitle', {
            'asset_id': 'captions', 'start_frame': 5, 'duration_frames': 20,
            'position': 'bottom', 'font_size': 24, 'color': '#ffffff',
        }))
        compiled = compile_timeline(project)
        graph = compiled['filtergraph']
        self.assertIn('subtitles=filename=', graph)
        self.assertNotIn("MarginV=24':enable=", graph)
        self.assertIn('color=c=black@0', graph)
        self.assertIn('format=rgba,geq=r=0:g=0:b=0:a=0', graph)
        self.assertIn('setpts=PTS-STARTPTS', graph)
        self.assertIn(r'gte(t\,0.166833)*lt(t\,0.834167)', graph)
        self.assertIn("overlay=eof_action=pass:shortest=0:format=auto:enable=", graph)

    def test_title_font_uses_safe_alias(self):
        project = make_demo_project()
        project.sequences[0].tracks[0].clips[0].effects.append(Effect('title', {
            'text': 'Safe title', 'font_family': 'Monospace', 'x': 0, 'y': 0,
            'font_size': 24, 'color': '#ffffff', 'start_frame': 0, 'duration_frames': 10,
        }))
        self.assertIn("font='Monospace'", compile_timeline(project)['filtergraph'])
        project.sequences[0].tracks[0].clips[0].effects[0].parameters['font_family'] = '../../etc/passwd'
        with self.assertRaises(ValueError):
            compile_timeline(project)

    def test_crop_and_pip_compose_and_loudness_is_measured_when_requested(self):
        project = make_demo_project()
        pip_clip = project.sequences[0].tracks[1].clips[0]
        pip_clip.effects.append(Effect('crop', {'x': 0, 'y': 0, 'width': 320, 'height': 180}))
        project.sequences[0].tracks[2].effects.append(Effect('normalize', {'enabled': True}))
        compiled = compile_timeline(project)
        self.assertIn('crop=320:180:0:0,scale=640:360', compiled['filtergraph'])
        self.assertIn('scale=160:90', compiled['filtergraph'])
        self.assertIn('loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', compiled['filtergraph'])
        self.assertTrue(compiled['audio_normalization'])

    def test_audio_track_treatment_is_versioned_and_applies_to_all_clips(self):
        project = make_demo_project()
        track = project.sequences[0].tracks[2]
        track.effects.extend([
            Effect('volume', {'gain': 0.5}),
            Effect('mute', {'enabled': False}),
            Effect('fade', {'in_frames': 5, 'out_frames': 5}),
        ])
        project.upgrade_asset_metadata()
        restored = type(project).from_dict(project.to_dict())
        self.assertEqual(restored.sequences[0].tracks[2].effects[0].kind, 'volume')
        graph = compile_timeline(restored)['filtergraph']
        self.assertIn('volume=0.2', graph)
        self.assertIn('afade=t=in', graph)
        self.assertIn('afade=t=out', graph)


if __name__ == '__main__':
    unittest.main()
