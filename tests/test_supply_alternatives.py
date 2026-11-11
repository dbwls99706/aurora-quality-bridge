"""Independent checks for conserved total, cautious split scheduling and read-only API."""
import copy
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import APIError, AuroraService, make_server

class SupplyAlternativesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=AuroraService(db_path=self.tmp.name+'/test.sqlite3')
        self.detail=self.service.detail('CASE-001')
        self.ids=sorted(c['customer_id'] for c in self.detail['customers'])

    def tearDown(self): self.tmp.cleanup()

    def database_snapshot(self, db):
        if hasattr(db, 'iterdump'):
            return list(db.iterdump())
        tables = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'").fetchall()
        # Names come from the isolated database catalog, not request input.
        return {r['table_name']: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                for row in db.execute('SELECT * FROM "' + r['table_name'].replace('"', '""') + '"').fetchall())
                for r in tables}

    def compare(self,total=17900,early=1000,entries=None):
        return self.service.supply_alternatives('CASE-001',total,early,entries or [])

    def entry(self,index=0,need=1000,early=1000,late=0,due='2026-07-20'):
        return {'customer_id':self.ids[index],'required_quantity':need,'early_quantity':early,'remaining_quantity':late,'due_date':due}

    def error(self,fn,code=None):
        with self.assertRaises(APIError) as c: fn()
        if code:self.assertEqual(c.exception.code,code)

    def test_stress_reference_and_no_default_demand_inference(self):
        r=self.compare();a,b=r['alternatives']
        self.assertEqual((a['production_days'],a['final_ship_date'],a['final_delay_calendar_days']),(90,'2026-11-11',114))
        self.assertEqual([(x['quantity'],x['ship_date']) for x in b['lots']],[(1000,'2026-07-15'),(16900,'2026-11-13')])
        self.assertEqual(b['final_delay_calendar_days'],116)
        self.assertEqual(r['tradeoff']['final_supply_later_calendar_days'],2)
        self.assertEqual(r['quantity_basis'],'user_entered_simulation_not_verified_replacement_demand')
        self.assertTrue(r['quantity_conserved'])
        self.assertEqual(r['unknown_customer_demand_count'],len(self.ids))
        self.assertEqual(a['unallocated_quantity'],17900)
        self.assertEqual(r['cost_comparison']['amount'],None)

    def test_same_customer_total_for_both_options_deadlines_and_unknowns(self):
        entries=[self.entry(),self.entry(1,16900,0,16900,'2026-11-12')]
        r=self.compare(entries=entries);a,b=r['alternatives']
        self.assertEqual((a['on_time_quantity'],a['late_quantity']),(16900,1000))
        self.assertEqual((b['on_time_quantity'],b['late_quantity']),(1000,16900))
        self.assertEqual(r['tradeoff']['on_time_quantity_change'],-15900)
        for result in (a,b):
            self.assertEqual(result['unallocated_quantity'],0)
            self.assertEqual(result['allocated_quantity'],17900)
            self.assertEqual(sum(c['assigned_quantity'] for c in result['customers']),17900)
            self.assertTrue(result['missing_information']) # remaining customers are still unknown, not not_required
            self.assertFalse(result['feasible'])
            self.assertEqual(result['approval_status'],'not_confirmed')
            self.assertEqual(result['customer_agreement'],'pending')
        self.assertEqual(b['customers'][1]['max_delay_calendar_days'],1)

    def test_partial_allocations_preserve_per_lot_and_customer_unmet_quantity(self):
        r=self.compare(entries=[self.entry(need=3000,early=500,late=400)])
        a,b=r['alternatives']
        self.assertEqual((a['allocated_quantity'],a['unallocated_quantity']),(900,17000))
        self.assertEqual([l['unallocated_quantity'] for l in b['lots']],[500,16500])
        self.assertEqual(a['customers'][0]['unmet_quantity'],2100)
        self.assertEqual(a['customers'][0]['assigned_quantity'],b['customers'][0]['assigned_quantity'])

    def test_unknown_deadlines_never_claim_on_time(self):
        a,b=self.compare(entries=[self.entry(due=None)])['alternatives']
        for r in (a,b):
            self.assertEqual((r['on_time_quantity'],r['late_quantity'],r['unknown_deadline_quantity']),(0,0,1000))
            self.assertIsNone(r['customers'][0]['on_time_quantity'])
            self.assertIsNone(r['customers'][0]['max_delay_calendar_days'])

    def test_boundary_rounding_and_no_parallel_qa(self):
        a,b=self.compare(201,1)['alternatives']
        self.assertEqual(a['production_days'],2)
        self.assertEqual([l['production_days'] for l in b['lots']],[1,1])
        self.assertEqual(a['final_ship_date'],'2026-07-10')
        self.assertEqual(b['first_ship_date'],'2026-07-09')
        self.assertEqual(b['final_ship_date'],'2026-07-14')
        a,b=self.compare(200,1)['alternatives']
        self.assertEqual((a['production_days'],b['production_days']),(1,2))
        self.assertEqual(b['final_ship_date'],'2026-07-14')

    def test_bad_total_and_split_rejected(self):
        for total in (None,0,-1,True,1.5,'17900',150746):
            self.error(lambda total=total:self.compare(total,1))
        for early in (None,0,-1,True,1.5,'1000',17900,18000):
            self.error(lambda early=early:self.compare(17900,early),'INVALID_SUPPLY_ALTERNATIVE')

    def test_customer_schema_identity_duplicates_and_fake_approvals_rejected(self):
        e=self.entry()
        cases=[{},'bad',[None],[{**e,'customer_id':'not-in-scope'}],[e,e],[{**e,'approved':True}], [{**e,'due_date':'2026-07-20','approval_status':'confirmed'}]]
        for entries in cases:self.error(lambda entries=entries:self.service.supply_alternatives('CASE-001',17900,1000,entries))

    def test_overallocation_negative_fraction_and_bool_rejected(self):
        for field in ('required_quantity','early_quantity','remaining_quantity'):
            for value in (-1,1.5,True,'1'):
                self.error(lambda field=field,value=value:self.compare(entries=[{**self.entry(),field:value}]))
        self.error(lambda:self.compare(entries=[self.entry(need=999)]))
        self.error(lambda:self.compare(entries=[self.entry(need=1001,early=1001)]))
        self.error(lambda:self.compare(entries=[self.entry(need=17000,early=0,late=17000)]))

    def test_calendar_constraints_apply_to_each_customer(self):
        for due in ('2026-06-30','2026-02-30','2026-7-20','2031-01-01',False,42):
            self.error(lambda due=due:self.compare(entries=[self.entry(due=due)]))
        self.compare(entries=[self.entry(due='2026-07-01')])

    def test_all_customers_known_total_mismatch_and_zero_not_approval(self):
        entries=[self.entry(i,0,0,0,None) for i in range(len(self.ids))]
        r=self.compare(entries=entries)
        self.assertTrue(any('불일치' in s for s in r['alternatives'][0]['missing_information']))
        self.assertFalse(r['alternatives'][0]['feasible'])
        self.assertEqual(r['alternatives'][0]['customers'][0]['approval_status'],'not_confirmed')

    def test_capacity_limit_total_and_shared_reservations(self):
        r=self.compare(60000,1000)
        self.assertTrue(any('잔여능력' in text for text in r['alternatives'][0]['calculated_constraints']))
        with self.service.connect() as db:
            other=self.service._case(db,'CASE-002');c=other['conditions'];c.update(replacement_qty=1000,production_confirmed=True)
            db.execute('UPDATE cases SET conditions=? WHERE id=?',(json.dumps(c),'CASE-002'))
        r=self.compare()
        for a in r['alternatives']:
            self.assertTrue(any('생산 예약의 합계' in s for s in a['calculated_constraints']))

    def test_read_only_and_no_reused_approvals(self):
        with self.service.connect() as db:
            case=self.service._case(db,'CASE-001');c=case['conditions'];c.update(replacement_qty=17900,inventory_verified=True,production_confirmed=True,qa_release=True)
            db.execute('UPDATE cases SET conditions=?,recall_authorized=1 WHERE id=?',(json.dumps(c),'CASE-001'))
        before=self.service.detail('CASE-001')
        with self.service.connect() as db: before_dump=self.database_snapshot(db)
        result=self.compare(entries=[self.entry()])
        self.assertEqual(before,self.service.detail('CASE-001'))
        with self.service.connect() as db:self.assertEqual(before_dump,self.database_snapshot(db))
        for a in result['alternatives']:
            self.assertFalse(a['physical_supply_ready']);self.assertFalse(a['feasible'])
            self.assertEqual(a['customer_agreement'],'pending');self.assertEqual(len(a['approvals_required']),4)

    def test_stale_version_rejected(self):
        self.error(lambda:self.service.supply_alternatives('CASE-001',17900,1000,[],2),'STALE_VERSION')
        self.error(lambda:self.service.supply_alternatives('CASE-001',17900,1000,[],True),'STALE_VERSION')

    def test_get_api_validation_and_state_unchanged(self):
        server=make_server(self.service,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        params={'total_quantity':'17900','early_quantity':'1000','allocations':'[]','expected_version':'1'}
        def request(changes=None,raw=None):
            return urllib.request.urlopen(base+'/api/cases/CASE-001/supply-alternatives?'+(raw if raw is not None else urllib.parse.urlencode({**params,**(changes or {})})))
        try:
            with request() as response: self.assertTrue(json.load(response)['quantity_conserved'])
            for change in ({'allocations':'not JSON'},{'allocations':'['*10000},{'allocations':'{}'},{'total_quantity':'true'},{'expected_version':'2'},{'early_quantity':'17900'}):
                with self.assertRaises(urllib.error.HTTPError) as c:request(change)
                self.assertIn(c.exception.code,(409,422));c.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as c:request(raw=urllib.parse.urlencode(params)+'&early_quantity=1')
            self.assertEqual(c.exception.code,422);c.exception.close()
            with urllib.request.urlopen(base+'/static/supply-alternatives.js') as response:self.assertEqual(response.status,200)
            self.assertEqual(self.detail,self.service.detail('CASE-001'))
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
