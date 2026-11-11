"""Exact local post-failure business rows, read from verified non-secret rollout.

Only a temporary SQLite test database is mutated. The recorded seven audit events,
three cases, one decision and thirteen tasks are replayed, not a live database.
"""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import test_ai_diagnostics as diagnostics
from backend import APIError

class RecordedCaseDiagnosticTests(unittest.TestCase):
    new_adapter=diagnostics.AIDiagnosticTests.new_adapter
    brief=diagnostics.AIDiagnosticTests.brief
    envelope=diagnostics.AIDiagnosticTests.envelope
    transport=diagnostics.AIDiagnosticTests.transport

    def setUp(self):
        diagnostics.AIDiagnosticTests.setUp(self)
        rows=json.loads((Path(__file__).parent/'fixtures'/'postapproval_business_state.json').read_text())
        with sqlite3.connect(self.service.db_path) as db:
            for table in ('audit','decisions','tasks','cases'):
                db.execute('DELETE FROM '+table)
                for row in rows[table]:
                    fields=list(row)
                    db.execute('INSERT INTO '+table+' ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(row[field] for field in fields))
        self.case=self.service.detail('CASE-001')
        self.assertEqual((self.case['status'],self.case['version'],self.case['review_revision']),('approved',3,1))
        self.assertTrue(self.case['recall_authorized'])
        self.assertEqual(self.case['analysis']['defect_rate_pct'],11.874357)
        self.assertTrue(self.service.verify_audit()['valid'])

    def snapshot(self):
        with sqlite3.connect(self.service.db_path) as db:
            return {table:db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall() for table in ('cases','decisions','tasks','audit')}

    def test_exact_recorded_case_failure_preserves_every_old_row(self):
        before=self.snapshot()
        self.assertEqual([len(before[k]) for k in ('cases','decisions','tasks','audit')],[3,1,13,7])
        bad=self.envelope();bad['content'][0]['input']['facts']['defect_rate_pct']=11.87
        with patch.object(self.adapter.opener,'open',side_effect=self.transport(bad)),self.assertRaises(APIError) as caught:
            self.service.mutate('/api/cases/CASE-001/ai-brief',{'expected_version':3},'quality','recorded-fixture-failure')
        self.assertEqual(caught.exception.diagnostic['validation_reason'],'canonical_fact_mismatch')
        after=self.snapshot()
        for table in ('cases','decisions','tasks'):self.assertEqual(before[table],after[table])
        self.assertEqual(before['audit'],after['audit'][:-1])
        self.assertEqual(len(after['audit']),8)
        last=self.service.detail('CASE-001')['audit'][0]
        self.assertEqual(last['payload']['diagnostic']['field'],'defect_rate_pct')
        self.assertTrue(self.service.verify_audit()['valid'])

    def test_exact_recorded_case_mock_success_does_not_execute_workflow(self):
        before=self.snapshot()
        with patch.object(self.adapter.opener,'open',side_effect=self.transport()):
            result=self.adapter.generate(self.case)
        self.assertEqual(result['structured']['facts']['defect_rate_pct'],11.874357)
        self.assertEqual(result['approval_effect'],'none')
        self.assertEqual(before,self.snapshot())

if __name__=='__main__':unittest.main()
