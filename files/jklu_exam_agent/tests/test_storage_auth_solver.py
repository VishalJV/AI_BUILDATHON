import os
import tempfile
import unittest

from helpers import make_state
import auth
import solver
import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        storage.DB_PATH = os.path.join(self.tmp, "t.db")
        storage._use_mem = False

    def test_state_roundtrip_keeps_tuples_and_sets(self):
        state = make_state()
        state["leave"] = {state["faculty"][0]: {"Monday"}}
        storage.save_snapshot(state, state, "PUBLISHED", 3, "15 Nov 2026")
        snap = storage.load_snapshot()
        self.assertEqual(snap["state"]["plan"], state["plan"])               # tuples preserved
        self.assertEqual(snap["state"]["leave"], state["leave"])             # sets preserved
        self.assertEqual((snap["status"], snap["version"], snap["published_at"]), ("PUBLISHED", 3, "15 Nov 2026"))

    def test_audit_log_is_persistent_and_ordered(self):
        for i in range(3):
            storage.append_audit("Admin", "ACTION", f"step {i}")
        rows = storage.load_audit()
        self.assertEqual([r["Detail"] for r in rows], ["step 0", "step 1", "step 2"])

    def test_lockout_after_repeated_failures(self):
        for _ in range(5):
            storage.record_failure("student:x")
        self.assertTrue(storage.is_locked("student:x", 5))
        storage.clear_failures("student:x")
        self.assertFalse(storage.is_locked("student:x", 5))

    def test_secret_is_stable(self):
        os.environ.pop("APP_SECRET", None)
        self.assertEqual(storage.get_secret(), storage.get_secret())

    def test_snapshot_signature_changes_on_save(self):
        state = make_state()
        s1 = storage.save_snapshot(state, None, "DRAFT", 1)
        s2 = storage.save_snapshot(state, None, "PUBLISHED", 1)
        self.assertNotEqual(s1, s2)
        self.assertEqual(storage.snapshot_sig(), s2)


class AuthTests(unittest.TestCase):
    def test_codes_are_stable_case_insensitive_and_secret_dependent(self):
        c = auth.make_code("secret", "student", "2026BTech001")
        self.assertEqual(len(c), auth.CODE_LEN)
        self.assertTrue(auth.check_code("secret", "student", "2026btech001", c.lower()))
        self.assertFalse(auth.check_code("other-secret", "student", "2026BTech001", c))
        self.assertFalse(auth.check_code("secret", "student", "2026BTech002", c))
        self.assertFalse(auth.check_code("secret", "faculty", "2026BTech001", c))

    def test_pin_and_faculty_ids(self):
        self.assertTrue(auth.check_pin("abc", "abc"))
        self.assertFalse(auth.check_pin("abc", "abd"))
        self.assertEqual(auth.faculty_by_id(" fac002 ", ["A", "B"]), "B")
        self.assertIsNone(auth.faculty_by_id("FAC009", ["A", "B"]))


class SolverTests(unittest.TestCase):
    def test_plan_verifier_accepts_good_and_rejects_bad_plans(self):
        rolls = {"A": {"1", "2"}, "B": {"2", "3"}, "C": {"4"}}
        sizes = {k: len(v) for k, v in rolls.items()}
        slot1, slot2 = ("Monday", "Morning"), ("Monday", "Afternoon")
        self.assertTrue(solver.plan_is_valid({"A": slot1, "B": slot2, "C": slot1}, rolls, sizes, 10, 3)[0])
        self.assertFalse(solver.plan_is_valid({"A": slot1, "B": slot1}, rolls, sizes, 10, 3)[0])            # shared student
        self.assertFalse(solver.plan_is_valid({"A": slot1, "C": slot1}, rolls, sizes, 2, 3)[0])             # capacity
        self.assertFalse(solver.plan_is_valid({"A": slot1, "C": slot1}, rolls, sizes, 10, 1)[0])            # courses per slot

    def test_cp_sat_plan_is_valid_when_ortools_is_installed(self):
        if not solver.solver_available():
            self.skipTest("ortools not installed (pip install ortools)")
        import engine
        state = make_state(use_solver=True)
        rolls, sizes = engine.get_stats(state)
        plan, info = solver.solve_plan(rolls, sizes, engine.total_cap(state), 3, engine.active_chrono(state), True)
        self.assertIsNotNone(plan, info)
        self.assertTrue(solver.plan_is_valid(plan, rolls, sizes, engine.total_cap(state), 3)[0])

    def test_missing_solver_falls_back_to_greedy(self):
        import engine
        state = make_state(use_solver=True)
        self.assertEqual(set(state["plan"]), set(engine.get_stats(state)[1]))
        self.assertTrue(state["opts"]["planner_used"])


if __name__ == "__main__":
    unittest.main()
