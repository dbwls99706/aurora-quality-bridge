import sys, tempfile, unittest, sqlite3
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.restore_budget_history import restore, RESERVED

class HistoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'budget.sqlite3'
 def tearDown(self):self.tmp.cleanup()
 def test_restore_and_repeat(self):
  self.assertEqual(restore(self.path)['attempts_reserved'],1)
  self.assertEqual(restore(self.path)['action'],'already_restored')
  self.assertEqual(restore(self.path)['reserved_usd'],0.21024)
 def test_preserves_later_usage(self):
  restore(self.path)
  with sqlite3.connect(self.path) as db:db.execute("INSERT INTO ai_reservations(created_at,model,reserved_microusd) VALUES ('later','test',?)",(RESERVED,))
  self.assertEqual(restore(self.path)['attempts_reserved'],2)
  self.assertEqual(restore(self.path)['remaining_attempts_at_most'],1)
 def test_nonempty_unknown_refused(self):
  restore(self.path)
  with sqlite3.connect(self.path) as db:db.execute('DROP TABLE aurora_budget_imports')
  with self.assertRaises(ValueError):restore(self.path)
  with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_reservations').fetchone()[0],1)
 def test_corrupt_refused(self):
  self.path.write_bytes(b'not a database')
  with self.assertRaises(sqlite3.DatabaseError):restore(self.path)
  self.assertEqual(self.path.read_bytes(),b'not a database')
 def test_missing_imported_reservation_refused(self):
  restore(self.path)
  with sqlite3.connect(self.path) as db:db.execute('DELETE FROM ai_reservations')
  with self.assertRaises(ValueError):restore(self.path)
 def test_unrelated_tables_refused(self):
  with sqlite3.connect(self.path) as db:db.execute('CREATE TABLE unrelated(value TEXT)')
  with self.assertRaises(ValueError):restore(self.path)
