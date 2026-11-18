"""Versioned travel policies, independent of application access roles."""
import json
import re
from datetime import date

FLIGHT_CABINS = ("Economy", "Premium economy", "Business")
RAIL_SEATS = ("Second class", "First class", "Business", "Soft seat / sleeper", "Hard seat / sleeper")
PAYMENT_MODES = ("Company", "Personal")


class PolicyViolation(ValueError):
    """A requested operation conflicts with the applicable travel policy."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_templates (
 id TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
 id INTEGER PRIMARY KEY, template_id TEXT NOT NULL REFERENCES policy_templates(id),
 effective_from TEXT NOT NULL, effective_until TEXT, rules TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_roles (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 template_id TEXT NOT NULL REFERENCES policy_templates(id)
);
CREATE TABLE IF NOT EXISTS employee_policy_roles (
 employee TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES policy_roles(id)
);
"""


def initialize(db, legacy_rules):
    db.executescript(SCHEMA)
    if "active" not in {r[1] for r in db.execute("PRAGMA table_info(policy_templates)")}:
        db.execute("ALTER TABLE policy_templates ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if not db.execute("SELECT 1 FROM policy_templates").fetchone():
        # //新增需求：多套模板和默认绑定仅用于演示；额度为虚构值。
        flexible = dict(legacy_rules, request_cap=200000, Flight=70000, Hotel=30000)
        for key, name, rules in (("STANDARD", "Standard travel", legacy_rules), ("FIELD", "Field travel", flexible)):
            # //新增需求：内置酒店城市组、额度与城市清单仅为虚构演示配置，不复制公司参数。
            rules = dict(rules, _controls={"hotel_city_rules": [{"group": "Group A", "cities": ["Seattle", "Portland"], "limit": 25000 if key == "STANDARD" else 35000}],
                        "flight_cabins": ["Economy"], "rail_seats": ["Second class"], "payment_modes": ["Company", "Personal"]})
            db.execute("INSERT INTO policy_templates(id,name) VALUES(?,?)", (key, name))
            db.execute("INSERT INTO policy_versions(template_id,effective_from,rules) VALUES(?,'1900-01-01',?)", (key, json.dumps(rules)))
        db.executemany("INSERT INTO policy_roles VALUES(?,?,?)", (("standard", "Standard employee", "STANDARD"), ("field", "Field employee", "FIELD")))
        db.executemany("INSERT INTO employee_policy_roles VALUES(?,?)", (("alex", "standard"), ("casey", "field")))
    # //新增需求：无历史快照的旧审批单冻结迁移时的旧全局额度，不推断原始审批规则。
    frozen = dict(legacy_rules, _policy={"source": "legacy_migration", "note": "Original approval policy unavailable; frozen at migration"})
    db.execute("UPDATE trips SET policy_snapshot=? WHERE policy_snapshot IS NULL AND status NOT IN ('Pending trip approval','Trip returned','Rejected')", (json.dumps(frozen),))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", value):
        raise ValueError("Use a policy identifier of 1–40 letters, numbers, underscores or hyphens.")
    return value


def select_version(db, template_id, on_date):
    on_date = date.fromisoformat(on_date).isoformat()
    # //新增需求：按行程开始日选择版本；同日起效取最后发布版本；结束日包含当天。
    # A newer expired revision blocks fallback to an older version, exposing configuration gaps.
    row = db.execute("SELECT * FROM policy_versions WHERE template_id=? AND effective_from<=? ORDER BY effective_from DESC,id DESC LIMIT 1", (template_id, on_date)).fetchone()
    if not row or (row["effective_until"] and row["effective_until"] < on_date):
        raise ValueError("No effective travel policy for this trip start date.")
    return row


def resolve(db, employee, start):
    row = db.execute("""SELECT r.id role_id,r.name role_name,t.id template_id,t.name template_name
        FROM employee_policy_roles b JOIN policy_roles r ON r.id=b.role_id
        JOIN policy_templates t ON t.id=r.template_id WHERE b.employee=? AND t.active=1""", (employee,)).fetchone()
    # //新增需求：未绑定或没有有效版本时拒绝申请/审批，不自动套用其他员工政策。
    if not row:
        raise ValueError("Employee has no travel policy role binding.")
    version = select_version(db, row["template_id"], start)
    rules = json.loads(version["rules"])
    rules["_policy"] = {**dict(row), "version_id": version["id"], "effective_from": version["effective_from"],
                        "effective_until": version["effective_until"], "basis": "trip_start", "date": start, "employee": employee}
    return rules


def publish(db, data, rules, parse_money):
    template_id = identifier(data.get("template_id", "STANDARD"))
    existing = db.execute("SELECT name,active FROM policy_templates WHERE id=?", (template_id,)).fetchone()
    if existing and not existing["active"]:
        raise ValueError("Deleted template identifiers cannot be reused.")
    name = data.get("template_name", existing["name"] if existing else template_id).strip()
    if not name or len(name) > 120:
        raise ValueError("Template name must be 1–120 characters.")
    start = date.fromisoformat(data.get("effective_from", "1900-01-01")).isoformat()
    end = data.get("effective_until") or None
    if end:
        end = date.fromisoformat(end).isoformat()
        if end < start:
            raise ValueError("Policy end date must be on or after its start date.")
    rules = dict(rules, _controls=validate_controls(data, parse_money))
    db.execute("INSERT INTO policy_templates(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (template_id, name))
    # //新增需求：发布追加版本，不覆盖旧版本；已审批快照不参与更新。
    cur = db.execute("INSERT INTO policy_versions(template_id,effective_from,effective_until,rules) VALUES(?,?,?,?)", (template_id, start, end, json.dumps(rules)))
    return template_id, cur.lastrowid


def configuration(db, actor, is_manager):
    versions = [dict(r) for r in db.execute("SELECT * FROM policy_versions ORDER BY effective_from,id")]
    for version in versions:
        version["rules"] = json.loads(version["rules"])
    return {"templates": [dict(r) for r in db.execute("SELECT * FROM policy_templates WHERE active=1 ORDER BY id")],
            "versions": versions, "roles": [dict(r) for r in db.execute("SELECT * FROM policy_roles ORDER BY id")],
            "bindings": [dict(r) for r in db.execute("SELECT * FROM employee_policy_roles WHERE employee=? OR ?", (actor, int(is_manager)))]}


def validate_controls(data, parse_money):
    """Validate fictional city tiers, travel classes, and payment options."""
    controls = {}
    for key, choices, defaults in (("flight_cabins", FLIGHT_CABINS, ["Economy"]), ("rail_seats", RAIL_SEATS, ["Second class"]), ("payment_modes", PAYMENT_MODES, ["Company", "Personal"])):
        values = data.get(key, defaults)
        if isinstance(values, str):
            values = [v.strip() for v in values.split(",") if v.strip()]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or v not in choices for v in values):
            raise ValueError(f"Choose supported values for {key}.")
        controls[key] = list(dict.fromkeys(values))
    entries = data.get("hotel_city_rules", [])
    if isinstance(entries, str):
        entries = json.loads(entries or "[]")
    if not isinstance(entries, list) or len(entries) > 40:
        raise ValueError("Hotel city rules must be a list of up to 40 groups.")
    city_rules, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid hotel city group.")
        group = entry.get("group", "").strip()
        cities = entry.get("cities")
        if not group or len(group) > 80 or not isinstance(cities, list) or not cities or len(cities) > 100:
            raise ValueError("Each hotel group needs a name and a nonempty city list.")
        clean = []
        for city in cities:
            if not isinstance(city, str) or not city.strip() or len(city.strip()) > 120:
                raise ValueError("Invalid hotel city name.")
            city = city.strip()
            # //新增需求：城市名称忽略大小写且不可重复归组，避免优先级不明确。
            if city.casefold() in seen:
                raise ValueError("A city cannot appear in multiple groups.")
            seen.add(city.casefold())
            clean.append(city)
        city_rules.append({"group": group, "cities": clean, "limit": parse_money(entry["amount"])})
    controls["hotel_city_rules"] = city_rules
    hotel_basis = data.get("hotel_limit_basis", "booking")
    if hotel_basis not in ("booking", "night"):
        raise ValueError("Choose a per-booking or per-night hotel limit.")
    if hotel_basis == "night":
        controls["hotel_limit_basis"] = hotel_basis
    return controls


def booking_terms(rules, kind, destination, data, previous=None, nights=1):
    controls = rules.get("_controls")
    if not controls:
        # Old snapshots predate these fields: preserve their original amount-only behavior.
        return rules[kind], {}
    previous = previous or {}
    payment = data.get("payment_mode", previous.get("payment_mode") or controls["payment_modes"][0])
    if payment not in controls["payment_modes"]:
        raise PolicyViolation("Payment mode is not allowed by the approved policy.")
    service_class = data.get("service_class", previous.get("service_class") or ("Economy" if kind == "Flight" else "Second class" if kind == "Rail" else ""))
    if kind in ("Flight", "Rail"):
        key = "flight_cabins" if kind == "Flight" else "rail_seats"
        if service_class not in controls[key]:
            raise PolicyViolation("Cabin or seat is not allowed by the approved policy.")
    limit, group = rules[kind], "Default"
    if kind == "Hotel":
        # Match the destination against city tiers, then apply the snapshotted basis.
        for row in controls["hotel_city_rules"]:
            if destination.strip().casefold() in {city.casefold() for city in row["cities"]}:
                limit, group = row["limit"], row["group"]
                break
        if controls.get("hotel_limit_basis") == "night":
            limit *= nights
    return limit, {"service_class": service_class, "payment_mode": payment, "hotel_group": group if kind == "Hotel" else ""}


def delete_templates(db, identifiers):
    """Delete one or more templates while retaining historical versions."""
    if isinstance(identifiers, str):
        identifiers = [v.strip() for v in identifiers.split(",") if v.strip()]
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError("Choose at least one policy template.")
    for key in identifiers:
        identifier(key)
        if not db.execute("SELECT 1 FROM policy_templates WHERE id=? AND active=1", (key,)).fetchone():
            raise ValueError("Policy template not found.")
        # //新增需求：仍被角色引用的模板不能删除；删除采用停用，历史版本及快照保留。
        if db.execute("SELECT 1 FROM policy_roles WHERE template_id=?", (key,)).fetchone():
            raise ValueError("Remove or reassign policy roles before deleting their template.")
        db.execute("UPDATE policy_templates SET active=0 WHERE id=?", (key,))
