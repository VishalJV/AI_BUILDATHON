import copy
import unittest

from helpers import make_state, demo_inputs
import engine


class EngineTests(unittest.TestCase):
    def test_parse_inputs_reports_bad_lines(self):
        students, faculty, rooms, warns = engine.parse_inputs(
            "Asha, R1, cs101\nBroken line\nAsha, R1, CS101\nBen, R2, CS101", "Dr. A\nDr. A\nDr. B", "R-1, 40, Curved\nR-2, abc\nR-1, 30")
        self.assertEqual([s["RollNo"] for s in students], ["R1", "R2"])      # bad + duplicate lines skipped
        self.assertEqual(faculty, ["Dr. A", "Dr. B"])                        # duplicates removed
        self.assertEqual([r["Room"] for r in rooms], ["R-1"])                # invalid capacity + duplicate room skipped
        self.assertEqual(len(warns), 4)

    def test_demo_schedule_passes_every_check(self):
        state = make_state()
        alloc = engine.build_allocations(state)
        checks = engine.run_validation(state, alloc)
        self.assertTrue(all(c["Status"].startswith("✅") for c in checks), [c for c in checks if not c["Status"].startswith("✅")])

    def test_no_student_has_two_exams_in_one_slot(self):
        state = make_state()
        rolls, _ = engine.get_stats(state)
        by_slot = {}
        for c, sl in state["plan"].items():
            by_slot.setdefault(sl, []).append(c)
        for courses in by_slot.values():
            for i, a in enumerate(courses):
                for b in courses[i + 1:]:
                    self.assertFalse(rolls[a] & rolls[b])

    def test_anti_collusion_no_direct_neighbours(self):
        alloc = engine.build_allocations(make_state())
        self.assertEqual(sum(r["risk_pairs"] for r in alloc["room_allocs"].values()), 0)

    def test_validator_catches_tampering(self):
        state = make_state()
        alloc = engine.build_allocations(state)
        bad = copy.deepcopy(alloc)
        room = next(iter(bad["room_allocs"].values()))
        room["students"].append(dict(room["students"][0]))                    # same student seated twice
        failed = {c["Check"] for c in engine.run_validation(state, bad) if c["Status"].startswith("❌")}
        self.assertIn("Every student-exam seated exactly once", failed)

    def test_faculty_on_leave_is_never_assigned(self):
        state = make_state()
        victim = state["faculty"][0]
        state["leave"][victim] = {"ALL"}
        alloc = engine.build_allocations(state)
        self.assertTrue(all(victim not in r["inv_list"] for r in alloc["room_allocs"].values()))
        checks = engine.run_validation(state, alloc)
        self.assertTrue(all(c["Status"].startswith("✅") for c in checks))

    def test_capacity_overflow_is_reported_not_crashed(self):
        s, f, _ = demo_inputs()
        students, faculty, rooms, _ = engine.parse_inputs(s, f, "Tiny, 10, Straight")
        state = {"students": students, "faculty": faculty, "rooms": rooms, "plan": {}, "pending": [], "leave": {}, "duties": {},
                 "opts": {"cps": 3, "strategy": "spread", "shifts": dict(engine.DEFAULT_SHIFTS), "spread_all": True, "use_solver": False}}
        plan, unplaced = engine.initial_plan(state)
        self.assertTrue(unplaced)                                              # courses too big for the room stay unscheduled
        state["plan"], state["pending"] = plan, unplaced
        checks = engine.run_validation(state, engine.build_allocations(state))
        self.assertTrue(any(c["Check"] == "All courses have an exam slot" and c["Status"].startswith("❌") for c in checks))

    def test_seating_is_deterministic(self):
        a = engine.build_allocations(make_state())["room_allocs"]
        b = engine.build_allocations(make_state())["room_allocs"]
        self.assertEqual({k: [s["RollNo"] for s in v["students"]] for k, v in a.items()},
                         {k: [s["RollNo"] for s in v["students"]] for k, v in b.items()})


if __name__ == "__main__":
    unittest.main()
