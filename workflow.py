"""Travel Desk workflow rules and SQLite persistence."""
import json
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import policies
from policies import PolicyViolation

ROOT = Path(__file__).resolve().parent
PEOPLE = {"alex": "Alex Rivera", "casey": "Casey Morgan", "jordan": "Jordan Lee", "sam": "Sam Patel", "taylor": "Taylor Quinn"}
ROLES = {"alex": "employee", "casey": "employee", "jordan": "manager", "sam": "finance", "taylor": "admin"}
POLICY_ACTIONS = frozenset(("policy", "delete_policy_templates", "delete_policy_role", "unbind_policy_role", "policy_role", "bind_policy_role"))
DEPARTMENTS = {"alex": "Product", "casey": "Operations"}
CATALOG = {"Flight": 24500, "Rail": 6800, "Hotel": 12900, "Ground transport": 4700}
RECEIPTS = {"DEMO-RAIL": ("Rail", 6800), "DEMO-HOTEL": ("Hotel", 12900), "DEMO-TAXI": ("Ground transport", 4700)}
# Fictional policy defaults in USD cents.
DEFAULT_POLICY = {"request_cap": 150000, "Flight": 50000, "Rail": 20000, "Hotel": 20000, "Ground transport": 10000}
SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
 id INTEGER PRIMARY KEY, owner TEXT NOT NULL, destination TEXT NOT NULL,
 purpose TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
 budget INTEGER NOT NULL, cost_center TEXT NOT NULL, status TEXT NOT NULL,
 provider_total INTEGER, voucher TEXT, paid_at TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
 id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id),
 kind TEXT NOT NULL, cents INTEGER NOT NULL, UNIQUE(trip_id,kind)
);
CREATE TABLE IF NOT EXISTS receipts (
 id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id),
 reference TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, cents INTEGER NOT NULL,
 verified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id),
 actor TEXT NOT NULL, detail TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), rules TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
 owner TEXT NOT NULL, agent TEXT NOT NULL, expires TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(owner,agent)
);
CREATE TABLE IF NOT EXISTS statement_lines (
 id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id),
 reference TEXT NOT NULL, cents INTEGER NOT NULL, UNIQUE(trip_id,reference)
);
CREATE TABLE IF NOT EXISTS admin_events (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, detail TEXT NOT NULL,
 created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS requests (
 actor TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
 result INTEGER, PRIMARY KEY(actor,request_id)
);
"""


def connect(path=ROOT / "travel.sqlite3"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(db):
    db.executescript(SCHEMA)
    # Existing local databases are upgraded without deleting records.
    additions = {
        "trips": {"policy_snapshot": "TEXT"},
        "bookings": {"state": "TEXT NOT NULL DEFAULT 'Booked'", "refund": "INTEGER NOT NULL DEFAULT 0", "fee": "INTEGER NOT NULL DEFAULT 0", "service_class": "TEXT", "payment_mode": "TEXT", "hotel_group": "TEXT"},
        "receipts": {"issued": "TEXT", "recognition": "TEXT NOT NULL DEFAULT 'Recognized'", "verification": "TEXT NOT NULL DEFAULT 'Pending'", "error": "TEXT NOT NULL DEFAULT ''"},
    }
    for table, columns in additions.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    db.execute("INSERT OR IGNORE INTO policy VALUES(1,?)", (json.dumps(DEFAULT_POLICY),))
    policies.initialize(db, policy(db))
    db.execute("UPDATE receipts SET verification='Verified' WHERE verified=1 AND verification='Pending'")
    if not db.execute("SELECT 1 FROM trips").fetchone():
        cur = db.execute("""INSERT INTO trips(owner,destination,purpose,start,end,budget,cost_center,status)
            VALUES('alex','Seattle','Customer workshop','2027-01-12','2027-01-14',75000,'Engineering','Pending trip approval')""")
        record(db, cur.lastrowid, "alex", "Submitted travel request")
    db.commit()


def setup():
    with connect() as db:
        initialize(db)


def record(db, trip_id, actor, detail):
    db.execute("INSERT INTO events(trip_id,actor,detail) VALUES(?,?,?)", (trip_id, actor, detail))


def required(data, key, length=120):
    value = data[key].strip()
    if not value or len(value) > length:
        raise ValueError(f"{key} must be 1–{length} characters")
    return value


def money(value, allow_zero=False):
    try:
        cents = Decimal(str(value)) * 100
        if not cents.is_finite() or cents != cents.to_integral_value() or not ((0 if allow_zero else 1) <= cents <= 100_000_000):
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid USD amount with up to two decimal places.") from None


def policy(db):
    return json.loads(db.execute("SELECT rules FROM policy WHERE id=1").fetchone()[0])


def total(db, table, trip_id):
    # //新增需求：退款到账后释放预算；供应商净额包含手续费。
    query = {"bookings": "SELECT COALESCE(SUM(cents-refund+fee),0) FROM bookings WHERE trip_id=?",
             "receipts": "SELECT COALESCE(SUM(cents),0) FROM receipts WHERE trip_id=?"}[table]
    return db.execute(query, (trip_id,)).fetchone()[0]


def delegated(db, owner, agent):
    # //新增需求：每次操作重新检查授权有效期和撤销状态；范围仅限票据和提交报销。
    return bool(db.execute("SELECT 1 FROM grants WHERE owner=? AND agent=? AND active=1 AND expires>=?",
                           (owner, agent, date.today().isoformat())).fetchone())


def settlement_totals(db, trip_id):
    # //新增需求：个人支付预订计入员工报销，企业支付计入供应商结算，避免重复付款。
    personal = db.execute("SELECT COALESCE(SUM(cents-refund+fee),0) FROM bookings WHERE trip_id=? AND payment_mode='Personal'", (trip_id,)).fetchone()[0]
    return total(db, "bookings", trip_id) - personal, total(db, "receipts", trip_id) + personal


def actions(trip, role):
    return {
        "employee": {
            "Trip returned": ("resubmit",),
            "Approved": ("book", "cancel_booking", "amend_booking", "receipt", "correct_receipt", "verify_receipt", "submit_claim"),
            "Claim returned": ("cancel_booking", "amend_booking", "receipt", "correct_receipt", "verify_receipt", "submit_claim"),
        },
        "manager": {"Pending trip approval": ("approve_trip", "return_trip", "reject_trip"), "Pending manager claim review": ("approve_claim", "return_claim")},
        "finance": {
            "Approved": ("refund_booking",), "Claim returned": ("refund_booking",),
            "Pending finance review": ("refund_booking", "statement", "reconcile", "return_claim"),
            "Discrepancy": ("refund_booking", "statement", "reconcile", "return_claim"),
            "Reconciled": ("statement", "reconcile", "return_claim", "voucher"),
            "Voucher ready": ("pay",), "Paid": ("archive",),
        },
    }.get(role, {}).get(trip["status"], ())


def reconciliation(db, trip_id):
    expected = {f"B-{r['id']:05d}": r["cents"] - r["refund"] + r["fee"]
                for r in db.execute("SELECT * FROM bookings WHERE trip_id=? AND COALESCE(payment_mode,'Company')<>'Personal'", (trip_id,))}
    actual = {r["reference"]: r["cents"] for r in db.execute("SELECT * FROM statement_lines WHERE trip_id=?", (trip_id,))}
    differences = []
    for ref in sorted(expected.keys() | actual.keys()):
        if ref not in actual:
            differences.append(f"Missing statement line: {ref}")
        elif ref not in expected:
            differences.append(f"Unknown order: {ref}")
        elif expected[ref] != actual[ref]:
            differences.append(f"Amount mismatch: {ref} (expected {expected[ref]/100:.2f}, statement {actual[ref]/100:.2f})")
    # //新增需求：未完成退款不能出凭证，防止账面结清后仍有未决资金。
    if db.execute("SELECT 1 FROM bookings WHERE trip_id=? AND state='Refund pending'", (trip_id,)).fetchone():
        differences.append("Refund pending")
    return differences


def dashboard(db, actor):
    result = []
    for row in db.execute("SELECT * FROM trips ORDER BY id DESC"):
        is_owner = row["owner"] == actor
        proxy = ROLES[actor] == "employee" and not is_owner and delegated(db, row["owner"], actor)
        if ROLES[actor] == "employee" and not is_owner and not proxy:
            continue
        trip = dict(row)
        for table in ("bookings", "receipts", "events", "statement_lines"):
            trip[table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE trip_id=? ORDER BY id", (row["id"],))]
        allowed = actions(row, ROLES[actor])
        trip["actions"] = [a for a in allowed if not proxy or a in ("receipt", "correct_receipt", "verify_receipt", "submit_claim")]
        trip["delegated"] = proxy
        trip["provider_net"], trip["employee_total"] = settlement_totals(db, row["id"])
        trip["differences"] = reconciliation(db, row["id"])
        result.append(trip)
    return result


def snapshot(db, actor):
    trips = dashboard(db, actor)
    groups = {key: {} for key in ("employee", "department", "cost_center")}
    for t in trips:
        for key, label in (("employee", PEOPLE[t["owner"]]), ("department", DEPARTMENTS[t["owner"]]), ("cost_center", t["cost_center"])):
            row = groups[key].setdefault(label, {"trips": 0, "provider": 0, "employee": 0})
            row["trips"] += 1
            row["provider"] += t["provider_net"]
            row["employee"] += t["employee_total"]
    return {"trips": trips, "people": PEOPLE, "catalog": CATALOG, "policy": policy(db), "reports": groups,
            "policy_config": policies.configuration(db, actor, ROLES[actor] in ("manager", "admin")),
            "grants": [dict(r) for r in db.execute("SELECT * FROM grants WHERE owner=? OR ?='manager'", (actor, ROLES[actor]))],
            "admin_events": [dict(r) for r in db.execute("SELECT * FROM admin_events ORDER BY id DESC LIMIT 30")] if ROLES[actor] in ("manager", "admin") else []}


def mutate(db, actor, data):
    # //新增需求：相同操作重试返回原结果，防止重复预订、重复手续费或退款。
    db.execute("BEGIN IMMEDIATE")
    request_id = data.get("request_id")
    payload = json.dumps(data, sort_keys=True)
    if request_id is not None:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("Invalid request identifier.")
        previous = db.execute("SELECT * FROM requests WHERE actor=? AND request_id=?", (actor, request_id)).fetchone()
        if previous:
            if previous["payload"] != payload:
                raise ValueError("Request identifier was already used for different fields.")
            return previous["result"]
    result = _mutate(db, actor, data)
    if request_id is not None:
        db.execute("INSERT INTO requests VALUES(?,?,?,?)", (actor, request_id, payload, result))
    return result


def _mutate(db, actor, data):
    role, action = ROLES[actor], data["action"]
    if action == "policy":
        if role not in ("manager", "admin"):
            raise PermissionError("Policy administrator required.")
        rules = {key: money(data[key]) for key in DEFAULT_POLICY}
        template_id, version_id = policies.publish(db, data, rules, money)
        if template_id == "STANDARD":
            db.execute("UPDATE policy SET rules=? WHERE id=1", (json.dumps(rules),))
        db.execute("INSERT INTO admin_events(actor,detail) VALUES(?,?)", (actor, f"Published policy {template_id} version {version_id}: " + json.dumps(rules)))
        return None
    if action in ("delete_policy_templates", "delete_policy_role", "unbind_policy_role"):
        if role not in ("manager", "admin"):
            raise PermissionError("Policy administrator required.")
        if action == "delete_policy_templates":
            policies.delete_templates(db, data["template_ids"])
        elif action == "delete_policy_role":
            role_id = policies.identifier(data["role_id"])
            if not db.execute("SELECT 1 FROM policy_roles WHERE id=?", (role_id,)).fetchone():
                raise ValueError("Policy role not found.")
            # //新增需求：删除方案先解除员工绑定；未审批新单须重新配置，历史快照不变。
            db.execute("DELETE FROM employee_policy_roles WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM policy_roles WHERE id=?", (role_id,))
        else:
            if ROLES.get(data["employee"]) != "employee":
                raise ValueError("Choose an employee account.")
            db.execute("DELETE FROM employee_policy_roles WHERE employee=?", (data["employee"],))
        db.execute("INSERT INTO admin_events(actor,detail) VALUES(?,?)", (actor, action + ": " + json.dumps(data, sort_keys=True)))
        return None
    if action in ("policy_role", "bind_policy_role"):
        if role not in ("manager", "admin"):
            raise PermissionError("Policy administrator required.")
        role_id = policies.identifier(data["role_id"])
        if action == "policy_role":
            template_id = data["template_id"]
            if not db.execute("SELECT 1 FROM policy_templates WHERE id=? AND active=1", (template_id,)).fetchone():
                raise ValueError("Policy template not found.")
            name = required(data, "role_name")
            db.execute("INSERT INTO policy_roles VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,template_id=excluded.template_id", (role_id, name, template_id))
            detail = f"Policy role {role_id} -> {template_id}"
        else:
            employee = data["employee"]
            if ROLES.get(employee) != "employee":
                raise ValueError("Choose an employee account.")
            if not db.execute("SELECT 1 FROM policy_roles WHERE id=?", (role_id,)).fetchone():
                raise ValueError("Policy role not found.")
            db.execute("INSERT INTO employee_policy_roles VALUES(?,?) ON CONFLICT(employee) DO UPDATE SET role_id=excluded.role_id", (employee, role_id))
            detail = f"Bound employee {employee} -> policy role {role_id}"
        db.execute("INSERT INTO admin_events(actor,detail) VALUES(?,?)", (actor, detail))
        return None
    if action in ("grant", "revoke"):
        if role != "employee":
            raise PermissionError("Employee role required.")
        agent = data["agent"]
        if agent == actor or ROLES.get(agent) != "employee":
            raise ValueError("Choose another employee.")
        if action == "grant":
            expires = date.fromisoformat(data["expires"])
            if expires < date.today():
                raise ValueError("Authorization has already expired.")
            db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,1)", (actor, agent, expires.isoformat()))
        else:
            db.execute("UPDATE grants SET active=0 WHERE owner=? AND agent=?", (actor, agent))
        db.execute("INSERT INTO admin_events(actor,detail) VALUES(?,?)", (actor, f"{action}: {agent}; scope=receipts,claim submission"))
        return None
    if action in ("create", "resubmit"):
        if role != "employee":
            raise PermissionError("Employee role required.")
        start, end = (date.fromisoformat(data[k]) for k in ("start", "end"))
        if start > end:
            raise ValueError("End date must be on or after start date.")
        budget = money(data["budget"])
        if budget > policies.resolve(db, actor, start.isoformat())["request_cap"]:
            raise PolicyViolation("Request exceeds the configured policy cap.")
        center = data["cost_center"]
        if center not in ("Engineering", "Operations", "Sales"):
            raise ValueError("Choose a cost center.")
        fields = (required(data, "destination"), required(data, "purpose", 250), start.isoformat(), end.isoformat(), budget, center)
        if action == "create":
            cur = db.execute("INSERT INTO trips(owner,destination,purpose,start,end,budget,cost_center,status) VALUES(?,?,?,?,?,?,?,'Pending trip approval')", (actor, *fields))
            trip_id = cur.lastrowid
        else:
            trip_id = int(data["id"])
            cur = db.execute("UPDATE trips SET destination=?,purpose=?,start=?,end=?,budget=?,cost_center=?,status='Pending trip approval' WHERE id=? AND owner=? AND status='Trip returned'", (*fields, trip_id, actor))
            if not cur.rowcount:
                raise PermissionError("This trip is unavailable for resubmission.")
        record(db, trip_id, actor, "Submitted travel request")
        return trip_id

    trip_id = int(data["id"])
    trip = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not trip or action not in actions(trip, role):
        raise PermissionError("This action is unavailable for this role or trip status.")
    if role == "employee" and trip["owner"] != actor:
        if action not in ("receipt", "correct_receipt", "verify_receipt", "submit_claim") or not delegated(db, trip["owner"], actor):
            raise PermissionError("No active authorization for this action.")
    # //新增需求：本人和代办提交人不得审批自己的单据。
    if action in ("approve_trip", "approve_claim") and (trip["owner"] == actor or db.execute("SELECT 1 FROM events WHERE trip_id=? AND actor=? AND detail='Submitted expense claim'", (trip_id, actor)).fetchone()):
        raise PermissionError("You cannot approve your own submission.")

    if action in ("book", "amend_booking"):
        # //新增需求：已审批单据缺少快照时拒绝预订，避免悄悄使用更新后的政策。
        if not trip["policy_snapshot"]:
            raise ValueError("Approved trip has no policy snapshot; policy review is required.")
        rules = json.loads(trip["policy_snapshot"])
        if action == "book":
            kind = data["kind"]
            if kind not in CATALOG:
                raise ValueError("Choose a booking category.")
            cents, previous = CATALOG[kind], 0
        else:
            booking = booking_row(db, trip_id, data)
            if booking["state"] != "Booked":
                raise ValueError("Only an active booking can be amended.")
            kind, cents, previous = booking["kind"], money(data["amount"]), booking["cents"] + booking["fee"]
        # //新增需求：历史改签费不覆盖；本次手续费追加到订单费用。
        fee = booking["fee"] + money(data.get("fee", "0"), True) if action == "amend_booking" else 0
        nights = 1
        if kind == "Hotel" and rules.get("_controls", {}).get("hotel_limit_basis") == "night":
            nights = max(1, (date.fromisoformat(trip["end"]) - date.fromisoformat(trip["start"])).days)
        limit, terms = policies.booking_terms(rules, kind, trip["destination"], data,
                                            dict(booking) if action == "amend_booking" else None, nights)
        if cents > limit:
            raise PolicyViolation(f"Booking exceeds the category policy limit for {kind} (USD {limit/100:,.2f}).")
        if total(db, "bookings", trip_id) + total(db, "receipts", trip_id) - previous + cents + fee > trip["budget"]:
            raise ValueError("Booking exceeds the approved budget.")
        if action == "book":
            cur = db.execute("INSERT INTO bookings(trip_id,kind,cents,service_class,payment_mode,hotel_group) VALUES(?,?,?,?,?,?)", (trip_id, kind, cents, terms.get("service_class"), terms.get("payment_mode"), terms.get("hotel_group")))
            detail = f"Booked {kind}; order B-{cur.lastrowid:05d}"
        else:
            db.execute("UPDATE bookings SET cents=?,fee=?,service_class=?,payment_mode=?,hotel_group=? WHERE id=?", (cents, fee, terms.get("service_class"), terms.get("payment_mode"), terms.get("hotel_group"), booking["id"]))
            detail = f"Amended B-{booking['id']:05d}; previous total={previous}; new total={cents+fee}; " + required(data, "reason", 250)
        record(db, trip_id, actor, detail)
        return trip_id

    if action in ("cancel_booking", "refund_booking"):
        booking = booking_row(db, trip_id, data)
        if action == "cancel_booking":
            if booking["state"] != "Booked":
                raise ValueError("Cancellation already requested.")
            fee = money(data["fee"], True)
            # //新增需求：保留原订单和已发生改签费，取消费不得超过票价。
            if fee > booking["cents"]:
                raise ValueError("Cancellation fee exceeds the booking amount.")
            db.execute("UPDATE bookings SET state='Refund pending',fee=fee+? WHERE id=?", (fee, booking["id"]))
            # //新增需求：在退款到账前，新增手续费也不能突破预算。
            if total(db, "bookings", trip_id) + total(db, "receipts", trip_id) > trip["budget"]:
                raise ValueError("Cancellation fee exceeds remaining budget; return for review.")
            detail = f"Cancellation requested B-{booking['id']:05d}; fee={fee}; " + required(data, "reason", 250)
        else:
            if booking["state"] != "Refund pending":
                raise ValueError("No pending refund for this booking.")
            refund = money(data["amount"], True)
            remaining = booking["cents"] - booking["refund"]
            if refund > remaining:
                raise ValueError("Refund exceeds remaining refundable amount.")
            final = data.get("final", "yes") == "yes"
            # //新增需求：部分退款可分次到账；关闭剩余退款需要填写原因并保留记录。
            reason = required(data, "reason", 250)
            db.execute("UPDATE bookings SET refund=refund+?,state=? WHERE id=?", (refund, "Cancelled" if final else "Refund pending", booking["id"]))
            detail = f"Refund received B-{booking['id']:05d}; amount={refund}; final={final}; {reason}"
        record(db, trip_id, actor, detail)
        return trip_id

    if action in ("receipt", "correct_receipt"):
        reference = required(data, "reference").upper()
        issued = data.get("issued", trip["start"])
        error = ""
        if reference in RECEIPTS:
            kind, cents = RECEIPTS[reference]
        else:
            kind = data.get("kind", "").strip()
            try:
                cents = money(data.get("amount", ""))
            except ValueError:
                cents, error = 0, "Missing or invalid amount"
            if not kind:
                error = "Missing category"
        try:
            receipt_date = date.fromisoformat(issued)
            # //新增需求：票据日期需处于出差区间；日期错误留在待修正状态。
            if not date.fromisoformat(trip["start"]) <= receipt_date <= date.fromisoformat(trip["end"]):
                error = "Receipt date is outside the trip"
        except (ValueError, TypeError):
            error = "Invalid receipt date"
        # //新增需求：平台订单编号不能再次作为员工垫付票据。
        if reference.startswith("B-"):
            raise ValueError("Company-paid orders cannot be reimbursed as employee receipts.")
        old = None
        if action == "correct_receipt":
            old = db.execute("SELECT * FROM receipts WHERE id=? AND trip_id=?", (int(data["receipt_id"]), trip_id)).fetchone()
            if not old:
                raise ValueError("Receipt not found.")
        if total(db, "bookings", trip_id) + total(db, "receipts", trip_id) - (old["cents"] if old else 0) + cents > trip["budget"]:
            raise ValueError("Receipt exceeds the approved budget.")
        recognized = "Needs correction" if error else "Recognized"
        # //新增需求：人工修正后旧验真结果失效；提交前必须重新验真。
        verified = int(reference in RECEIPTS and not error)
        verification = "Verified" if verified else "Pending"
        fields = (reference, kind or "Unknown", cents, verified, issued, recognized, verification, error)
        if old:
            db.execute("UPDATE receipts SET reference=?,kind=?,cents=?,verified=?,issued=?,recognition=?,verification=?,error=? WHERE id=?", (*fields, old["id"]))
        else:
            db.execute("INSERT INTO receipts(reference,kind,cents,verified,issued,recognition,verification,error,trip_id) VALUES(?,?,?,?,?,?,?,?,?)", (*fields, trip_id))
        record(db, trip_id, actor, f"{action}: {reference}; {recognized}; {error}")
        return trip_id
    if action == "verify_receipt":
        receipt = db.execute("SELECT * FROM receipts WHERE id=? AND trip_id=?", (int(data["receipt_id"]), trip_id)).fetchone()
        if not receipt or receipt["recognition"] != "Recognized":
            raise ValueError("Correct receipt fields before verification.")
        verified = not receipt["reference"].startswith("FAIL-")
        db.execute("UPDATE receipts SET verified=?,verification=?,error=? WHERE id=?", (int(verified), "Verified" if verified else "Failed", "" if verified else "Verification fixture rejected this reference", receipt["id"]))
        record(db, trip_id, actor, f"Receipt {receipt['reference']}: verification {'passed' if verified else 'failed'}")
        return trip_id

    if action == "statement":
        # Independent supplier input: reference,amount per line. Never silently copied from orders.
        raw = required(data, "lines", 12000)
        lines = []
        for line in raw.splitlines():
            ref, amount = line.split(",")
            ref = ref.strip().upper()
            if not ref or len(ref) > 120:
                raise ValueError("Invalid statement reference.")
            lines.append((trip_id, ref, money(amount.strip(), True)))
        reason = required(data, "reason", 250)
        db.execute("DELETE FROM statement_lines WHERE trip_id=?", (trip_id,))
        db.executemany("INSERT INTO statement_lines(trip_id,reference,cents) VALUES(?,?,?)", lines)
        db.execute("UPDATE trips SET status='Pending finance review',provider_total=NULL WHERE id=?", (trip_id,))
        record(db, trip_id, actor, f"Imported statement {lines}; reason: {reason}")
        return trip_id
    if action == "reconcile":
        bookings = db.execute("SELECT * FROM bookings WHERE trip_id=? AND COALESCE(payment_mode,'Company')<>'Personal'", (trip_id,)).fetchall()
        # Compatibility with the original single-order walkthrough, without accepting aggregate-only matching.
        if "provider_total" in data:
            if len(bookings) != 1 or db.execute("SELECT 1 FROM statement_lines WHERE trip_id=?", (trip_id,)).fetchone():
                raise ValueError("Import individual statement lines before reconciling.")
            db.execute("INSERT INTO statement_lines(trip_id,reference,cents) VALUES(?,?,?)", (trip_id, f"B-{bookings[0]['id']:05d}", money(data["provider_total"], True)))
        differences = reconciliation(db, trip_id)
        statement = db.execute("SELECT COALESCE(SUM(cents),0) FROM statement_lines WHERE trip_id=?", (trip_id,)).fetchone()[0]
        status = "Discrepancy" if differences else "Reconciled"
        db.execute("UPDATE trips SET status=?,provider_total=? WHERE id=?", (status, statement, trip_id))
        record(db, trip_id, actor, status + ": " + ("; ".join(differences) or "All statement lines matched"))
        return trip_id

    transitions = {"approve_trip": ("Approved", "Approved travel request"), "return_trip": ("Trip returned", "Returned travel request"),
                   "reject_trip": ("Rejected", "Rejected travel request"),
                   "submit_claim": ("Pending manager claim review", "Submitted expense claim"), "approve_claim": ("Pending finance review", "Approved expense claim"),
                   "return_claim": ("Claim returned", "Returned expense claim"), "voucher": ("Voucher ready", "Generated accounting voucher"),
                   "pay": ("Paid", "Recorded settlement and employee reimbursement"), "archive": ("Archived", "Archived completed trip")}
    status, detail = transitions[action]
    if action == "approve_trip":
        # //新增需求：审批保存政策快照，后续政策调整不追溯影响已审批单据。
        approved_rules = policies.resolve(db, trip["owner"], trip["start"])
        if trip["budget"] > approved_rules["request_cap"]:
            raise PolicyViolation("Request exceeds current policy; return it for revision.")
        db.execute("UPDATE trips SET policy_snapshot=? WHERE id=?", (json.dumps(approved_rules), trip_id))
    if action == "submit_claim":
        if not db.execute("SELECT 1 FROM bookings WHERE trip_id=? UNION ALL SELECT 1 FROM receipts WHERE trip_id=?", (trip_id, trip_id)).fetchone():
            raise ValueError("Add a booking or receipt first.")
        # //新增需求：存在待修正、未验真或验真失败的票据，不允许提交报销。
        if db.execute("SELECT 1 FROM receipts WHERE trip_id=? AND (verified=0 OR recognition<>'Recognized')", (trip_id,)).fetchone():
            raise ValueError("Correct and verify every receipt before submitting.")
    if action in ("return_trip", "return_claim", "reject_trip"):
        detail += ": " + required(data, "reason", 250)
        db.execute("UPDATE trips SET provider_total=NULL WHERE id=?", (trip_id,))
    if action == "voucher":
        if reconciliation(db, trip_id):
            raise ValueError("Resolve all statement and refund differences first.")
        db.execute("UPDATE trips SET voucher=? WHERE id=?", (f"V-{trip_id:05d}", trip_id))
    if action == "pay":
        # //新增需求：失败可重试；成功后状态锁定，不能重复付款。
        if data.get("outcome", "success") == "failure":
            record(db, trip_id, actor, "Payment failed; retry available")
            return trip_id
        db.execute("UPDATE trips SET paid_at=CURRENT_TIMESTAMP WHERE id=? AND paid_at IS NULL", (trip_id,))
        provider, employee = settlement_totals(db, trip_id)
        detail += f"; provider={provider}; employee={employee}"
    # //新增需求：仅已付款单据可归档，归档后的动作表为空，禁止继续修改。
    if action == "archive" and not trip["paid_at"]:
        raise ValueError("Payment must complete before archival.")
    db.execute("UPDATE trips SET status=? WHERE id=?", (status, trip_id))
    record(db, trip_id, actor, detail)
    return trip_id


def booking_row(db, trip_id, data):
    booking = db.execute("SELECT * FROM bookings WHERE id=? AND trip_id=?", (int(data["booking_id"]), trip_id)).fetchone()
    if not booking:
        raise ValueError("Booking not found.")
    return booking
