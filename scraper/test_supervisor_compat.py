"""Offline supervisor compatibility: never main, phones, cron or external storage."""
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from automation import wechat_dual_scrape_supervisor as s
class SupervisorCommandTests(unittest.TestCase):
 def test_generated_worker_command_is_nondestructive_and_accepted_by_scraper(self):
  import test_package_metadata as m
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();device='fixture';out=root/('device_'+device);out.mkdir()
   s.write_csv(root/('input_'+device+'.csv'),['appid','name'],[dict(appid='wx0123456789abcdef',name='Fixture')])
   calls=[]
   def popen(cmd,**kwargs):calls.append(cmd);return SimpleNamespace(pid=123)
   with patch.object(s,'DEVICES',[device]),patch.object(s.subprocess,'Popen',side_effect=popen):s.start_workers(root)
   self.assertEqual(len(calls),1)
   self.assertNotIn('--clear-cache-before-open',calls[0])
   output=Path(calls[0][calls[0].index('--output')+1])
   m.scraper.validate_retention_policy(output/'manifest.csv',False)
 def test_same_timestamp_prepare_run_allocates_unique_new_outputs(self):
  from datetime import datetime
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();active=root/'active';out=root/'out';out.mkdir()
   status=out/'status.csv';s.write_csv(status,['appid','name','is_scraped'],[dict(appid='wx0123456789abcdef',name='Fixture',is_scraped='0')])
   with patch.multiple(s,ACTIVE_ROOT=active,CURRENT_MARKER=active/'CURRENT',STATUS_CSV=status,DEVICES=['fixture']),patch.object(s,'datetime') as clock:
    clock.now.return_value=datetime(2026,11,1,12,0,0)
    first=s.prepare_run();(first/'device_fixture/manifest.csv').write_bytes(b'history\n')
    second=s.prepare_run()
    self.assertNotEqual(first,second)
    self.assertEqual((first/'device_fixture/manifest.csv').read_bytes(),b'history\n')
    self.assertEqual(list((second/'device_fixture').iterdir()),[])
 def test_reused_or_symlink_output_rejected_before_any_worker_starts(self):
  for existing in ['manifest.csv','package_metadata.csv','package_metadata_evidence/a.json','run.log','symlink']:
   with self.subTest(existing=existing),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp).resolve()
    for serial in ['clean','dirty']:
     (root/('device_'+serial)).mkdir();s.write_csv(root/('input_'+serial+'.csv'),['appid'],[{'appid':'wx0123456789abcdef'}])
    out=root/'device_dirty'
    if existing=='symlink':out.rmdir();out.symlink_to(root/'device_clean',target_is_directory=True)
    else:p=out/existing;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'history')
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with patch.object(s,'DEVICES',['clean','dirty']),patch.object(s.subprocess,'Popen') as popen,patch.object(s,'terminate_workers'):
     with self.assertRaisesRegex(ValueError,'fresh_worker_output_required'):s.start_workers(root)
     popen.assert_not_called()
    self.assertEqual({str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()},before)
class SupervisorHistoryTests(unittest.TestCase):
 def test_cleanup_preserves_run_history_sidecars_markers_and_canonical_files(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve();active=root/'active';run=active/'run';out=root/'out';run.mkdir(parents=True);out.mkdir()
   marker=active/'CURRENT';marker.write_text(str(run))
   for name in ['manifest.csv','package_history.csv','package_metadata.csv','package_metadata_evidence/sha/summary.json','blobs/a.wxapkg','run.log','pid']:
    p=run/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('historical '+name)
   (out/'audit.json').write_text('audit');(out/'merge.log').write_text('merge')
   before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   with patch.multiple(s,ACTIVE_ROOT=active,CURRENT_MARKER=marker,OUT=out):s.clean_junk(run)
   self.assertEqual({str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()},before)
 def test_dedupe_reports_but_retains_duplicate_bytes_and_empty_directories(self):
  with tempfile.TemporaryDirectory() as tmp:
   out=Path(tmp).resolve()
   for name in ['wx1/a.wxapkg','wx2/b.wxapkg']:
    p=out/name;p.parent.mkdir();p.write_bytes(b'same-package')
   (out/'wxempty').mkdir()
   before={str(p):p.read_bytes() for p in out.rglob('*') if p.is_file()}
   with patch.object(s,'OUT',out):stats=s.dedupe_canonical()
   self.assertEqual({str(p):p.read_bytes() for p in out.rglob('*') if p.is_file()},before)
   self.assertTrue((out/'wxempty').is_dir())
   self.assertEqual(stats['removed_duplicate_files'],0)
   self.assertEqual(stats['preserved_duplicate_files'],1)
if __name__=='__main__':unittest.main()
