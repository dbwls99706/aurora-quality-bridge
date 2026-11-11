"""Offline fixtures only: review linkage does NOT verify a real AI provider."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import APIError, AuroraService


class AIHumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.name + '/state.sqlite3'
        with patch.dict(os.environ, {}, clear=True):
            self.service = AuroraService(db_path=self.db)
        self.sequence = 0

    def op(self, action, actor='quality', case='CASE-001', **payload):
        self.sequence += 1
        payload.setdefault('expected_version', self.service.detail(case)['version'])
        return self.service.mutate(f'/api/cases/{case}/{action}', payload, actor, f'review-{self.sequence}')

    def generate(self, case='CASE-001'):
        def fixture(d):
            return {'kind':'test_fixture','model':'OFFLINE TEST ONLY','prompt_hash':'fixture-not-real',
                    'review_revision':d['review_revision'],'generated_at':'2026-11-10T00:00:00+00:00',
                    'structured':{'summary':'시험용 근거 요약입니다.', 'risks':['고객 범위를 추가 확인해야 합니다.'],
                                  'alternatives':['출하 연결이 없는 고객의 범위 확인을 먼저 요청합니다.'],
                                  'evidence_ids':[d['claims'][0]['claim_id']]}}
        with patch.object(self.service.ai, 'generate', side_effect=fixture), patch('urllib.request.urlopen') as network:
            detail = self.op('ai-brief', case=case)
            network.assert_not_called()
        return detail

    def review(self, actor='quality', case='CASE-001', **changes):
        brief = self.service.detail(case)['ai']['brief']
        payload = {'brief_id':brief['id'], 'field':'alternatives','index':0,
                   'disposition':'apply','note':'시험용 사람 검토: C-105 범위 미확인을 후속 담당자 확인에 반영합니다.',
                   'evidence_ids':[self.service.detail(case)['analysis']['evidence'][0]['record_id']],
                   'verification_note':'시험용 원본 대조: 배치 생산 수량과 확정 수량을 확인하고 미매핑 고객은 별도 확인합니다.',
                   'source_checked':True,'policy_checked':True}
        payload.update(changes)
        return self.op('ai-review', actor, case, **payload)

    def expect(self, code, callback):
        with self.assertRaises(APIError) as e:
            callback()
        self.assertEqual(code, e.exception.code)

    def test_input_output_snapshot_is_preserved_after_regeneration(self):
        first = self.generate()
        event = next(e for e in first['audit'] if e['action']=='ai_brief_created')
        snapshot = copy.deepcopy(event)
        self.assertEqual(event['payload']['input']['analysis'], first['analysis'])
        self.assertEqual(event['payload']['output']['kind'], 'test_fixture')
        self.assertEqual(event['payload']['brief_id'], first['ai']['brief']['id'])
        second = self.generate()
        self.assertNotEqual(first['ai']['brief']['id'], second['ai']['brief']['id'])
        self.assertEqual(snapshot, next(e for e in second['audit'] if e['id']==snapshot['id']))
        self.assertTrue(self.service.verify_audit()['valid'])

    def test_review_preserves_workflow_and_survives_restart(self):
        before = self.generate()
        after = self.review()
        for key in ('status','conditions','tasks','decisions','recall_authorized','review_revision'):
            self.assertEqual(before[key], after[key])
        review = after['ai']['reviews'][0]
        self.assertFalse(review['stale'])
        self.assertEqual(review['payload']['selected_text'], before['ai']['brief']['structured']['alternatives'][0])
        self.assertEqual(review['payload']['reviewer_kind'], 'simulated_role')
        self.assertEqual(review['payload']['approval_effect'], 'none')
        self.assertFalse(after['ai']['connected'], 'offline fixture cannot verify provider status')
        with patch.dict(os.environ, {}, clear=True):
            restarted = AuroraService(db_path=self.db)
        self.assertEqual(after['ai']['reviews'], restarted.detail('CASE-001')['ai']['reviews'])

    def test_review_links_to_committee_decision_only_by_explicit_choice(self):
        self.op('submit')
        self.generate()
        reviewed = self.review('committee')
        review_id = reviewed['ai']['reviews'][0]['id']
        after = self.op('decision','committee',decision='approve',reason='시험용 AI 대안의 고객 범위 확인은 후속으로 남기고 원본 기준에 따라 의결',ai_review_id=review_id)
        event = next(e for e in after['audit'] if e['action']=='committee_approve')
        self.assertEqual(event['payload']['ai_review']['review_id'], review_id)
        self.assertTrue(after['recall_authorized'])
        self.assertTrue(self.service.verify_audit()['valid'])

    def test_review_links_to_department_reply(self):
        self.op('submit');self.op('decision','committee',decision='approve',reason='모의 원본 검토 승인')
        self.generate();reviewed = self.review('sales',disposition='defer')
        task = next(t for t in reviewed['tasks'] if t['type']=='customer_scope_check')
        after = self.op('tasks/'+task['id']+'/reply','sales',outcome='blocked',note='모의 확인: AI가 제시한 C-105 연결 근거는 확인되지 않아 범위 재확인',ai_review_id=reviewed['ai']['reviews'][0]['id'])
        event = next(e for e in after['audit'] if e['action']=='simulated_reply')
        self.assertEqual(event['payload']['ai_review']['disposition'],'defer')
        self.assertEqual(event['payload']['task_id'],task['id'])
        self.assertFalse(after['analysis']['recovery_plan']['feasible'])

    def test_old_review_cannot_be_reused_after_conditions_change(self):
        self.op('submit');self.generate();reviewed = self.review()
        review_id = reviewed['ai']['reviews'][0]['id']
        self.op('conditions',changes={'replacement_qty':1000},reason='모의 수량 변경으로 근거 갱신')
        self.assertTrue(self.service.detail('CASE-001')['ai']['reviews'][0]['stale'])
        self.expect('STALE_AI_REVIEW',lambda:self.review())
        self.expect('STALE_AI_REVIEW',lambda:self.op('decision','committee',decision='approve',reason='오래된 검토 재사용 금지 시험',ai_review_id=review_id))
        self.assertFalse(self.service.detail('CASE-001')['recall_authorized'])

    def test_regeneration_stales_prior_review_even_with_same_facts(self):
        self.generate();self.review();self.generate()
        self.assertTrue(self.service.detail('CASE-001')['ai']['reviews'][0]['stale'])

    def test_cross_case_and_forged_review_links_are_denied(self):
        self.generate();reviewed=self.review();self.op('submit',case='CASE-002')
        for invalid in (True, '1', 99999, reviewed['ai']['reviews'][0]['id']):
            self.expect('INVALID_AI_REVIEW',lambda v=invalid:self.op('decision','committee',case='CASE-002',decision='approve',reason='다른 사건 검토 연결 금지 시험',ai_review_id=v))

    def test_review_does_not_grant_committee_authority(self):
        self.op('submit');self.generate();reviewed=self.review('sales')
        self.expect('FORBIDDEN',lambda:self.op('decision','sales',decision='approve',reason='권한 상승 차단 시험',ai_review_id=reviewed['ai']['reviews'][0]['id']))

    def test_unreviewed_decision_is_not_falsely_linked(self):
        self.op('submit');self.generate();self.review()
        after=self.op('decision','committee',decision='approve',reason='AI 기록을 연결하지 않은 원본 검토 의결')
        event=next(e for e in after['audit'] if e['action']=='committee_approve')
        self.assertIsNone(event['payload']['ai_review'])

    def test_missing_result_and_invalid_review_fields_rejected(self):
        self.expect('AI_BRIEF_REQUIRED',lambda:self.op('ai-review',brief_id='fake',field='summary',index=0,disposition='apply',note='검토 대상 없음'))
        self.generate()
        for fields in ({'field':'invented'},{'index':True},{'index':-1},{'index':9},{'disposition':'approved'}, {'note':'짧음'}, {'note':'x'*3001},{'note':{'bad':'type'}}):
            self.expect('INVALID_AI_REVIEW',lambda v=fields:self.review(**v))
        self.assertEqual([],self.service.detail('CASE-001')['ai']['reviews'])

    def test_apply_requires_explicit_source_and_policy_attestation(self):
        self.generate()
        for changed in ({'source_checked':False},{'source_checked':'true'},{'policy_checked':False},
                        {'evidence_ids':[]},{'evidence_ids':['QC-99999']},{'evidence_ids':['QC-00001','QC-00001']},
                        {'verification_note':'짧음'},{'verification_note':None}):
            with self.subTest(changed=changed):
                self.expect('AI_SOURCE_CHECK_REQUIRED',lambda v=changed:self.review(**v))
        self.assertEqual([],self.service.detail('CASE-001')['ai']['reviews'])
        accepted=self.review()['ai']['reviews'][0]
        self.assertTrue(accepted['linkable'])
        self.assertEqual(accepted['payload']['source_verification']['kind'],'human_attestation_not_machine_verification')

    def test_reject_and_defer_can_be_recorded_without_falsely_attesting(self):
        self.generate()
        for disposition in ('reject','defer'):
            reviewed=self.review(disposition=disposition,source_checked=False,policy_checked=False,evidence_ids=[],verification_note=None)
            record=reviewed['ai']['reviews'][0]
            self.assertIsNone(record['payload']['source_verification'])
            self.assertTrue(record['linkable'])

    def test_legacy_apply_review_cannot_link_without_new_source_check(self):
        self.op('submit');self.generate();reviewed=self.review()
        record=reviewed['ai']['reviews'][0]
        # Simulate an old-version audit record in this isolated test database.
        payload={k:v for k,v in record['payload'].items() if k!='source_verification'}
        with self.service.connect() as db:
            self.service._audit(db,'CASE-001','ai_brief_reviewed','quality',payload)
        legacy=self.service.detail('CASE-001')['ai']['reviews'][0]
        self.assertFalse(legacy['stale']);self.assertFalse(legacy['linkable'])
        self.expect('AI_SOURCE_CHECK_REQUIRED',lambda:self.op('decision','committee',decision='approve',reason='이전 검토의 자동 재사용 금지',ai_review_id=legacy['id']))
        self.assertFalse(self.service.detail('CASE-001')['recall_authorized'])

    def test_unsafe_narrative_is_not_a_classifier_gate_or_workflow_authority(self):
        before=self.generate()
        # Pretend an older release retained unchecked text, using only a temp DB.
        brief=before['ai']['brief'];brief['structured']['summary']='고객 통지 발송 완료.'
        import json
        with self.service.connect() as db:
            db.execute('UPDATE cases SET ai_brief=? WHERE id=?',(json.dumps(brief),'CASE-001'))
        # Human attestation is not a semantic classifier. Raw text never gains authority.
        reviewed=self.review()
        self.assertFalse(reviewed['ai']['reviews'][0]['payload']['narrative_trusted'])
        self.assertFalse(reviewed['recall_authorized'])
        self.assertEqual(reviewed['tasks'],before['tasks'])
        rejected=self.review(disposition='reject')
        self.assertEqual(rejected['ai']['reviews'][0]['payload']['disposition'],'reject')
        self.assertFalse(rejected['recall_authorized'])

    def test_review_idempotency_does_not_create_duplicate_audit(self):
        self.generate();before=self.service.detail('CASE-001')
        payload={'expected_version':before['version'],'brief_id':before['ai']['brief']['id'],'field':'summary','index':0,'disposition':'reject','note':'시험용 요약은 원본 대조가 충분하지 않아 미채택합니다.'}
        one=self.service.mutate('/api/cases/CASE-001/ai-review',payload,'quality','same-review')
        two=self.service.mutate('/api/cases/CASE-001/ai-review',payload,'quality','same-review')
        self.assertEqual(one,two)
        self.assertEqual(1,len(self.service.detail('CASE-001')['ai']['reviews']))

if __name__=='__main__':unittest.main()
