"""Policy matching, effective dates, historical snapshots and access boundaries."""
import json
import sqlite3
import unittest

import policies
import workflow as w


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        w.initialize(self.db)

    def tearDown(self):
        self.db.close()

    def act(self, actor, action, **fields):
        with self.db:
            return w.mutate(self.db, actor, dict(id=1, action=action, **fields))

    def publish(self, template_id="STANDARD", start="1900-01-01", end="", flight="500", cap="1500"):
        values = {k: str(v / 100) for k, v in w.DEFAULT_POLICY.items()}
        values.update(Flight=flight, request_cap=cap, template_id=template_id, effective_from=start, effective_until=end)
        self.act("jordan", "policy", **values)

    def frozen(self):
        return self.db.execute("SELECT policy_snapshot FROM trips WHERE id=1").fetchone()[0]

    def test_default_role_matching_and_permissions_are_separate(self):
        self.assertEqual(policies.resolve(self.db, "alex", "2027-01-12")["_policy"]["template_id"], "STANDARD")
        self.assertEqual(policies.resolve(self.db, "casey", "2027-01-12")["_policy"]["template_id"], "FIELD")
        self.act("jordan", "bind_policy_role", employee="alex", role_id="field")
        self.assertEqual(policies.resolve(self.db, "alex", "2027-01-12")["request_cap"], 200000)
        self.assertEqual(w.ROLES["alex"], "employee")
        with self.assertRaises(PermissionError):
            self.act("alex", "approve_trip")

    def test_create_template_role_and_bind(self):
        self.publish("PROJECT", flight="300", cap="1800")
        self.act("jordan", "policy_role", role_id="project", role_name="Project employee", template_id="PROJECT")
        self.act("jordan", "bind_policy_role", employee="casey", role_id="project")
        result = policies.resolve(self.db, "casey", "2027-01-12")
        self.assertEqual(result["Flight"], 30000)
        self.assertEqual(result["_policy"]["role_id"], "project")

    def test_effective_date_boundaries_future_revision_and_expiry(self):
        self.publish(start="2027-02-01", end="2027-02-28", flight="210")
        self.assertEqual(policies.resolve(self.db, "alex", "2027-01-31")["Flight"], 50000)
        self.assertEqual(policies.resolve(self.db, "alex", "2027-02-01")["Flight"], 21000)
        self.assertEqual(policies.resolve(self.db, "alex", "2027-02-28")["Flight"], 21000)
        with self.assertRaises(ValueError):
            policies.resolve(self.db, "alex", "2027-03-01")

    def test_no_version_before_first_effective_date(self):
        self.publish("FUTURE", start="2028-01-01")
        self.act("jordan", "policy_role", role_id="future", role_name="Future employee", template_id="FUTURE")
        self.act("jordan", "bind_policy_role", employee="alex", role_id="future")
        with self.assertRaises(ValueError):
            self.act("jordan", "approve_trip")
        self.assertIsNone(self.frozen())

    def test_invalid_dates_and_unauthorized_changes_rollback(self):
        before = self.db.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0]
        with self.assertRaises(ValueError):
            self.publish(start="2027-03-01", end="2027-02-01")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0], before)
        for actor in ("alex", "casey", "sam"):
            with self.assertRaises(PermissionError):
                self.act(actor, "bind_policy_role", employee="alex", role_id="field")
            with self.assertRaises(PermissionError):
                self.act(actor, "policy_role", role_id="other", role_name="Other", template_id="STANDARD")

    def test_new_approval_uses_latest_matching_policy(self):
        self.publish(start="2027-01-01", flight="200")
        self.act("jordan", "approve_trip")
        self.assertEqual(json.loads(self.frozen())["Flight"], 20000)
        with self.assertRaises(ValueError):
            self.act("alex", "book", kind="Flight")

    def test_history_survives_policy_role_and_binding_changes(self):
        self.act("jordan", "approve_trip")
        original = self.frozen()
        old_version = json.loads(original)["_policy"]["version_id"]
        self.publish(start="2027-01-01", flight="100")
        self.act("jordan", "policy_role", role_id="standard", role_name="Renamed role", template_id="FIELD")
        self.act("jordan", "bind_policy_role", employee="alex", role_id="field")
        w.initialize(self.db)
        self.assertEqual(self.frozen(), original)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM policy_versions WHERE id=?", (old_version,)).fetchone())
        self.act("alex", "book", kind="Flight")

    def test_pending_request_rechecks_reduced_cap_at_approval(self):
        self.publish(start="2027-01-01", cap="500")
        with self.assertRaises(ValueError):
            self.act("jordan", "approve_trip")
        self.assertIsNone(self.frozen())

    def test_claim_agent_uses_owners_existing_snapshot(self):
        self.act("jordan", "approve_trip")
        original = self.frozen()
        self.act("alex", "grant", agent="casey", expires="2099-01-01")
        self.act("casey", "receipt", reference="DEMO-RAIL")
        self.act("casey", "submit_claim")
        self.assertEqual(self.frozen(), original)
        self.assertEqual(json.loads(original)["_policy"]["employee"], "alex")

    def test_missing_binding_does_not_fall_back(self):
        self.db.execute("DELETE FROM employee_policy_roles WHERE employee='alex'")
        self.db.commit()
        with self.assertRaises(ValueError):
            self.act("jordan", "approve_trip")

    def test_flat_legacy_snapshot_is_preserved(self):
        raw = json.dumps(dict(w.DEFAULT_POLICY, Flight=20000))
        self.db.execute("UPDATE trips SET status='Approved',policy_snapshot=?", (raw,))
        self.db.commit()
        self.publish(flight="900")
        w.initialize(self.db)
        self.assertEqual(self.frozen(), raw)
        with self.assertRaises(ValueError):
            self.act("alex", "book", kind="Flight")

    def test_legacy_missing_snapshot_freezes_once_at_migration(self):
        self.db.execute("UPDATE trips SET status='Approved',policy_snapshot=NULL")
        self.db.commit()
        w.initialize(self.db)
        original = self.frozen()
        self.assertEqual(json.loads(original)["_policy"]["source"], "legacy_migration")
        self.publish(flight="100")
        w.initialize(self.db)
        self.assertEqual(self.frozen(), original)
        self.act("alex", "book", kind="Flight")

    def test_same_effective_date_uses_last_published_revision(self):
        self.publish(start="2027-01-01", flight="300")
        self.publish(start="2027-01-01", flight="400")
        result = policies.resolve(self.db, "alex", "2027-01-12")
        self.assertEqual(result["Flight"], 40000)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM policy_versions WHERE template_id='STANDARD' AND effective_from='2027-01-01'").fetchone()[0], 2)

    def publish_controls(self, **extra):
        values = {k: str(v / 100) for k, v in w.DEFAULT_POLICY.items()}
        values.update(extra)
        self.act("jordan", "policy", **values)

    def test_city_hotel_rule_snapshot_and_default_fallback(self):
        self.publish_controls(hotel_city_rules=[{"group": "A", "cities": ["Seattle"], "amount": "120"}])
        self.act("jordan", "approve_trip")
        raw = self.frozen()
        self.publish_controls(hotel_city_rules=[{"group": "A", "cities": ["Seattle"], "amount": "400"}])
        with self.assertRaises(ValueError):
            self.act("alex", "book", kind="Hotel")
        self.assertEqual(self.frozen(), raw)
        rules = json.loads(raw)
        self.assertEqual(policies.booking_terms(rules, "Hotel", "Other city", {})[0], 20000)
        self.assertEqual(policies.booking_terms(rules, "Hotel", " seAttle ", {})[0], 12000)
        # No per-night multiplier is inferred from the source screenshot.
        self.assertEqual(policies.booking_terms(rules, "Hotel", "Seattle", {"nights": 5})[0], 12000)

    def test_cabin_seat_payment_restrictions_and_snapshot(self):
        self.publish_controls(flight_cabins=["Economy"], rail_seats=["Second class"], payment_modes=["Company"])
        self.act("jordan", "approve_trip")
        self.publish_controls(flight_cabins=["Business"], rail_seats=["Business"], payment_modes=["Personal"])
        for kind, values in (("Flight", {"service_class": "Business"}), ("Rail", {"service_class": "Business"}), ("Flight", {"payment_mode": "Personal"})):
            with self.assertRaises(ValueError):
                self.act("alex", "book", kind=kind, **values)
        self.act("alex", "book", kind="Flight", service_class="Economy", payment_mode="Company")
        self.assertEqual(self.db.execute("SELECT service_class,payment_mode FROM bookings").fetchone()[:], ("Economy", "Company"))
        with self.assertRaises(ValueError):
            self.act("alex", "amend_booking", booking_id=1, amount="250", fee="0", reason="Change", service_class="Business")

    def test_duplicate_city_group_rejected_atomically(self):
        before = self.db.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0]
        with self.assertRaises(ValueError):
            self.publish_controls(hotel_city_rules=[{"group": "A", "cities": ["Seattle"], "amount": "230"}, {"group": "B", "cities": ["seattle"], "amount": "180"}])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0], before)

    def test_delete_binding_role_and_batch_templates_preserve_history(self):
        self.act("jordan", "approve_trip")
        old = self.frozen()
        with self.assertRaises(ValueError):
            self.act("jordan", "delete_policy_templates", template_ids="STANDARD")
        self.act("jordan", "delete_policy_role", role_id="standard")
        self.act("jordan", "delete_policy_role", role_id="field")
        self.act("jordan", "delete_policy_templates", template_ids="STANDARD,FIELD")
        self.assertEqual(policies.configuration(self.db, "jordan", True)["templates"], [])
        w.initialize(self.db)
        self.assertEqual(self.frozen(), old)
        self.act("alex", "book", kind="Flight")
        with self.assertRaises(ValueError):
            policies.resolve(self.db, "alex", "2027-01-12")
        with self.assertRaises(ValueError):
            self.publish()

    def test_batch_delete_rolls_back_if_any_template_still_used(self):
        self.publish("UNUSED")
        with self.assertRaises(ValueError):
            self.act("jordan", "delete_policy_templates", template_ids="UNUSED,STANDARD")
        self.assertEqual(self.db.execute("SELECT active FROM policy_templates WHERE id='UNUSED'").fetchone()[0], 1)
        with self.assertRaises(PermissionError):
            self.act("alex", "delete_policy_templates", template_ids="UNUSED")

    def test_personal_payment_settles_to_employee_not_provider(self):
        self.act("jordan", "approve_trip")
        self.act("alex", "book", kind="Flight", payment_mode="Personal")
        self.assertEqual(w.settlement_totals(self.db, 1), (0, 24500))
        self.assertEqual(w.reconciliation(self.db, 1), [])
        self.act("alex", "submit_claim")
        self.act("jordan", "approve_claim")
        self.act("sam", "reconcile")
        self.act("sam", "voucher")
        self.act("sam", "pay")
        self.assertIn('provider=0; employee=24500', self.db.execute("SELECT detail FROM events ORDER BY id DESC").fetchone()[0])

    def test_multiple_policy_roles_can_share_one_template(self):
        self.act("jordan", "policy_role", role_id="additional", role_name="Additional role", template_id="STANDARD")
        self.act("jordan", "bind_policy_role", employee="casey", role_id="additional")
        self.assertEqual(policies.resolve(self.db, "alex", "2027-01-12")["_policy"]["template_id"], policies.resolve(self.db, "casey", "2027-01-12")["_policy"]["template_id"])
        self.act("jordan", "unbind_policy_role", employee="casey")
        with self.assertRaises(ValueError):
            policies.resolve(self.db, "casey", "2027-01-12")
        self.assertEqual(policies.resolve(self.db, "alex", "2027-01-12")["_policy"]["role_id"], "standard")


if __name__ == "__main__":
    unittest.main()
