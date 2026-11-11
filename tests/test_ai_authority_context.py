"""Offline replay: contract attributes are not new approvals or approval exemptions."""
import copy,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import AuroraService,ModelAdapter,APIError,dumps,digest
RECORDED=json.loads((Path(__file__).parent/'fixtures/accepted_ai_brief_authority_20261111.json').read_text())
class AIAuthorityContextTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'AURORA_AI_ALLOW_NETWORK':'1','AURORA_AI_API_KEY':'OFFLINE-ONLY','AURORA_AI_MODEL':'fixture','AURORA_AI_ENDPOINT':'https://provider.test/model'},clear=True);env.start();self.addCleanup(env.stop)
        network=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No real API'));network.start();self.addCleanup(network.stop)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.service=AuroraService(db_path=Path(tmp.name)/'state.sqlite3');self.n=0
        self.op('submit');self.op('decision','committee',decision='approve',reason='오프라인 원본·권한 대조 시험')
        self.case=self.service.detail('CASE-001')
    def op(self,path,actor='quality',**payload):
        self.n+=1;payload['expected_version']=self.service.detail('CASE-001')['version']
        return self.service.mutate('/api/cases/CASE-001/'+path,payload,actor,str(self.n))
    def replay(self):return io.BytesIO(dumps({'choices':[{'message':{'content':dumps(RECORDED['structured'])},'finish_reason':'stop'}]}).encode())
    def test_original_contract_attributes_do_not_define_new_resupply_approval(self):
        b=ModelAdapter.reference_context(self.case)['decision_boundaries']
        m=b['contract_facts'];self.assertEqual((m['material_id'],m['supplier_id']),('M-B','V-205'))
        self.assertTrue(m['is_exclusive']);self.assertEqual(m['contract_type'],'2년 장기+MOQ+자동갱신·위약')
        self.assertEqual(m['source']['row_number'],3)
        self.assertEqual(b['existing_line_extra_resupply_approval']['status'],'not_specified_in_provided_sources')
        self.assertIsNone(b['existing_line_extra_resupply_approval']['authority'])
        self.assertIn('or exemption',b['existing_line_extra_resupply_approval']['meaning'])
        self.assertEqual(b['existing_line_checks']['kind'],'application_simulation_workflow')
    def test_f2_investment_owner_is_not_ordinary_resupply_or_recall_owner(self):
        snapshot=ModelAdapter.input_snapshot(self.case);b=snapshot['decision_context']
        self.assertEqual(b['f2_investment']['authority'],'CEO/이사회')
        self.assertEqual(b['f2_investment']['scope'],'investment_only_not_ordinary_resupply')
        self.assertEqual(b['f2_investment']['prerequisites'],['원료 재계약','처방 재검증'])
        self.assertIsNone(b['alternative_material_approval_authority'])
        self.assertIn('capex',snapshot['source_manifest']);self.assertIn('authority',snapshot['source_manifest'])
    def test_nonexclusive_material_does_not_inherit_exclusive_or_f2_claims(self):
        case=copy.deepcopy(self.case)
        case['analysis']['supply_constraints']['material']=copy.deepcopy(self.service.evidence.materials['M-A'])
        case['analysis']['alternatives']=[{'id':'existing_line'}]
        b=ModelAdapter.reference_context(case)['decision_boundaries']
        self.assertFalse(b['contract_facts']['is_exclusive'])
        self.assertFalse(b['f2_investment']['applicable']);self.assertIsNone(b['f2_investment']['authority'])
        self.assertEqual(b['f2_investment']['prerequisites'],[])
        self.assertIsNone(b['existing_line_extra_resupply_approval']['authority'])
    def test_c105_missing_link_is_not_a_confirmed_recall_boundary(self):
        scope=ModelAdapter.reference_context(self.case)['customer_scope']
        self.assertEqual(scope['claim_without_batch_shipment'],['C-105'])
        self.assertEqual(scope['batch_shipment_customers'],['C-101','C-102','C-103','C-104'])
        self.assertIn('not_a_confirmed_recall_boundary_conflict',scope['missing_link_meaning'])
        self.assertFalse(scope['shipment_is_current_inventory'])
    def test_real_bad_text_is_preserved_not_keyword_filtered_or_certified(self):
        with patch.object(self.service.ai.opener,'open',return_value=self.replay()):result=self.service.ai.generate(self.case)
        self.assertEqual(result['structured'],RECORDED['structured'])
        self.assertIn('재공급 승인 필요',result['structured']['risks'][2])
        self.assertFalse(result['narrative_screening']['semantic_verified'])
        self.assertEqual(result['approval_effect'],'none')
        self.assertEqual(result['reference_context']['decision_boundaries']['existing_line_extra_resupply_approval']['status'],'not_specified_in_provided_sources')
    def test_no_business_gate_added_and_exact_input_is_auditable(self):
        before=self.service.detail('CASE-001')
        with patch.object(self.service.ai.opener,'open',return_value=self.replay()) as calls:after=self.op('ai-brief')
        for key in ('status','conditions','tasks','decisions','recall_authorized','review_revision'):self.assertEqual(before[key],after[key])
        payload=json.loads(calls.call_args.args[0].data);transmitted=json.loads(payload['messages'][1]['content'])
        event=next(e for e in after['audit'] if e['action']=='ai_brief_created')
        self.assertEqual(transmitted,event['payload']['model_input'])
        self.assertEqual(digest(transmitted),after['ai']['brief']['input_fingerprint'])
        self.assertEqual(transmitted['input_contract_version'],5)
        for text in ('필요/불필요를 단정하지','CEO/이사회','회수 범위 불일치 확정으로 확대하지'):
            self.assertIn(text,payload['messages'][0]['content'])
        self.assertTrue(self.service.verify_audit()['valid'])
    def test_recorded_v4_result_is_stale_under_new_contract(self):
        self.assertEqual(RECORDED['input_contract_version'],4)
        with self.service.connect() as db:db.execute('UPDATE cases SET ai_brief=? WHERE id=?',(dumps(RECORDED),'CASE-001'))
        detail=self.service.detail('CASE-001')
        self.assertTrue(detail['ai']['brief']['stale'])
        self.assertEqual(detail['ai']['brief']['structured'],RECORDED['structured'])

if __name__=='__main__':unittest.main()
