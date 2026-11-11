"""Offline replay and typed planning boundary regressions; never call a provider."""
import copy, io, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import AuroraService,ModelAdapter,APIError,dumps,add_business_days
RECORDED=json.loads((Path(__file__).parent/'fixtures/accepted_ai_brief_quantity_20261111.json').read_text())

class AIPlanningContextTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'AURORA_AI_ALLOW_NETWORK':'1','AURORA_AI_API_KEY':'OFFLINE-ONLY','AURORA_AI_MODEL':'fixture','AURORA_AI_ENDPOINT':'https://provider.test/model'},clear=True)
        env.start();self.addCleanup(env.stop)
        network=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Real API forbidden'))
        network.start();self.addCleanup(network.stop)
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.service=AuroraService(db_path=Path(tmp.name)/'state.sqlite3');self.n=0
        self.op('submit');self.op('decision','committee',decision='approve',reason='오프라인 의결·병행 원인 조사 시험')
        self.case=self.service.detail('CASE-001')
    def op(self,path,actor='quality',**data):
        self.n+=1;data['expected_version']=self.service.detail('CASE-001')['version']
        return self.service.mutate('/api/cases/CASE-001/'+path,data,actor,str(self.n))
    def condition(self,**changes):
        return self.op('conditions',changes=changes,reason='명시적 모의 비교 입력, 실제 확약 아님')
    def replay(self):
        return io.BytesIO(dumps({'choices':[{'message':{'content':dumps(RECORDED['structured'])},'finish_reason':'stop'}]}).encode())
    def test_unknown_replacement_keeps_duration_and_ship_date_unknown(self):
        c=ModelAdapter.reference_context(self.case);p=c['planning']
        self.assertEqual(p['confirmed_defect_qty'],17900)
        for key in ('replacement_qty','production_days','total_business_days','earliest_ship_date'):self.assertIsNone(p[key],key)
        self.assertEqual(p['quantity_status'],'not_entered')
        self.assertEqual(p['selected_daily_capacity'],200)
        self.assertEqual(p['assumed_daily_ceiling'],224)
        self.assertTrue(p['ceiling_must_not_override_selected_rate'])
        self.assertFalse(p['inventory_verified']);self.assertFalse(p['production_confirmed'])
    def test_explicit_17900_at_selected_200_has_90_production_and_95_total_business_days(self):
        detail=self.condition(replacement_qty=17900)
        p=ModelAdapter.reference_context(detail)['planning']
        self.assertEqual((p['production_days'],p['total_business_days']),(90,95))
        self.assertEqual(p['earliest_ship_date'],add_business_days('2026-07-01',95))
        self.assertEqual(p['quantity_status'],'simulation_input')
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
    def test_explicit_rate_change_to_ceiling_is_not_automatic(self):
        baseline=self.condition(replacement_qty=17900)
        changed=self.condition(production_daily_capacity=224)
        p=ModelAdapter.reference_context(changed)['planning']
        self.assertEqual((p['production_days'],p['total_business_days']),(80,85))
        self.assertEqual(baseline['conditions']['production_daily_capacity'],200)
        self.assertEqual(p['selected_daily_capacity'],224)
        self.assertTrue(p['ceiling_must_not_override_selected_rate'])
        self.assertFalse(changed['analysis']['recovery_plan']['feasible'])
    def test_other_quantity_is_not_replaced_with_defect_count_and_delays_are_included(self):
        detail=self.condition(replacement_qty=1000,production_delay_days=4)
        p=ModelAdapter.reference_context(detail)['planning']
        self.assertEqual(p['confirmed_defect_qty'],17900);self.assertEqual(p['replacement_qty'],1000)
        self.assertEqual((p['production_days'],p['total_business_days']),(5,14))
    def test_line_scope_not_factory_scope_is_explicit(self):
        c=ModelAdapter.reference_context(self.case)['capacity']
        self.assertEqual((c['plant_id'],c['line_id'],c['product_id']),('F1','L-02','PL-02'))
        self.assertEqual(c['utilization_scope'],'line_not_factory')
        self.assertEqual(c['utilization_pct'],97);self.assertIsNone(c['factory_utilization_pct'])
        self.assertIn('dictionary',ModelAdapter.input_snapshot(self.case)['source_manifest'])
    def test_root_cause_stays_open_after_approval_and_changes_stale_input(self):
        current=ModelAdapter.reference_context(self.case)['workflow']['root_cause']
        self.assertEqual(current['status'],'open');self.assertEqual(current['department'],'quality')
        before=self.service._ai_input_fingerprint(self.case)
        changed=copy.deepcopy(self.case)
        next(t for t in changed['tasks'] if t['type']=='root_cause')['status']='done'
        self.assertNotEqual(before,self.service._ai_input_fingerprint(changed))
        self.assertEqual(ModelAdapter.reference_context(changed)['workflow']['root_cause']['status'],'done')
        changed['tasks']=[];self.assertIsNone(ModelAdapter.reference_context(changed)['workflow']['root_cause'])
    def test_actual_bad_response_stays_untrusted_while_comparison_values_remain_correct(self):
        before=copy.deepcopy(self.case)
        with patch.object(self.service.ai.opener,'open',return_value=self.replay()) as calls:
            result=self.service.ai.generate(self.case)
        self.assertIn('79~80일',result['structured']['risks'][1])
        self.assertIn('F1 공장 가동률 97%',result['structured']['risks'][1])
        self.assertFalse(result['narrative_screening']['semantic_verified'])
        self.assertIsNone(result['reference_context']['planning']['replacement_qty'])
        self.assertIsNone(result['reference_context']['planning']['production_days'])
        self.assertEqual(result['reference_context']['planning']['selected_daily_capacity'],200)
        self.assertEqual(self.case,before)
        transmitted=json.loads(calls.call_args.args[0].data)
        prompt=transmitted['messages'][0]['content'];snapshot=json.loads(transmitted['messages'][1]['content'])
        self.assertEqual(snapshot['input_contract_version'],5)
        self.assertEqual(snapshot['planning_context'],result['reference_context']['planning'])
        for term in ('replacement_qty=null','생산일수·출하일 미산정','selected_daily_capacity','root_cause.status','공장 전체 가동률'):
            self.assertIn(term,prompt)
        self.assertNotIn('남은 공급 확인만',prompt)
    def test_prior_v3_snapshot_is_stale_but_review_only_version_is_not(self):
        self.assertEqual(RECORDED['input_contract_version'],3)
        with self.service.connect() as db:
            db.execute('UPDATE cases SET ai_brief=? WHERE id=?',(dumps(RECORDED),'CASE-001'))
        self.assertTrue(self.service.detail('CASE-001')['ai']['brief']['stale'])
        snapshot=ModelAdapter.input_snapshot(self.case)
        self.assertEqual(snapshot['input_contract_version'],5)
        before=self.service._ai_input_fingerprint(self.case)
        candidate=copy.deepcopy(self.case);candidate['version']+=1
        self.assertEqual(before,self.service._ai_input_fingerprint(candidate))

if __name__=='__main__':unittest.main()
