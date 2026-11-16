"""
solver.py - optional OR-Tools CP-SAT planner for the course -> exam-slot assignment.

* `solve_plan` builds a constraint model (no student clashes, room capacity, max courses per slot) and
  optimises it (balanced load / fewer slots, and no back-to-back exams for students who share courses).
* `plan_is_valid` is an independent checker. The app NEVER trusts the solver blindly: if the solver is
  missing, infeasible, times out or returns something that fails verification, the caller falls back to
  the deterministic greedy planner.
"""


def solver_available():
    try:
        from ortools.sat.python import cp_model  # noqa: F401
        return True
    except Exception:
        return False


def plan_is_valid(plan, rolls, sizes, cap, cps):
    """Re-check a {course: slot} plan from scratch. Returns (ok, reason)."""
    by_slot = {}
    for c, sl in plan.items():
        by_slot.setdefault(sl, []).append(c)
    for sl, courses in by_slot.items():
        if len(courses) > cps:
            return False, f"{len(courses)} courses in {sl} exceeds the limit of {cps}"
        if sum(sizes[c] for c in courses) > cap:
            return False, f"{sl} needs more seats than the rooms hold ({cap})"
        for i, a in enumerate(courses):
            for b in courses[i + 1:]:
                if rolls[a] & rolls[b]:
                    return False, f"{a} and {b} share students in {sl}"
    return True, ""


def _gap_weight(slots, s, t):
    """Penalty weight for two exams sitting in neighbouring slots (slots are in chronological order)."""
    i, j = slots.index(s), slots.index(t)
    if abs(i - j) != 1:
        return 0
    return 3 if s[0] == t[0] else 1        # same-day back-to-back hurts more than an overnight gap


def solve_plan(rolls, sizes, cap, cps, slots, spread_all=True, time_limit=5.0):
    """
    rolls: {course: set(rolls)}   sizes: {course: n}   cap: total seats per slot   cps: max courses per slot
    slots: chronological list of open (day, slot) tuples.
    Returns (plan, info). plan is {course: (day, slot)} or None when the solver cannot/should not be used.
    """
    try:
        from ortools.sat.python import cp_model
    except Exception:
        return None, "ortools not installed"
    courses = sorted(c for c in sizes if sizes[c] <= cap)
    if not courses or not slots:
        return None, "nothing to solve"
    if len(courses) > len(slots) * cps:
        return None, "more courses than the open slots can hold"
    try:
        m = cp_model.CpModel()
        x = {(c, s): m.NewBoolVar(f"x_{c}_{i}") for c in courses for i, s in enumerate(slots)}
        for c in courses:
            m.Add(sum(x[c, s] for s in slots) == 1)                       # every course gets exactly one slot
        conflicts = {}
        for i, a in enumerate(courses):
            for b in courses[i + 1:]:
                shared = len(rolls[a] & rolls[b])
                if shared:
                    conflicts[(a, b)] = shared
        for s in slots:
            m.Add(sum(x[c, s] for c in courses) <= cps)                   # max courses per slot
            m.Add(sum(sizes[c] * x[c, s] for c in courses) <= cap)        # room capacity
            for (a, b) in conflicts:
                m.Add(x[a, s] + x[b, s] <= 1)                             # a shared student can't sit both
        # objective: balanced / compact load + rest between exams for students who share courses
        pen_terms = []
        for (a, b), shared in conflicts.items():
            for s in slots:
                for t in slots:
                    w = _gap_weight(slots, s, t)
                    if w:
                        y = m.NewBoolVar(f"y_{a}_{b}_{slots.index(s)}_{slots.index(t)}")
                        m.Add(x[a, s] + x[b, t] - 1 <= y)
                        pen_terms.append(shared * w * y)
        if spread_all:
            maxload = m.NewIntVar(0, cap, "maxload")
            for s in slots:
                m.Add(sum(sizes[c] * x[c, s] for c in courses) <= maxload)
            objective = 2 * maxload + sum(pen_terms)
        else:
            used = {s: m.NewBoolVar(f"used_{i}") for i, s in enumerate(slots)}
            for c in courses:
                for s in slots:
                    m.Add(x[c, s] <= used[s])
            objective = 1000 * sum(used.values()) + sum(pen_terms)
        m.Minimize(objective)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(time_limit)
        try:
            solver.parameters.random_seed = 1                             # reproducible demos
        except Exception:
            pass
        status = solver.Solve(m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None, f"solver status {solver.StatusName(status)}"
        plan = {c: s for c in courses for s in slots if solver.Value(x[c, s])}
        label = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE (time limit)"
        return plan, f"OR-Tools CP-SAT ({label})"
    except Exception as e:                                                # any API / model problem -> caller falls back
        return None, f"solver error: {type(e).__name__}: {e}"
