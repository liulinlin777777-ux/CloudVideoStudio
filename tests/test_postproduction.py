import unittest
import tempfile
import subprocess
import json
from pathlib import Path
from unittest.mock import patch

from cloud_video_studio.media import (
    make_demo_project, compile_timeline, render_project, _audio_qa_graph, _parse_audio_qa,
)
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
        self.assertIn(r"enable='between(n\,0\,9)'", graph)
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
        self.assertIn('format=yuva420p,geq=r=0:g=0:b=0:a=0', graph)
        self.assertIn('setpts=PTS-STARTPTS', graph)
        self.assertIn('trim=start_frame=5:end_frame=25', graph)
        self.assertIn('setpts=PTS+0*1001/30000/TB', graph)
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
        self.assertIn('loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json,aresample=48000', compiled['filtergraph'])

    def test_pre_export_audio_qa_uses_complex_graph_and_strict_silence_json(self):
        qa_graph = _audio_qa_graph('color=s=2x2[vout];anullsrc[aout]')
        self.assertTrue(qa_graph.endswith('[aout];[aout]ebur128=peak=true,astats=metadata=0:reset=0,silencedetect=noise=-50dB:d=0.5[aqa]'))
        self.assertEqual(_parse_audio_qa('Peak level dB: -inf\nRMS level dB: -inf'), {
            'peak_level_db': None, 'rms_level_db': None,
        })
        self.assertEqual(json.dumps(_parse_audio_qa('Peak level dB: -inf\nRMS level dB: -inf'), allow_nan=False),
                         '{"peak_level_db": null, "rms_level_db": null}')
        self.assertEqual(_parse_audio_qa('Peak level dB: -3.25\nRMS level dB: -12.5'), {
            'peak_level_db': -3.25, 'rms_level_db': -12.5,
        })

    def test_title_frame_span_is_clip_local_at_nonzero_timeline_offset(self):
        project = make_demo_project()
        clip = project.sequences[0].tracks[0].clips[0]
        clip.start_frame = 45
        project.sequences[0].tracks[0].clips = [clip]
        clip.effects.append(Effect('title', {
            'text': 'Local span', 'x': 10, 'y': 10, 'font_size': 24,
            'color': '#ffffff', 'start_frame': 5, 'duration_frames': 20,
        }))
        graph = compile_timeline(project)['filtergraph']
        self.assertIn(r"enable='between(n\,5\,24)'", graph)

    def test_subtitle_frame_span_and_top_placement_are_clip_local(self):
        project = make_demo_project()
        project.assets.append(Asset('captions', 'assets/files/0123456789abcdef0123456789abcdef.srt', 150,
                                  {'version': 2, 'kind': 'subtitle'}))
        clip = project.sequences[0].tracks[0].clips[0]
        clip.start_frame = 45
        project.sequences[0].tracks[0].clips = [clip]
        clip.effects.append(Effect('subtitle', {
            'asset_id': 'captions', 'start_frame': 5, 'duration_frames': 20,
            'position': 'top', 'font_size': 24, 'color': '#ffffff',
        }))
        graph = compile_timeline(project)['filtergraph']
        self.assertIn('Alignment=6,MarginV=24', graph)
        self.assertIn('trim=start_frame=5:end_frame=25', graph)
        self.assertIn('setpts=PTS+45*1001/30000/TB', graph)

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


class FontCoverageRegression(unittest.TestCase):
    def test_latin_alias_needs_no_fontconfig_probe(self):
        from cloud_video_studio.media import _title_font_family
        from unittest.mock import patch
        with patch("cloud_video_studio.media.subprocess.run") as run:
            self.assertEqual(_title_font_family("Serif", "Literal title [] %"), "Serif")
            run.assert_not_called()
    def test_missing_cjk_glyphs_are_rejected(self):
        from cloud_video_studio.media import _title_font_family
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch("cloud_video_studio.media.subprocess.run", return_value=SimpleNamespace(stdout="DejaVu Sans\n20-7e")):
            with self.assertRaisesRegex(ValueError,"lacks title glyphs"):
                _title_font_family("Sans", "中文")
    def test_installed_cjk_family_is_coverage_checked(self):
        from cloud_video_studio.media import _title_font_family
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch("cloud_video_studio.media.subprocess.run", return_value=SimpleNamespace(stdout="Noto Sans CJK SC\n20-7e 4e00-9fff")):
            self.assertEqual(_title_font_family("Sans", "中文 title"),"Noto Sans CJK SC")


class PreviewRangeRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile,runpy,sys
        from unittest.mock import patch
        cls.temporary=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temporary.name)
        with patch.object(sys,"argv",["preview_server.py","--media-root",str(cls.root),"--port","18776"]),patch("http.server.ThreadingHTTPServer"):
            module=runpy.run_path("tools/preview_server.py",run_name="range_contract")
        cls.handler=module["Handler"]
        cls.parser=staticmethod(module["parse_byte_range"])
        (cls.root/"render.mp4").write_bytes(b"0123456789")
    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()
    def request(self,value):
        from email.message import Message
        h=object.__new__(self.handler);h.headers=Message()
        if value is not None:h.headers["Range"]=value
        calls=[]
        h.reply=lambda code,body,kind="application/json",headers=None:calls.append((code,body,headers))
        h.media("render.mp4")
        return calls[0]
    def test_normal_open_closed_and_suffix(self):
        self.assertEqual(self.request(None),(200,b"0123456789",{"Accept-Ranges":"bytes"}))
        self.assertEqual(self.request("bytes=2-4"),(206,b"234",{"Accept-Ranges":"bytes","Content-Range":"bytes 2-4/10"}))
        self.assertEqual(self.request("bytes=8-")[1],b"89")
        self.assertEqual(self.request("bytes=-3")[1],b"789")
        self.assertEqual(self.request("bytes=0-99")[1],b"0123456789")
    def test_malformed_and_unsatisfiable_fail_closed(self):
        for value in ["bytes=10-","bytes=4-2","bytes=-0","bytes=0-1,4-5","bytes=-","items=1-2","bytes="+"9"*200+"-"]:
            code,body,headers=self.request(value)
            with self.subTest(value=value):
                self.assertEqual(code,416);self.assertEqual(body,b"");self.assertEqual(headers["Content-Range"],"bytes */10")


class EncodedAudioQaRegression(unittest.TestCase):
    def test_encoded_clipping_is_rejected_after_aac_decode(self):
        from cloud_video_studio.media import _encoded_audio_qa
        from types import SimpleNamespace
        from unittest.mock import patch
        log="Peak level dB: 2.0\nRMS level dB: -8.0\nI: -10.0 LUFS\nPeak: 2.0 dBFS\nNumber of samples: 192000"
        with patch("cloud_video_studio.media.subprocess.run",return_value=SimpleNamespace(returncode=0,stderr=log)):
            with self.assertRaisesRegex(ValueError,"ENCODED_AUDIO_CLIPPING_DETECTED"):
                _encoded_audio_qa(Path("owned.mp4"),ffmpeg="ffmpeg",timeout=5)
    def test_encoded_measurements_are_real_complete_and_json_safe(self):
        from cloud_video_studio.media import _encoded_audio_qa
        from types import SimpleNamespace
        from unittest.mock import patch
        log="Peak level dB: -inf\nRMS level dB: -inf\nI: -70.0 LUFS\nPeak: -inf dBFS\nNumber of samples: 192000"
        with patch("cloud_video_studio.media.subprocess.run",return_value=SimpleNamespace(returncode=0,stderr=log)):
            result=_encoded_audio_qa(Path("owned.mp4"),ffmpeg="ffmpeg",timeout=5)
            self.assertIsNone(result["peak_level_db"]);json.dumps(result,allow_nan=False)
        with patch("cloud_video_studio.media.subprocess.run",return_value=SimpleNamespace(returncode=0,stderr="")):
            with self.assertRaisesRegex(RuntimeError,"incomplete"):
                _encoded_audio_qa(Path("owned.mp4"),ffmpeg="ffmpeg",timeout=5)
