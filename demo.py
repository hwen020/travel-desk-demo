"""Session storage, fictional seed scenarios and dashboard aggregation."""
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import secrets
import tempfile
from threading import Lock
import time

from workflow import ROOT, ROLES, POLICY_ACTIONS, PolicyViolation, connect, initialize, mutate, snapshot

COOKIE = "travel_demo"
TOKEN = re.compile(r"[0-9a-f]{48}")
TEMPORARY = re.compile(r"[0-9a-f]{48}-[a-z0-9_]+\.tmp(?:-journal)?")
SESSION_SCHEMA = """
CREATE TABLE demo_session (actor TEXT NOT NULL, revision TEXT NOT NULL, name TEXT NOT NULL DEFAULT 'Demo workspace');
CREATE TABLE policy_checks (
    id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
    trip_id INTEGER REFERENCES trips(id), reason TEXT NOT NULL,
    request_id TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(actor, request_id)
);
"""


class SessionLimit(Exception):
    pass


def seed(db):
    initialize(db)
    db.executescript(SESSION_SCHEMA)
    with db:
        db.execute("INSERT INTO demo_session(actor,revision) VALUES ('alex',?)", (secrets.token_hex(16),))

    def apply(actor, action, **fields):
        with db:
            return mutate(db, actor, {"action": action, **fields})

    def trip(owner, destination, purpose, start):
        return apply(owner, "create", destination=destination, purpose=purpose,
                     start=start, end=start, budget="750", cost_center="Engineering")

    completed = trip("alex", "Portland", "Fictional design offsite", "2026-11-04")
    apply("jordan", "approve_trip", id=completed)
    apply("alex", "book", id=completed, kind="Hotel")
    apply("alex", "receipt", id=completed, reference="DEMO-TAXI")
    apply("alex", "submit_claim", id=completed)
    apply("jordan", "approve_claim", id=completed)
    apply("sam", "reconcile", id=completed, provider_total="129")
    apply("sam", "voucher", id=completed)
    apply("sam", "pay", id=completed)
    apply("sam", "archive", id=completed)

    rejected = trip("alex", "Austin", "Fictional vendor briefing", "2026-12-08")
    apply("jordan", "reject_trip", id=rejected, reason="The exercise can be run remotely.")

    refund = trip("alex", "Denver", "Fictional field study", "2026-12-15")
    apply("jordan", "approve_trip", id=refund)
    apply("alex", "book", id=refund, kind="Rail")
    order = db.execute("SELECT id FROM bookings WHERE trip_id=?", (refund,)).fetchone()[0]
    apply("alex", "cancel_booking", id=refund, booking_id=order, fee="8", reason="Fictional schedule change")

    with db:
        db.execute("UPDATE trips SET purpose='Fictional product workshop' WHERE id=1")
        db.execute("""INSERT INTO policy_checks(actor,action,reason)
                      VALUES ('alex','create','Request exceeds the configured policy cap.')""")


