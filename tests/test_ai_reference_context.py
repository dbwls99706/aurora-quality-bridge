"""Offline replay of an actual accepted response. No new provider execution.

Acceptance validates transport/schema/five numerical facts/record ID existence,
not the truth of free text. The two layers must never acquire each other's trust.
"""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import APIError, AuroraService, ModelAdapter, digest, dumps

FIXTURE = json.loads((Path(__file__).parent/'fixtures/accepted_ai_brief_20261111.json').read_text())

class AIReferenceContextTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'AURORA_AI_ALLOW_NETWORK':'1','AURORA_AI_API_KEY':'OFFLINE-ONLY',
            'AURORA_AI_MODEL':'fixture','AURORA_AI_ENDPOINT':'https://provider.test/model'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('External calls forbidden'))
        network.start(); self.addCleanup(network.stop)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.service = AuroraService(db_path=Path(tmp.name)/'test.sqlite3')
        self.n = 0
        self.op('submit')
        self.op('decision', 'committee', decision='approve', reason='오프라인 검증용 모의 의결, 보유량 확인은 별도 후속')
        self.case = self.service.detail('CASE-001')

    def op(self, path, actor='quality', **data):
        self.n += 1
        data['expected_version'] = self.service.detail('CASE-001')['version']
        return self.service.mutate('/api/cases/CASE-001/'+path, data, actor, 'context-'+str(self.n))

    def response(self, structured=None):
        return io.BytesIO(dumps({'choices':[{'message':{'content':dumps(structured or FIXTURE['structured'])},
            'finish_reason':'stop'}]}).encode())

    def test_current_reference_separates_approved_recall_and_supply_assumptions(self):
        context = ModelAdapter.reference_context(self.case)
        workflow = context['workflow']; capacity = context['capacity']; scope = context['customer_scope']
        self.assertTrue(workflow['recall_authorized'])
        self.assertEqual(workflow['review_scope'], 'supply')
        self.assertEqual(workflow['latest_decision']['decision'], 'approve')
        self.assertEqual(workflow['latest_decision']['scope'], 'recall')
        self.assertFalse(workflow['external_execution'])
        self.assertEqual(capacity['capacity_qty'], 1949480)
        self.assertEqual(capacity['utilization_pct'], 97)
        self.assertEqual(capacity['residual_capacity_qty'], 58484)
        self.assertNotIn('working_days', capacity['residual_formula'])
        self.assertEqual(capacity['capacity_period_assumption'], 'annual_not_specified_in_original')
        self.assertEqual(capacity['working_days_assumption'], 261)
        self.assertEqual(capacity['daily_capacity_limit'], 224)
        self.assertEqual(capacity['source']['row_number'], 3)
        self.assertEqual(scope['claim_without_batch_shipment'], ['C-105'])
        self.assertEqual(scope['batch_shipment_customers'], ['C-101','C-102','C-103','C-104'])
        self.assertFalse(scope['shipment_is_current_inventory'])

    def test_compaction_keeps_all_business_values_rows_assumptions_and_source_files(self):
        before = copy.deepcopy(self.case)
        snapshot = ModelAdapter.input_snapshot(self.case)
        def without_provenance(value):
            if isinstance(value, list): return [without_provenance(v) for v in value]
            if not isinstance(value, dict): return value
            return {k:without_provenance(v) for k,v in value.items() if k not in ('sha256','path','row_hash')}
        self.assertEqual(without_provenance(snapshot['analysis']), without_provenance(self.case['analysis']))
        self.assertEqual(snapshot['conditions'], self.case['conditions'])
        self.assertEqual(len(snapshot['analysis']['evidence']), 23)
        for row in self.case['analysis']['evidence']:
            self.assertEqual(snapshot['source_manifest'][row['source_id']], {k:row[k] for k in ('path','sha256')})
        self.assertIn('policy', snapshot['source_manifest'])
        self.assertEqual(self.case, before)
        # Byte reduction only. This does not assert a provider token count.
        old = {'case_id':self.case['id'],'revision':self.case['review_revision'], 'batch_id':self.case['batch_id'],
            'analysis':self.case['analysis'],'conditions':self.case['conditions'],
            'canonical_facts':snapshot['canonical_facts'], 'workflow_context':{
                k:snapshot['workflow_context'][k] for k in ('status','recall_authorized','simulated_decision','external_execution')}}
        self.assertLess(len(dumps(snapshot).encode()), len(dumps(old).encode()))

    def test_lifecycle_scope_customer_and_source_changes_invalidate_the_input(self):
        original = self.service._ai_input_fingerprint(self.case)
        for field, value in [('status','awaiting_approval'),('recall_authorized',False),
            ('correction_review_required',True),('review_scope','quality_correction'),('source_fingerprint','changed')]:
            candidate=copy.deepcopy(self.case); candidate[field]=value
            self.assertNotEqual(original,self.service._ai_input_fingerprint(candidate),field)
        candidate=copy.deepcopy(self.case);candidate['decisions'][0]['scope']='supply'
        self.assertNotEqual(original,self.service._ai_input_fingerprint(candidate))
        candidate=copy.deepcopy(self.case);candidate['customers'][0]['shipment_count']=0
        self.assertNotEqual(original,self.service._ai_input_fingerprint(candidate))
        # An AI review changes case version, not model input; it must stay reusable.
        candidate=copy.deepcopy(self.case);candidate['version']+=1
        self.assertEqual(original,self.service._ai_input_fingerprint(candidate))

    def test_old_incomplete_fingerprint_is_not_current(self):
        old=digest({k:self.case[k] for k in ('id','review_revision','batch_id','analysis','conditions')})
        self.assertNotEqual(old,self.service._ai_input_fingerprint(self.case))

    def test_recorded_bad_narrative_does_not_gain_semantic_trust_or_change_facts(self):
        before=copy.deepcopy(self.case)
        with patch.object(self.service.ai.opener,'open',return_value=self.response()):
            brief=self.service.ai.generate(self.case)
        self.assertIn('품질위원회 승인 전',brief['structured']['risks'][1])
        self.assertIn('58,484EA',brief['structured']['risks'][2])
        self.assertTrue(brief['canonical_facts_validated'])
        self.assertFalse(brief['narrative_screening']['semantic_verified'])
        self.assertEqual(brief['approval_effect'],'none')
        self.assertEqual(brief['reference_context'],ModelAdapter.reference_context(self.case))
        self.assertTrue(brief['reference_context']['workflow']['recall_authorized'])
        self.assertEqual(brief['reference_context']['capacity']['working_days_assumption'],261)
        self.assertEqual(self.case,before)

    def test_model_input_audit_and_fingerprint_exactly_match_transmitted_user_data(self):
        with patch.object(self.service.ai.opener,'open',return_value=self.response()) as calls:
            detail=self.op('ai-brief')
        payload=json.loads(calls.call_args.args[0].data)
        transmitted=json.loads(payload['messages'][1]['content'])
        event=next(e for e in detail['audit'] if e['action']=='ai_brief_created')
        self.assertEqual(event['payload']['model_input'],transmitted)
        self.assertEqual(detail['ai']['brief']['input_fingerprint'],digest(transmitted))
        self.assertEqual(transmitted,ModelAdapter.input_snapshot(self.case))
        self.assertFalse(detail['ai']['brief']['stale'])
        self.assertTrue(self.service.verify_audit()['valid'])
        prompt=payload['messages'][0]['content']
        for text in ('기승인 사실','원본 기간 미기재','261영업일','출하 고객','확인 질문'):
            self.assertIn(text,prompt)
        for key in ('recall_authorized','status','conditions','tasks','decisions'):
            self.assertEqual(detail[key],self.case[key])

    def test_facts_mismatch_still_rejected_and_does_not_change_workflow(self):
        bad=copy.deepcopy(FIXTURE['structured']);bad['facts']['produced_qty']=58484
        before=self.service.detail('CASE-001')
        with patch.object(self.service.ai.opener,'open',return_value=self.response(bad)):
            with self.assertRaises(APIError) as caught:self.op('ai-brief')
        self.assertEqual(caught.exception.diagnostic['validation_reason'],'canonical_fact_mismatch')
        after=self.service.detail('CASE-001')
        for key in ('version','status','recall_authorized','decisions','tasks','conditions'):
            self.assertEqual(before[key],after[key])

    def test_correction_context_does_not_claim_current_approval(self):
        corrected=self.op('conditions',changes={'confirmed_qty_override':1000},reason='오프라인 정정 시험')
        context=ModelAdapter.reference_context(corrected)
        self.assertFalse(context['workflow']['recall_authorized'])
        self.assertTrue(context['workflow']['correction_review_required'])
        self.assertEqual(context['workflow']['review_scope'],'quality_correction')
        self.assertEqual(context['workflow']['latest_decision']['decision'],'approve')

    def test_missing_capacity_is_not_filled_from_model_text(self):
        case=copy.deepcopy(self.case);case['analysis']['recovery_plan']={}
        context=ModelAdapter.reference_context(case)
        self.assertIsNone(context['capacity']['capacity_qty'])
        self.assertIsNone(context['capacity']['residual_capacity_qty'])

    def test_review_from_before_submission_cannot_link_to_later_decision(self):
        service = AuroraService(db_path=self.service.db_path+'.draft')
        def change(path, actor='quality', **payload):
            payload['expected_version']=service.detail('CASE-001')['version']
            return service.mutate('/api/cases/CASE-001/'+path,payload,actor,path)
        with patch.object(service.ai.opener,'open',return_value=self.response()):
            detail=change('ai-brief')
        reviewed=change('ai-review',brief_id=detail['ai']['brief']['id'],field='summary',index=0,
            disposition='defer',note='상신 전 자료이므로 의결 상태 변화 뒤 재검토합니다.')
        review_id=reviewed['ai']['reviews'][0]['id']
        submitted=change('submit')
        self.assertTrue(submitted['ai']['brief']['stale'])
        self.assertTrue(submitted['ai']['reviews'][0]['stale'])
        with self.assertRaises(APIError) as caught:
            change('decision','committee',decision='approve',reason='이전 검토를 새 상태에 연결하려는 시험',ai_review_id=review_id)
        self.assertEqual(caught.exception.code,'STALE_AI_REVIEW')
        self.assertFalse(service.detail('CASE-001')['recall_authorized'])

    def test_human_attestation_keeps_comparison_context_without_certifying_prose(self):
        with patch.object(self.service.ai.opener,'open',return_value=self.response()):
            detail=self.op('ai-brief')
        reviewed=self.op('ai-review',brief_id=detail['ai']['brief']['id'],field='summary',index=0,
            disposition='apply',note='원문을 확정 사실로 채택하지 않고 직접 대조한 판단만 참고합니다.',
            source_checked=True,policy_checked=True,evidence_ids=['B-02-2602-01'],
            verification_note='이미 모의 승인됨을 확인했고 연간·261영업일 가정과 C-105 연결 미확인을 별도 대조했습니다.')
        payload=reviewed['ai']['reviews'][0]['payload']
        self.assertFalse(payload['narrative_trusted'])
        self.assertEqual(payload['approval_effect'],'none')
        self.assertEqual(payload['source_verification']['reference_context'],ModelAdapter.reference_context(self.case))
        self.assertEqual(payload['source_verification']['kind'],'human_attestation_not_machine_verification')

if __name__=='__main__':unittest.main()
