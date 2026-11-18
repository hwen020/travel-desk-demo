"""Offline workflow tests: no listening server and no project database writes."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

import workflow as w
import server


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        w.initialize(self.db)

    def tearDown(self):
        self.db.close()

    def act(self, actor, action, **fields):
        with self.db:
            return w.mutate(self.db, actor, {"id": 1, "action": action, **fields})

    def status(self):
        return self.db.execute("SELECT status FROM trips WHERE id=1").fetchone()[0]

    def approve(self):
        self.act("jordan", "approve_trip")

    def claim(self):
        self.act("alex", "submit_claim")
        self.act("jordan", "approve_claim")

    def test_full_lifecycle_and_payment_retry(self):
        self.approve()
        self.act("alex", "book", kind="Flight")
        self.act("alex", "receipt", reference="DEMO-RAIL")
        self.claim()
        self.act("sam", "statement", lines="B-00001,244.00", reason="Initial statement")
        self.act("sam", "reconcile")
        self.assertEqual(self.status(), "Discrepancy")
        with self.assertRaises(PermissionError):
            self.act("sam", "voucher")
        self.act("sam", "statement", lines="B-00001,245.00", reason="Supplier corrected amount")
        self.act("sam", "reconcile")
        self.act("sam", "voucher")
        self.act("sam", "pay", outcome="failure")
        self.assertEqual(self.status(), "Voucher ready")
        self.act("sam", "pay", outcome="success")
        with self.assertRaises(PermissionError):
            self.act("sam", "pay")
        self.act("sam", "archive")
        with self.assertRaises(PermissionError):
            self.act("alex", "receipt", reference="AFTER-ARCHIVE")
        self.assertEqual(w.dashboard(self.db, "sam")[0]["employee_total"], 6800)

    def test_cancellation_partial_refund_and_net_statement(self):
        self.approve()
        self.act("alex", "book", kind="Flight")
        self.act("alex", "cancel_booking", booking_id=1, fee="20", reason="Trip change")
        self.assertEqual(w.total(self.db, "bookings", 1), 26500)
        self.act("sam", "refund_booking", booking_id=1, amount="100", final="no", reason="First refund")
        self.assertEqual(w.total(self.db, "bookings", 1), 16500)
        self.claim()
        self.act("sam", "statement", lines="B-00001,165", reason="Interim statement")
        self.act("sam", "reconcile")
        self.assertEqual(self.status(), "Discrepancy")
        self.act("sam", "refund_booking", booking_id=1, amount="145", final="yes", reason="Final refund")
        self.act("sam", "statement", lines="B-00001,20", reason="Final fee only")
        self.act("sam", "reconcile")
        self.assertEqual(self.status(), "Reconciled")
        self.act("sam", "voucher")
        self.assertEqual(w.total(self.db, "bookings", 1), 2000)

    def test_missing_unknown_and_offsetting_statement_differences(self):
        self.approve()
        self.act("alex", "book", kind="Flight")
        self.act("alex", "book", kind="Rail")
        self.claim()
        self.act("sam", "statement", lines="B-00001,244\nB-00002,69", reason="Same total, wrong individual amounts")
        self.act("sam", "reconcile")
        self.assertEqual(len(w.reconciliation(self.db, 1)), 2)
        self.act("sam", "statement", lines="UNKNOWN,313", reason="Unknown order and missing orders")
        self.act("sam", "reconcile")
        self.assertEqual(len(w.reconciliation(self.db, 1)), 3)

    def test_policy_limits_snapshot_and_budget_rollback(self):
        rules = {k: str(v / 100) for k, v in w.DEFAULT_POLICY.items()}
        rules["Flight"] = "200"
        self.act("jordan", "policy", **rules)
        self.approve()
        rules["Flight"] = "600"
        self.act("jordan", "policy", **rules)
        with self.assertRaises(ValueError):
            self.act("alex", "book", kind="Flight")
        self.assertEqual(w.total(self.db, "bookings", 1), 0)
        self.assertEqual(json.loads(self.db.execute("SELECT policy_snapshot FROM trips").fetchone()[0])["Flight"], 20000)
        self.db.execute("UPDATE trips SET budget=6800")
        self.db.commit()
        self.act("alex", "book", kind="Rail")
        with self.assertRaises(ValueError):
            self.act("alex", "cancel_booking", booking_id=1, fee="1", reason="No headroom")
        self.assertEqual(self.db.execute("SELECT state,fee FROM bookings").fetchone()[:], ("Booked", 0))

    def test_delegation_expiry_revocation_and_scope(self):
        self.approve()
        with self.assertRaises(PermissionError):
            self.act("casey", "receipt", reference="DEMO-RAIL")
        self.act("alex", "grant", agent="casey", expires=(date.today() + timedelta(days=1)).isoformat())
        trip = w.dashboard(self.db, "casey")[0]
        self.assertTrue(trip["delegated"])
        self.assertNotIn("book", trip["actions"])
        with self.assertRaises(PermissionError):
            self.act("casey", "book", kind="Flight")
        self.act("casey", "receipt", reference="DEMO-RAIL")
        self.act("alex", "revoke", agent="casey")
        self.assertEqual(w.dashboard(self.db, "casey"), [])
        with self.assertRaises(PermissionError):
            self.act("casey", "submit_claim")
        self.db.execute("UPDATE grants SET active=1,expires='2000-01-01'")
        self.db.commit()
        with self.assertRaises(PermissionError):
            self.act("casey", "submit_claim")

    def test_receipt_correction_failure_duplicates_and_company_order(self):
        self.approve()
        self.act("alex", "receipt", reference="FAIL-001", kind="Rail", amount="10", issued="bad date")
        with self.assertRaises(ValueError):
            self.act("alex", "submit_claim")
        self.act("alex", "correct_receipt", receipt_id=1, reference="FAIL-001", kind="Rail", amount="10", issued="2027-01-12")
        self.act("alex", "verify_receipt", receipt_id=1)
        self.assertEqual(self.db.execute("SELECT verification FROM receipts").fetchone()[0], "Failed")
        self.act("alex", "correct_receipt", receipt_id=1, reference="MANUAL-001", kind="Rail", amount="10", issued="2027-01-12")
        self.assertEqual(self.db.execute("SELECT verified FROM receipts").fetchone()[0], 0)
        self.act("alex", "verify_receipt", receipt_id=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.act("alex", "receipt", reference="MANUAL-001", kind="Rail", amount="10")
        with self.assertRaises(ValueError):
            self.act("alex", "receipt", reference="B-00001", kind="Flight", amount="245")
        self.claim()
        self.act("sam", "reconcile")
        self.act("sam", "voucher")

    def test_returns_amendment_and_reports(self):
        self.act("jordan", "return_trip", reason="Clarify purpose")
        self.act("alex", "resubmit", destination="Seattle", purpose="Workshop", start="2027-01-12", end="2027-01-14", budget="750", cost_center="Engineering")
        self.approve()
        self.act("alex", "book", kind="Flight")
        self.act("alex", "amend_booking", booking_id=1, amount="260", fee="5", reason="New flight time")
        self.claim()
        self.act("sam", "return_claim", reason="Additional receipt")
        self.act("alex", "receipt", reference="DEMO-RAIL")
        self.claim()
        result = w.snapshot(self.db, "sam")
        self.assertEqual(result["reports"]["employee"]["Alex Rivera"]["provider"], 26500)
        self.assertEqual(result["reports"]["department"]["Product"]["employee"], 6800)

    def test_migration_keeps_existing_data(self):
        w.initialize(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trips").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_upgrade_from_original_schema(self):
        old = sqlite3.connect(":memory:")
        old.row_factory = sqlite3.Row
        try:
            old.executescript("""
            CREATE TABLE trips(id INTEGER PRIMARY KEY,owner TEXT,destination TEXT,purpose TEXT,start TEXT,end TEXT,budget INTEGER,cost_center TEXT,status TEXT,provider_total INTEGER,voucher TEXT,paid_at TEXT);
            CREATE TABLE bookings(id INTEGER PRIMARY KEY,trip_id INTEGER,kind TEXT,cents INTEGER,UNIQUE(trip_id,kind));
            CREATE TABLE receipts(id INTEGER PRIMARY KEY,trip_id INTEGER,reference TEXT UNIQUE,kind TEXT,cents INTEGER,verified INTEGER);
            INSERT INTO trips VALUES(9,'alex','Portland','Existing request','2027-01-12','2027-01-14',75000,'Engineering','Approved',NULL,NULL,NULL);
            INSERT INTO bookings VALUES(5,9,'Flight',24500);
            INSERT INTO receipts VALUES(6,9,'LEGACY-RECEIPT','Rail',6800,1);
            """)
            w.initialize(old)
            self.assertEqual(w.dashboard(old, "alex")[0]["id"], 9)
            self.assertEqual(w.total(old, "bookings", 9), 24500)
            self.assertEqual(old.execute("SELECT verification FROM receipts").fetchone()[0], "Verified")
        finally:
            old.close()

    def test_http_read_handlers_offline(self):
        handler = server.Handler.__new__(server.Handler)
        handler.command = "GET"
        output = []
        handler.send_bytes = lambda code, body, mime: output.append((code, body, mime))
        with tempfile.TemporaryDirectory() as directory:
            handler.server = type("OfflineServer", (), {"sessions": server.Sessions(directory)})()
            with handler.server.sessions.open(None, create=True) as (token, path, _):
                db = w.connect(path)
                with db:
                    db.execute("UPDATE demo_session SET actor='sam'")
                db.close()
            handler.headers = {"Cookie": f"travel_demo={token}"}
            for route in ("/", "/app.js", "/style.css", "/api/trips", "/api/report.csv", "/api/summary.csv"):
                handler.path = route
                handler.do_GET()
                self.assertEqual(output[-1][0], 200, route)
            db = w.connect(path)
            with db:
                db.execute("UPDATE demo_session SET actor='alex'")
            db.close()
            handler.path = "/api/report.csv"
            handler.do_GET()
            self.assertEqual(output[-1][0], 403)

    def test_idempotent_refund_and_conflicting_retry(self):
        self.approve()
        self.act("alex", "book", kind="Flight", request_id="booking-1")
        self.act("alex", "book", kind="Flight", request_id="booking-1")
        self.act("alex", "cancel_booking", booking_id=1, fee="0", reason="Cancel")
        for _ in range(2):
            self.act("sam", "refund_booking", booking_id=1, amount="100", final="no", reason="Partial", request_id="refund-1")
        self.assertEqual(w.total(self.db, "bookings", 1), 14500)
        with self.assertRaises(ValueError):
            self.act("sam", "refund_booking", booking_id=1, amount="120", final="no", reason="Partial", request_id="refund-1")

    def test_amendment_fees_accumulate(self):
        self.approve()
        self.act("alex", "book", kind="Flight")
        self.act("alex", "amend_booking", booking_id=1, amount="260", fee="5", reason="First change")
        self.act("alex", "amend_booking", booking_id=1, amount="250", fee="7", reason="Second change")
        self.assertEqual(w.total(self.db, "bookings", 1), 26200)


if __name__ == "__main__":
    unittest.main()
