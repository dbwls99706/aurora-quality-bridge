"""Typed facts are validated; prose percentages are ALWAYS untrusted data."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import APIError,AuroraService,ModelAdapter

class ModelNumericValidationTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'AURORA_AI_API_KEY':'OFFLINE-ONLY','AURORA_AI_MODEL':'fixture','AURORA_AI_ALLOW_NETWORK':'1','AURORA_AI_ENDPOINT':'https://provider.test/model'},clear=True)
        env.start();self.addCleanup(env.stop)
        net=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No external calls'))
        net.start();self.addCleanup(net.stop)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.service=AuroraService(db_path=Path(temp.name)/'state.sqlite3');self.case=self.service.detail('CASE-001')
    def response(self,**changes):
        result={'summary':'원본과 대조해야 합니다.','risks':[],'alternatives':[], 'evidence_ids':['QC-00001'],
                'facts':{**{k:self.case['analysis'][k] for k in ('confirmed_qty','produced_qty','defect_rate_pct','recall_eligible')},'recall_threshold_pct':10}}
        result.update(changes);return result
    def transport(self,response):
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(response)}}]}).encode())
    def generate(self,response):
        adapter=ModelAdapter()
        with patch.object(adapter.opener,'open',return_value=self.transport(response)) as net:result=adapter.generate(self.case)
        net.assert_called_once();return result
    def rejected(self,response):
        with self.assertRaises(APIError) as error:self.generate(response)
        self.assertEqual(error.exception.code,'AI_CALL_FAILED')
    def test_correct_structured_facts_are_checked_independently_of_prose(self):
        result=self.generate(self.response())
        self.assertTrue(result['canonical_facts_validated']);self.assertFalse(result['narrative_screening']['semantic_verified'])
    def test_each_wrong_structured_metric_is_rejected(self):
        for field,bad in [('confirmed_qty',150745),('produced_qty',17900),('defect_rate_pct',97),('recall_threshold_pct',97),('recall_eligible',False)]:
            with self.subTest(field=field):self.rejected(self.response(facts={**self.response()['facts'],field:bad}))
    def test_wrong_types_and_nonfinite_values_are_rejected(self):
        for value in [True,'17900',None,float('nan'),float('inf')]:
            with self.subTest(value=value):self.rejected(self.response(facts={**self.response()['facts'],'confirmed_qty':value}))
    def test_missing_and_extra_fact_keys_are_rejected(self):
        valid=self.response()['facts']
        for facts in [{k:v for k,v in valid.items() if k!='produced_qty'},{**valid,'unsupported':1}]:self.rejected(self.response(facts=facts))
    def test_free_prose_percentages_are_not_promoted_to_facts(self):
        for text in ['확정 불량률은 97%입니다.','회수 기준 9.99%','생산 가동률 85%','출하 비율 10%','공급 여력 3%']:
            with self.subTest(text=text):
                result=self.generate(self.response(summary=text));self.assertEqual(result['structured']['summary'],text)
                self.assertEqual(result['structured']['facts'],self.response()['facts']);self.assertFalse(result['narrative_screening']['semantic_verified']);self.assertEqual(result['approval_effect'],'none')
    def test_benign_number_and_percentage_formats_are_not_keyword_rejected(self):
        for text in ['확정 불량품 수량은 17,900 EA입니다.','17,900 EA가 확정 불량 수량입니다.','11.87%는 배치의 확정 불량률입니다.','원본 가동률은 97 퍼센트입니다.','준비 3일, 검사 2일은 참가자 가정입니다.']:
            with self.subTest(text=text):self.assertEqual(self.generate(self.response(summary=text))['structured']['summary'],text)
    def test_explicit_evidence_ids_are_checked_but_prose_citations_are_untrusted(self):
        self.rejected(self.response(evidence_ids=['QC-99999']))
        result=self.generate(self.response(summary='QC-99999가 근거입니다.'))
        self.assertTrue(result['evidence_ids_validated']);self.assertFalse(result['narrative_screening']['semantic_verified'])
    def test_rejected_structured_fact_does_not_mutate_workflow(self):
        before=self.service.detail('CASE-001');bad=self.response(facts={**self.response()['facts'],'recall_threshold_pct':97})
        with patch.object(self.service.ai.opener,'open',return_value=self.transport(bad)):
            with self.assertRaises(APIError):self.service.mutate('/api/cases/CASE-001/ai-brief',{'expected_version':before['version']},'quality','bad-typed-fact')
        after=self.service.detail('CASE-001')
        for key in ('version','status','conditions','tasks','decisions','recall_authorized','ai_brief'):self.assertEqual(before[key],after[key])
        self.assertFalse(self.service.ai.verified)

if __name__=='__main__':unittest.main()
