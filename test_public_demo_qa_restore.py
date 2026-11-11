"""Mocked persistence recovery only; no actual DB, wake, or QA execution."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import public_demo_cloud_selftest as qa

class RestoreTests(unittest.TestCase):
    def context(self,exists,row=None):
        db=Mock();db.execute.return_value.fetchone.side_effect=[(exists,),row] if exists else [(None,)]
        ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
        return db,ctx
    def test_exact_version_passed_restored_without_running(self):
        result={'status':'passed','qa_version':'v-current','checks':[],'real_judge_login_tested':False}
        db,ctx=self.context(True,('passed',json.dumps(result)))
        reg=SimpleNamespace(config='fake',connector=None,resolve=Mock())
        with patch.object(qa,'registry_connection',return_value=ctx),patch.object(qa,'qa_version',return_value='v-current'):
            self.assertEqual(qa.load_result(reg),result)
        reg.resolve.assert_not_called()
        self.assertTrue(all(c.args[0].startswith('SELECT') for c in db.execute.call_args_list))
        self.assertIn(('v-current',),[c.args[1] for c in db.execute.call_args_list])
    def test_missing_version_table_does_not_upgrade_legacy(self):
        db,ctx=self.context(False);reg=SimpleNamespace(config='fake',connector=None)
        with patch.object(qa,'registry_connection',return_value=ctx),patch.object(qa,'qa_version',return_value='v-current'):
            self.assertEqual(qa.load_result(reg)['status'],'awaiting_owner_qa')
        self.assertEqual(db.execute.call_count,1)
    def test_missing_current_version_needs_owner(self):
        db,ctx=self.context(True,None)
        with patch.object(qa,'registry_connection',return_value=ctx),patch.object(qa,'qa_version',return_value='v-current'):
            self.assertEqual(qa.load_result(SimpleNamespace(config='fake',connector=None))['status'],'awaiting_owner_qa')
    def test_running_failed_and_mismatch_fail_closed(self):
        self.assertEqual(qa.decode_result(('running',None),'v')['status'],'running')
        self.assertEqual(qa.decode_result(('failed',json.dumps({'status':'failed','qa_version':'v'})),'v')['status'],'failed')
        self.assertEqual(qa.decode_result(('passed',json.dumps({'status':'passed','qa_version':'old'})),'v')['status'],'failed')
        self.assertEqual(qa.decode_result(('passed',None),'v')['status'],'failed')
    def test_fingerprint_is_content_derived_and_not_profile_toggle(self):
        version=qa.qa_version();self.assertTrue(version.startswith(qa.QA_PROTOCOL+':'))
        self.assertEqual(len(version.split(':')[1]),64)
        self.assertNotIn('public_demo_cloud_profile.py',qa.QA_FILES)
        with patch.object(qa.Path,'read_bytes',return_value=b'changed-source'):
            self.assertNotEqual(qa.qa_version(),version)
