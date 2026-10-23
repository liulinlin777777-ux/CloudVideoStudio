import hashlib,tempfile,unittest
from pathlib import Path
from cloud_video_studio.assets import AssetStore

class FrozenAssetContract(unittest.TestCase):
 def test_srt_import_preserves_source_and_metadata_reload(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'captions.srt';src.write_text('1\n00:00:00,000 --> 00:00:01,000\nSafe caption\n',encoding='utf-8');before=src.read_bytes()
   store=AssetStore(root/'store');asset=store.import_file(src)
   self.assertEqual(asset['sha256'],hashlib.sha256(before).hexdigest());self.assertEqual(asset['kind'],'subtitle');self.assertEqual(src.read_bytes(),before)
   self.assertEqual(AssetStore(root/'store').list_assets(),store.list_assets())
 def test_unsupported_and_size_limit(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);bad=p/'untrusted.bin';bad.write_bytes(b'x'*40)
   with self.assertRaises(ValueError):AssetStore(p/'s').import_file(bad)
   s=p/'large.srt';s.write_bytes(b'x'*40)
   with self.assertRaises(ValueError):AssetStore(p/'s2',max_file_bytes=16).import_file(s)
 def test_symlinks_and_hardlinks_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);s=p/'caption.srt';s.write_text('1\n00:00:00,000 --> 00:00:01,000\ntext\n');link=p/'alias.srt';link.symlink_to(s)
   with self.assertRaises(ValueError):AssetStore(p/'s').import_file(link)
   link.unlink();link.hardlink_to(s)
   with self.assertRaises(ValueError):AssetStore(p/'s2').import_file(link)
