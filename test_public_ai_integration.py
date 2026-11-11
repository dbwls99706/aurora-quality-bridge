"""Public integration fail-closed tests. No provider or database calls."""
import os,json,unittest
from unittest.mock import patch
import public_demo_cloud as cloud
class IntegrationTests(unittest.TestCase):
 def test_off_mode_rejects_public_key_without_reading_value(self):
  env={'AURORA_ISOLATED_APP_ID':cloud.profile.APP_ID,'AURORA_AI_ALLOW_NETWORK':'0','AURORA_PUBLIC_AI_KEY':'NEVER_DISCLOSE'}
  with patch.dict(os.environ,env,clear=True),patch.object(cloud.profile,'PUBLIC_AI_ENABLED',False):
   r=cloud.Runtime();r.initialize()
  self.assertEqual(r.result['phase'],'provider_presence');self.assertIsNone(r.registry);self.assertNotIn('NEVER_DISCLOSE',json.dumps(r.result))
 def test_enabled_profile_requires_explicit_network_one(self):
  env={'AURORA_ISOLATED_APP_ID':cloud.profile.APP_ID,'AURORA_AI_ALLOW_NETWORK':'0'}
  with patch.dict(os.environ,env,clear=True),patch.object(cloud.profile,'PUBLIC_AI_ENABLED',True):
   r=cloud.Runtime();r.initialize()
  self.assertEqual(r.result['phase'],'profile');self.assertIsNone(r.registry)
 def test_legacy_keys_still_rejected_in_enabled_mode(self):
  for key in ('ANTHROPIC_API_KEY','OPENAI_API_KEY','AURORA_AI_API_KEY','LLM_API_KEY'):
   env={'AURORA_ISOLATED_APP_ID':cloud.profile.APP_ID,'AURORA_AI_ALLOW_NETWORK':'1',key:'NEVER_DISCLOSE'}
   with patch.dict(os.environ,env,clear=True),patch.object(cloud.profile,'PUBLIC_AI_ENABLED',True):
    r=cloud.Runtime();r.initialize()
   self.assertEqual(r.result['phase'],'provider_presence');self.assertIsNone(r.registry);self.assertNotIn('NEVER_DISCLOSE',json.dumps(r.result))
if __name__=='__main__':unittest.main()
