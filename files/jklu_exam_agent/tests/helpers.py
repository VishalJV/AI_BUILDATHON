import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def demo_inputs():
    """Same synthetic demo dataset as the app's 'Quick-Load Demo Dataset' (120 students + 6 double enrolments)."""
    pool = ["MATH101", "CS101", "PHY101", "CHEM101", "ECO101", "AI101"]
    first = ["Aaradhya", "Abhishek", "Aditya", "Akshara", "Ananya", "Devanshi", "Divyanshu", "Gautam", "Kanishka", "Manas",
             "Mayank", "Pradeep", "Rishin", "Sanjay", "Shagun", "Shaurya", "Uday", "Vishal"]
    last = ["Sharma", "Aheer", "Singh", "Jain", "Gupta", "Rathore", "Reddy", "Chauhan", "Kaur", "Agarwal", "Choudhary", "Khandelwal"]
    rng = random.Random(42)
    lines, names = [], {}
    for i in range(1, 121):
        fn, ln = rng.choice(first), rng.choice(last)
        roll = f"2026BTech{i:03d}"
        names[roll] = f"{fn} {ln}"
        lines.append(f"{fn} {ln}, {roll}, {pool[(i - 1) % 6]}")
    for i in range(1, 7):
        roll = f"2026BTech{i:03d}"
        lines.append(f"{names[roll]}, {roll}, {pool[i % 6]}")
    faculty = "\n".join(f"Dr. Faculty{i} Name{i}" for i in range(1, 11))
    rooms = "TB1-102, 40, Curved\nLH-1, 80, Straight\nLT-301, 40, Curved"
    return "\n".join(lines), faculty, rooms


def make_state(cps=3, strategy="spread", spread_all=True, use_solver=False):
    import engine
    s, f, r = demo_inputs()
    students, faculty, rooms, _ = engine.parse_inputs(s, f, r)
    state = {"students": students, "faculty": faculty, "rooms": rooms, "plan": {}, "pending": [], "leave": {}, "duties": {},
             "opts": {"cps": cps, "strategy": strategy, "shifts": dict(engine.DEFAULT_SHIFTS), "spread_all": spread_all, "use_solver": use_solver}}
    plan, unplaced = engine.initial_plan(state)
    state["plan"], state["pending"] = plan, unplaced
    return state