class Sessions:
    """Persist isolated SQLite files for one application process on one volume."""

    def __init__(self, directory=None, ttl=None, capacity=None):
        self.directory = Path(directory if directory is not None else os.getenv("DEMO_DATA_DIR", ROOT / ".demo-data"))
        self.ttl = ttl if ttl is not None else int(os.getenv("DEMO_TTL_HOURS", "24")) * 3600
        self.capacity = capacity if capacity is not None else int(os.getenv("DEMO_MAX_SESSIONS", "250"))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = Lock()
        self.next_sweep = 0

    def fresh(self, path):
        with tempfile.NamedTemporaryFile(prefix=f"{path.stem}-", suffix=".tmp", dir=self.directory, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            with closing(connect(temporary)) as db:
                seed(db)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def open(self, token, create=False):
        with self.lock:
            now = time.time()
            if now >= self.next_sweep:
                for old in self.directory.iterdir():
                    managed = (old.suffix == ".sqlite3" and TOKEN.fullmatch(old.stem)) or TEMPORARY.fullmatch(old.name)
                    if managed and old.is_file() and now - old.stat().st_mtime >= self.ttl:
                        old.unlink()
                self.next_sweep = now + 60
            path = self.directory / f"{token}.sqlite3" if TOKEN.fullmatch(token or "") else None
            if path and path.is_file() and now - path.stat().st_mtime >= self.ttl:
                path.unlink()
            is_new = not path or not path.is_file()
            if is_new:
                if not create:
                    raise PermissionError("Start the live demo before using this action.")
                count = sum(TOKEN.fullmatch(p.stem) is not None for p in self.directory.glob("*.sqlite3"))
                if count >= self.capacity:
                    raise SessionLimit("The demo is at capacity. Please try again later.")
                token = secrets.token_hex(24)
                path = self.directory / f"{token}.sqlite3"
                self.fresh(path)
            else:
                path.touch()
            yield token, path, is_new


def change(db, actor, data):
    if data.get("action") == "workspace_config":
        if ROLES[actor] != "admin":
            raise PermissionError("Admin role required for workspace configuration.")
        name = data.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 45 or not name.isprintable():
            raise ValueError("Enter a workspace name of 1–45 printable characters.")
        with db:
            updated = db.execute("UPDATE demo_session SET name=? WHERE name<>?", (name.strip(), name.strip()))
            if updated.rowcount:
                db.execute("INSERT INTO admin_events(actor,detail) VALUES (?,?)", (actor, "Updated workspace name"))
        return None
    if data.get("action") in POLICY_ACTIONS and ROLES[actor] != "admin":
        raise PermissionError("Admin role required for policy configuration.")
    try:
        with db:
            return mutate(db, actor, data)
    except PolicyViolation as exc:
        with db:
            trip = data.get("id") if data["action"] != "create" else None
            db.execute("""INSERT INTO policy_checks(actor,action,trip_id,reason,request_id)
                          VALUES (?,?,?,?,?) ON CONFLICT(actor,request_id) DO NOTHING""",
                       (actor, data["action"], trip, str(exc), data.get("request_id")))
        raise


def workspace(db, actor):
    state = snapshot(db, actor)
    revision, name = db.execute("SELECT revision,name FROM demo_session").fetchone()
    state.update(actor=actor, roles=ROLES, revision=revision, workspace_name=name)
    trips = state["trips"]
    status = Counter(t["status"] for t in trips)
    monthly = defaultdict(int)
    reimbursements = Counter()
    for trip in trips:
        orders = {booking["kind"] for booking in trip["bookings"]}
        order_states = {booking["state"] for booking in trip["bookings"]}
        available = {
            "book": bool(state["catalog"].keys() - orders),
            "cancel_booking": "Booked" in order_states,
            "amend_booking": "Booked" in order_states,
            "refund_booking": "Refund pending" in order_states,
            "correct_receipt": bool(trip["receipts"]),
            "verify_receipt": bool(trip["receipts"]),
            "submit_claim": bool(orders or trip["receipts"]),
        }
        trip["actions"] = [action for action in trip["actions"] if available.get(action, True)]
        monthly[trip["start"][:7]] += trip["provider_net"] + trip["employee_total"]
        if any(event["detail"] == "Submitted expense claim" for event in trip["events"]):
            reimbursements[trip["status"]] += 1
    checks = [dict(row) for row in db.execute(
        "SELECT action,trip_id,reason,created FROM policy_checks WHERE actor=? OR ? ORDER BY id DESC LIMIT 8",
        (actor, ROLES[actor] != "employee"))]
    pending = ("Pending trip approval", "Pending manager claim review", "Pending finance review")
    state["analytics"] = {
        "requests": len(trips), "pending": sum(status[s] for s in pending),
        "approved": sum(t["policy_snapshot"] is not None for t in trips), "rejected": status["Rejected"],
        "expenses": sum(monthly.values()), "monthly": dict(sorted(monthly.items())),
        "reimbursements": dict(reimbursements), "checks": checks,
        "violations": db.execute("SELECT COUNT(*) FROM policy_checks WHERE actor=? OR ?",
                                 (actor, ROLES[actor] != "employee")).fetchone()[0],
    }
    return state
