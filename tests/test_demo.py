"""HTTP integration checks for session security and the portfolio workflow."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

from demo import Sessions
from server import Server
from workflow import connect

REQUEST = {"destination": "Seattle", "purpose": "Fictional workshop", "start": "2027-01-15",
           "end": "2027-01-17", "budget": "750", "cost_center": "Engineering"}
POLICY = {"request_cap": "2000", "Flight": "600", "Rail": "200", "Hotel": "75", "Ground transport": "100"}


class Visitor:
    def __init__(self, port):
        self.port = port
        self.cookie = ""
        self.state = {}

    def call(self, path, data=None, **extra_headers):
        headers = {"Cookie": self.cookie, **extra_headers}
        if data is not None:
            headers = {"Content-Type": "application/json", "X-Travel-Demo": "1", **headers}
        with closing(HTTPConnection("127.0.0.1", self.port, timeout=4)) as http:
            http.request("GET" if data is None else "POST", path, None if data is None else json.dumps(data), headers)
            result = http.getresponse()
            if cookie := result.getheader("Set-Cookie"):
                self.cookie = cookie.split(";", 1)[0]
                self.cookie_header = cookie
            body = result.read()
            if result.getheader("Content-Type").startswith("application/json"):
                body = json.loads(body)
            if result.status == 200 and isinstance(body, dict) and "actor" in body:
                self.state = body
            return result.status, body

    def role(self, actor):
        return self.call("/api/role", {"actor": actor})

    def action(self, action, **fields):
        return self.call("/api/trips", {"action": action, "actor": self.state["actor"],
                                        "revision": self.state["revision"], **fields})


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = Server(("127.0.0.1", 0), Sessions(self.directory.name), secure=True)
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.visitor = Visitor(self.server.server_port)

    def start(self):
        self.assertEqual(self.visitor.call("/api/session", {})[0], 200)
        return self.visitor

    def action(self, action, **fields):
        code, body = self.visitor.action(action, **fields)
        self.assertEqual(code, 200, body)
        return body["id"]

    def test_ephemeral_bootstrap_is_idempotent_and_health_is_stateless(self):
        directory = Path(self.directory.name) / "fresh"
        self.server.sessions = Sessions(directory, ephemeral=True)
        seed = directory / ".seed.sqlite3"
        original = seed.read_bytes()
        self.assertEqual(self.visitor.call("/healthz"), (200, {"status": "ok"}))
        self.assertEqual(list(directory.iterdir()), [seed])
        self.start()
        self.action("create", **REQUEST)
        self.server.sessions = Sessions(directory, ephemeral=True)
        self.assertEqual(seed.read_bytes(), original)
        self.assertEqual(self.visitor.call("/api/trips")[1]["analytics"]["requests"], 5)

    def test_ephemeral_recovery_rotates_lost_cookie_and_rejects_stale_writes(self):
        directory = Path(self.directory.name)
        self.server.sessions = Sessions(directory, ephemeral=True)
        visitor = self.start()
        self.action("create", **REQUEST)
        visitor.role("jordan")
        old_cookie, old_revision = visitor.cookie, visitor.state["revision"]
        for database in directory.glob("*.sqlite3"):
            database.unlink()
        self.server.sessions = Sessions(directory, ephemeral=True)
        self.assertEqual(visitor.action("approve_trip", id=1)[0], 403)
        self.assertEqual(Visitor(self.server.server_port).call("/api/trips")[0], 403)
        status, restored = visitor.call("/api/trips")
        self.assertEqual(status, 200)
        self.assertTrue(restored["session_restored"])
        self.assertEqual(restored["actor"], "alex")
        self.assertEqual(restored["analytics"]["requests"], 4)
        self.assertNotEqual(visitor.cookie, old_cookie)
        self.assertNotEqual(restored["revision"], old_revision)
        self.assertEqual(visitor.action("create", **REQUEST, revision=old_revision)[0], 403)
        self.assertNotIn("session_restored", visitor.call("/api/trips")[1])

    def test_ephemeral_concurrent_sessions_and_reset_are_independent(self):
        self.server.sessions = Sessions(self.directory.name, ephemeral=True)
        visitors = [self.visitor, Visitor(self.server.server_port)]

        def submit(visitor, destination):
            self.assertEqual(visitor.call("/api/session", {})[0], 200)
            self.assertEqual(visitor.action("create", **{**REQUEST, "destination": destination})[0], 200)
            return visitor.call("/api/trips")[1]["trips"][0]["destination"]

        with ThreadPoolExecutor(max_workers=2) as workers:
            destinations = list(workers.map(submit, visitors, ["Guest A", "Guest B"]))
        self.assertEqual(destinations, ["Guest A", "Guest B"])
        self.assertNotEqual(visitors[0].state["revision"], visitors[1].state["revision"])
        self.assertEqual(visitors[0].call("/api/reset", {})[1]["analytics"]["requests"], 4)
        self.assertEqual(visitors[1].call("/api/trips")[1]["trips"][0]["destination"], "Guest B")

    def test_server_roles_csrf_and_forged_header(self):
        v = self.visitor
        self.assertEqual(v.call("/api/trips")[0], 403)
        self.assertEqual(v.call("/api/session", {}, **{"X-Travel-Demo": ""})[0], 403)
        self.start()
        for option in ("HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(option, v.cookie_header)
        self.assertEqual(v.call("/api/report.csv", **{"X-Demo-Actor": "sam"})[0], 403)
        self.assertEqual(v.action("approve_trip", id=1, actor="jordan")[0], 403)
        self.assertEqual(v.action("approve_trip", id=1)[0], 403)
        self.assertEqual(v.action("policy", **POLICY)[0], 403)
        self.assertEqual(v.action("workspace_config", name="Fictional workspace")[0], 403)
        self.assertEqual(v.role("unknown")[0], 400)
        original_revision = v.state["revision"]
        self.assertEqual(v.role("jordan")[0], 200)
        self.assertEqual(v.action("policy", **POLICY)[0], 403)
        self.assertEqual(v.role("taylor")[0], 200)
        self.assertEqual(v.action("approve_trip", id=1)[0], 403)
        self.action("policy", **POLICY)
        self.action("workspace_config", name="Fictional workspace")
        self.assertEqual(v.role("alex")[1]["workspace_name"], "Fictional workspace")
        self.assertEqual(v.action("create", **REQUEST, revision=original_revision)[0], 403)
        self.assertEqual(v.role("sam")[0], 200)
        self.assertEqual(v.call("/api/report.csv")[0], 200)

    def test_session_isolation_reset_restart_and_stale_form(self):
        v = self.start()
        other = Visitor(self.server.server_port)
        self.assertEqual(other.call("/api/session", {})[0], 200)
        self.assertNotEqual(v.cookie, other.cookie)
        self.action("create", **REQUEST)
        self.assertEqual(v.call("/api/trips")[1]["analytics"]["requests"], 5)
        self.assertEqual(other.call("/api/trips")[1]["analytics"]["requests"], 4)
        self.server.sessions = Sessions(self.directory.name)
        self.assertEqual(v.call("/api/trips")[1]["analytics"]["requests"], 5)
        stale_key = v.state["revision"]
        self.assertEqual(v.call("/api/reset", {})[1]["analytics"]["requests"], 4)
        self.assertEqual(v.action("create", **REQUEST, revision=stale_key)[0], 403)
        self.assertEqual(other.call("/api/trips")[1]["analytics"]["requests"], 4)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.sqlite3"))), 2)

    def test_expired_session_does_not_mutate_or_reuse_cookie(self):
        v = self.start()
        previous = v.cookie
        self.server.sessions.ttl = 0
        self.assertEqual(v.call("/api/reset", {})[0], 403)
        self.server.sessions.ttl = 3600
        self.assertEqual(v.call("/api/session", {})[0], 200)
        self.assertNotEqual(previous, v.cookie)

    def test_end_to_end_refund_reconciliation_and_analytics(self):
        v = self.start()
        trip = self.action("create", **REQUEST)
        v.role("jordan")
        self.action("approve_trip", id=trip)
        v.role("alex")
        self.action("book", id=trip, kind="Flight")
        self.action("receipt", id=trip, reference="DEMO-RAIL")
        self.action("submit_claim", id=trip)
        v.role("jordan")
        self.action("approve_claim", id=trip)
        v.role("sam")
        self.action("refund_booking", id=4, booking_id=2, amount="68", final="yes", reason="Demo refund")
        order = next(t for t in v.state["trips"] if t["id"] == trip)["bookings"][0]["id"]
        self.action("statement", id=trip, lines=f"B-{order:05d},244", reason="Test mismatch")
        self.action("reconcile", id=trip)
        mismatch = v.call("/api/trips")[1]["trips"][0]
        self.assertEqual(mismatch["status"], "Discrepancy")
        self.assertIn("Amount mismatch", mismatch["differences"][0])
        self.action("statement", id=trip, lines=f"B-{order:05d},245", reason="Corrected supplier line")
        self.action("reconcile", id=trip)
        self.action("voucher", id=trip)
        self.action("pay", id=trip, outcome="success")
        analytics = v.call("/api/trips")[1]["analytics"]
        self.assertEqual(analytics["reimbursements"]["Paid"], 1)
        self.assertEqual(analytics["monthly"]["2027-01"], 31300)
        self.assertEqual(analytics["expenses"], 49700)
        with closing(connect(next(Path(self.directory.name).glob("*.sqlite3")))) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT trip_id FROM statement_lines WHERE reference=?",
                                        (f"B-{order:05d}",)).fetchone()[0], trip)

    def test_frozen_nightly_policy_and_violation_audit(self):
        v = self.start()
        v.role("taylor")
        self.action("policy", **POLICY, hotel_limit_basis="night", effective_from="2027-01-15")
        v.role("alex")
        code, message = v.action("create", **{**REQUEST, "budget": "2500"}, request_id="policy-block")
        self.assertEqual(code, 422)
        self.assertIn("policy cap", message["error"])
        self.assertEqual(v.action("create", **{**REQUEST, "budget": "2500"}, request_id="policy-block")[0], 422)
        trip = self.action("create", **REQUEST)
        v.role("jordan")
        self.action("approve_trip", id=trip)
        v.role("taylor")
        self.action("policy", **{**POLICY, "Hotel": "20"}, effective_from="2027-01-15")
        v.role("alex")
        self.action("book", id=trip, kind="Hotel", payment_mode="Company")
        code, message = v.action("amend_booking", id=trip, booking_id=3, amount="160", reason="Demo overage")
        self.assertEqual(code, 422)
        self.assertIn("150.00", message["error"])
        state = v.call("/api/trips")[1]
        self.assertEqual(state["analytics"]["violations"], 3)
        frozen = json.loads(state["trips"][0]["policy_snapshot"])
        self.assertEqual(frozen["Hotel"], 7500)
        self.assertEqual(frozen["_controls"]["hotel_limit_basis"], "night")
