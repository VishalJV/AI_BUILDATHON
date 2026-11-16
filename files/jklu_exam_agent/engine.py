"""
engine.py - the pure scheduling / seating / validation engine (no Streamlit imports).

Everything here is deterministic and unit-testable:
  parse + validate inputs -> clash-free course->slot plan -> rooms, seats, invigilators -> self-verification.
"""
import copy
import random
from collections import defaultdict, Counter

import pandas as pd

from solver import solve_plan, plan_is_valid, solver_available

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOTS = ["Morning", "Afternoon"]
CHRONO = [(d, s) for d in DAYS for s in SLOTS]                      # real-time order
SPREAD = [(d, "Morning") for d in DAYS] + [(d, "Afternoon") for d in DAYS]  # give students rest days first
TITLES = {"dr", "prof", "mr", "mrs", "ms"}
DEFAULT_SHIFTS = {d: len(SLOTS) for d in DAYS}      # shifts per day: 0 = no exams, 1 = Morning only, 2 = Morning + Afternoon


def active_chrono(state):
    """Exam slots that really exist, given the shifts-per-day configured in the sidebar / chat."""
    sh = (state.get("opts") or {}).get("shifts") or DEFAULT_SHIFTS
    return [(d, s) for d in DAYS for i, s in enumerate(SLOTS) if i < sh.get(d, len(SLOTS))]


def active_spread(state):
    act = set(active_chrono(state))
    return [x for x in SPREAD if x in act]        # rest-day-first order, but only for open shifts


def parse_inputs(raw_students, raw_faculty, raw_rooms):
    warns, students, seen, names = [], [], set(), {}
    for ln, line in enumerate(raw_students.strip().split("\n"), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not all(parts[:3]):
            warns.append(f"Student line {ln} skipped (needs Name, RollNo, Course): '{line.strip()[:40]}'")
            continue
        name, roll, course = parts[0], parts[1], parts[2].upper()
        if (roll, course) in seen:
            warns.append(f"Duplicate entry skipped: {roll} / {course}")
            continue
        seen.add((roll, course))
        if roll in names and names[roll].lower() != name.lower():
            warns.append(f"Roll {roll} appears with two names ('{names[roll]}', '{name}') - using the first")
            name = names[roll]
        names.setdefault(roll, name)
        students.append({"Name": name, "RollNo": roll, "Course": course})

    faculty, fseen = [], set()
    for f in raw_faculty.strip().split("\n"):
        f = f.strip()
        if f and f.lower() not in fseen:
            fseen.add(f.lower())
            faculty.append(f)

    rooms, rseen = [], set()
    for ln, line in enumerate(raw_rooms.strip().split("\n"), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            warns.append(f"Room line {ln} skipped (needs Name, Capacity, Layout)")
            continue
        try:
            cap = int(parts[1])
            if cap < 1:
                raise ValueError
        except ValueError:
            warns.append(f"Room '{parts[0]}': invalid capacity '{parts[1]}' - room skipped")
            continue
        layout = parts[2].capitalize() if len(parts) > 2 and parts[2] else "Curved"
        if layout not in ("Curved", "Straight"):
            warns.append(f"Room '{parts[0]}': unknown layout '{parts[2]}' - using Curved")
            layout = "Curved"
        if parts[0].lower() in rseen:
            warns.append(f"Duplicate room '{parts[0]}' skipped")
            continue
        rseen.add(parts[0].lower())
        rooms.append({"Room": parts[0], "Capacity": cap, "Layout": layout})
    return students, faculty, rooms, warns


def make_pattern(n, per):
    out = []
    while n > 0:
        out.append(min(per, n))
        n -= per
    return out


def make_layout(layout, cap):
    if layout.lower() == "curved":
        if cap == 40:   # the real JKLU 40-seat curved room
            wings = {"Left Wing": [1, 2, 2, 2, 2, 2, 1], "Middle Wing": [1, 1, 2, 3, 3, 3, 3], "Right Wing": [1, 2, 2, 2, 2, 2, 1]}
        else:
            side = int(round(cap * 0.3)) if cap >= 6 else 0
            wings = {}
            if side:
                wings["Left Wing"] = make_pattern(side, 2)
            wings["Middle Wing"] = make_pattern(cap - 2 * side, 3)
            if side:
                wings["Right Wing"] = make_pattern(side, 2)
    else:
        wings = {"Straight": [4] * (cap // 4) + ([cap % 4] if cap % 4 else [])}
    seats = []
    for w, pat in wings.items():
        for b, size in enumerate(pat, 1):
            for k in range(1, size + 1):
                seats.append((w, b, k))
    return wings, seats


# Adjacency weights: direct neighbours are what the anti-collusion check counts; diagonals and two-seat gaps
# only act as tie-breakers (they can never be traded against a direct neighbour) so same-course students end up far apart.
W_SIDE, W_FRONT, W_DIAG, W_NEAR = 40, 30, 2, 1      # direct neighbours dominate: one beats any number of diagonal / gap pairs
DIRECT_MIN = W_FRONT


def seat_neighbors(seats):
    idx = {s: i for i, s in enumerate(seats)}
    nb = defaultdict(list)
    for i, (w, b, k) in enumerate(seats):
        for key, wt in (((w, b, k + 1), W_SIDE), ((w, b + 1, k), W_FRONT),
                        ((w, b + 1, k + 1), W_DIAG), ((w, b + 1, k - 1), W_DIAG), ((w, b, k + 2), W_NEAR)):
            j = idx.get(key)
            if j is not None:
                nb[i].append((j, wt))
                nb[j].append((i, wt))
    return nb


def local_cost(i, occ, nb):
    o = occ[i]
    if o is None:
        return 0
    return sum(w for j, w in nb[i] if occ[j] is not None and occ[j]["Course"] == o["Course"])


def total_cost(occ, nb):
    return sum(local_cost(i, occ, nb) for i in range(len(occ)))


def _count_pairs(occ, nb, direct):
    cnt = 0
    for i in range(len(occ)):
        if occ[i] is None:
            continue
        for j, w in nb[i]:
            if j > i and occ[j] is not None and occ[j]["Course"] == occ[i]["Course"] and ((w >= DIRECT_MIN) == direct):
                cnt += 1
    return cnt


def risk_pairs(occ, nb):
    """Same-course students sitting directly side-by-side or front/back (what the verifier checks)."""
    return _count_pairs(occ, nb, True)


def near_pairs(occ, nb):
    """Same-course students on a diagonal or one seat apart (reported, optimised, but not a hard failure)."""
    return _count_pairs(occ, nb, False)


def optimise(occ, nb, rng, iters=6000, polish=1200):
    """Hill-climb with random swaps. Stops when everything is separated, or when direct neighbours are
    gone and `polish` extra iterations have improved the diagonal / two-seat-gap separation."""
    cap = len(occ)
    if cap < 2:
        return occ
    for it in range(iters):
        if it % 100 == 0:
            if total_cost(occ, nb) == 0 or (it >= polish and risk_pairs(occ, nb) == 0):
                break
        i, j = rng.randrange(cap), rng.randrange(cap)
        if i == j or (occ[i] is None and occ[j] is None):
            continue
        before = local_cost(i, occ, nb) + local_cost(j, occ, nb)
        occ[i], occ[j] = occ[j], occ[i]
        if local_cost(i, occ, nb) + local_cost(j, occ, nb) > before:
            occ[i], occ[j] = occ[j], occ[i]
    return occ


def interleave_students(chunk):
    """Round-robin by course so identical courses never start out adjacent."""
    course_map = defaultdict(list)
    for s in chunk:
        course_map[s['Course']].append(s)
    courses = list(course_map.keys())
    interleaved = []
    max_len = max(len(v) for v in course_map.values()) if course_map else 0
    for i in range(max_len):
        for c in courses:
            if i < len(course_map[c]):
                interleaved.append(course_map[c][i])
    return interleaved


_SEAT_CACHE = {}          # identical (room, students) -> identical seating, so repeated verifications are instant


def seat_room(room, students, rng):
    key = (room["Layout"], room["Capacity"], tuple((e["RollNo"], e["Course"]) for e in students))
    if key in _SEAT_CACHE:
        return copy.deepcopy(_SEAT_CACHE[key])
    wings, seats = make_layout(room["Layout"], room["Capacity"])
    cap, n = len(seats), len(students)
    nb = seat_neighbors(seats)
    chosen = list(range(cap)) if n >= cap else sorted({int(i * cap / n) for i in range(n)})  # spread out empty seats
    base = [None] * cap
    for idx, e in zip(chosen, students):
        base[idx] = e
    best, best_score = None, None
    for attempt in range(3):                                   # restart with a reshuffle if direct neighbours remain
        occ = list(base)
        if attempt:
            filled = [i for i in range(cap) if occ[i] is not None]
            vals = [occ[i] for i in filled]
            rng.shuffle(vals)
            for i, v in zip(filled, vals):
                occ[i] = v
        occ = optimise(occ, nb, rng)
        score = (risk_pairs(occ, nb), total_cost(occ, nb))
        if best is None or score < best_score:
            best, best_score = occ, score
        if score[0] == 0:
            break
    occ = best
    pairs, near = risk_pairs(occ, nb), near_pairs(occ, nb)
    grid, placed, si = {}, [], 0
    for w, pat in wings.items():
        grid[w] = []
        for b, size in enumerate(pat, 1):
            row = []
            for k in range(1, size + 1):
                e = occ[si]
                si += 1
                if e is None:
                    row.append(None)
                else:
                    e2 = dict(e)
                    e2["Position"] = f"Straight Row {b}, Seat {k}" if w == "Straight" else f"{w} | Bench {b} | Seat {k}"
                    placed.append(e2)
                    row.append(e2)
            grid[w].append(row)
    if len(_SEAT_CACHE) > 400:
        _SEAT_CACHE.clear()
    _SEAT_CACHE[key] = copy.deepcopy((placed, grid, pairs, near))
    return placed, grid, pairs, near


# =====================================================================
#  SCHEDULING ENGINE (course -> slot, clash-free, capacity-aware)
# =====================================================================
def get_stats(state):
    rolls = defaultdict(set)
    for e in state["students"]:
        rolls[e["Course"]].add(e["RollNo"])
    sizes = {c: len(v) for c, v in rolls.items()}
    return rolls, sizes


def total_cap(state):
    return sum(r["Capacity"] for r in state["rooms"])


def slot_check(course, slot, plan, rolls, sizes, cap):
    others = [c for c, s in plan.items() if s == slot and c != course]
    for o in others:
        inter = rolls[course] & rolls[o]
        if inter:
            return False, f"{len(inter)} student(s) (e.g. {sorted(inter)[0]}) also write {o} in that slot"
    load = sum(sizes[o] for o in others) + sizes[course]
    if load > cap:
        return False, f"needs {load} seats but rooms hold only {cap}"
    return True, ""


def greedy_plan(state):
    rolls, sizes = get_stats(state)
    cap, cps = total_cap(state), state["opts"]["cps"]
    spread = active_spread(state)
    conflicts = {c: {o for o in rolls if o != c and rolls[c] & rolls[o]} for c in rolls}
    spread_all = state["opts"].get("spread_all", False)
    # spread mode: open one group per available shift up front so exams use every open day/shift
    groups = [{"courses": [], "n": 0} for _ in range(min(len(spread), len(sizes)))] if spread_all else []
    unplaced = []
    for c in sorted(sizes, key=lambda x: (-sizes[x], x)):
        if sizes[c] > cap:
            unplaced.append(c)
            continue
        cands = [g for g in groups if len(g["courses"]) < cps and g["n"] + sizes[c] <= cap
                 and not any(x in conflicts[c] for x in g["courses"])]
        if cands:
            g = min(cands, key=lambda g: (g["n"], len(g["courses"])))
        else:
            if len(groups) >= len(spread):
                unplaced.append(c)
                continue
            g = {"courses": [], "n": 0}
            groups.append(g)
        g["courses"].append(c)
        g["n"] += sizes[c]
    groups = [g for g in groups if g["courses"]]
    plan = {c: spread[i] for i, g in enumerate(groups) for c in g["courses"]}
    return plan, unplaced


def initial_plan(state):
    """CP-SAT when available (and enabled); every solver answer is re-verified, otherwise the greedy planner runs.
    Returns (plan, unplaced). The planner used is recorded in state['opts']['planner_used']."""
    opts = state["opts"]
    rolls, sizes = get_stats(state)
    cap, cps = total_cap(state), opts["cps"]
    if opts.get("use_solver", True) and solver_available():
        plan, info = solve_plan(rolls, sizes, cap, cps, active_chrono(state), opts.get("spread_all", False))
        if plan is not None:
            ok, why = plan_is_valid(plan, rolls, sizes, cap, cps)
            if ok:
                opts["planner_used"] = info
                unplaced = sorted(c for c in sizes if c not in plan)
                return plan, unplaced
            info = f"solver result rejected by verifier ({why})"
        opts["planner_used"] = f"greedy fallback ({info})"
    else:
        opts["planner_used"] = "greedy heuristic" + ("" if solver_available() else " (OR-Tools not installed)")
    return greedy_plan(state)


def room_counts(n, caps, strategy):
    total = sum(caps)
    if n > total:
        return None
    if strategy == "min":
        out, rem = [], n
        for c in caps:
            take = min(c, rem)
            out.append(take)
            rem -= take
        return out
    base = [int(n * c / total) for c in caps]
    rem = n - sum(base)
    order = sorted(range(len(caps)), key=lambda i: -(n * caps[i] / total - base[i]))
    i = 0
    while rem > 0:
        j = order[i % len(order)]
        if base[j] < caps[j]:
            base[j] += 1
            rem -= 1
        i += 1
    return base


def deal(seq, quotas):
    buckets, remaining = [[] for _ in quotas], list(quotas)
    for e in seq:
        j = max((i for i in range(len(quotas)) if remaining[i] > 0), key=lambda i: remaining[i] / quotas[i])
        buckets[j].append(e)
        remaining[j] -= 1
    return buckets


def unavailable(state, fac, day):
    lv = state["leave"].get(fac, set())
    return "ALL" in lv or day in lv


def build_allocations(state):
    """Turns the course->slot plan into rooms, seats and invigilators."""
    by_course = defaultdict(list)
    for e in state["students"]:
        by_course[e["Course"]].append(e)
    for c in by_course:
        by_course[c].sort(key=lambda e: e["RollNo"])
    rooms = sorted(state["rooms"], key=lambda r: -r["Capacity"])
    by_slot = defaultdict(list)
    for c, sl in state["plan"].items():
        by_slot[sl].append(c)

    rows, room_allocs, overflow, new_duties = [], {}, [], {}
    load, sess_no, fidx = Counter(), 0, {f: i for i, f in enumerate(state["faculty"])}

    for slot in CHRONO:
        if slot not in by_slot:
            continue
        day, sl = slot
        courses = sorted(by_slot[slot])
        entries = [e for c in courses for e in by_course[c]]
        counts = room_counts(len(entries), [r["Capacity"] for r in rooms], state["opts"]["strategy"])
        if counts is None:
            overflow.append(f"{day} {sl}")
            continue
        used = [(r, n) for r, n in zip(rooms, counts) if n > 0]
        buckets = deal(interleave_students(entries), [n for _, n in used])

        avail = [f for f in state["faculty"] if not unavailable(state, f, day)]
        taken, invs = set(), {}
        for r, _ in used:                                  # keep still-valid previous duties (minimal change)
            key = f"{day}|{sl}|{r['Room']}"
            keep = [f for f in state["duties"].get(key, []) if f in avail and f not in taken][:2]
            invs[key] = keep
            taken.update(keep)
            for f in keep:
                load[f] += 1
        for target in (1, 2):                              # everyone gets 1 first, then a 2nd
            for r, _ in used:
                key = f"{day}|{sl}|{r['Room']}"
                while len(invs[key]) < target:
                    free = [f for f in avail if f not in taken]
                    if not free:
                        break
                    f = min(free, key=lambda x: (load[x], fidx[x]))
                    invs[key].append(f)
                    taken.add(f)
                    load[f] += 1

        for (r, n), stu in zip(used, buckets):
            key = f"{day}|{sl}|{r['Room']}"
            new_duties[key] = invs[key]
            rng = random.Random(key)
            placed, grid, pairs, near = seat_room(r, stu, rng)
            sess_no += 1
            inv_str = ", ".join(invs[key]) if invs[key] else "UNASSIGNED"
            courses_here = sorted({e["Course"] for e in placed})
            rows.append({
                "Session ID": f"SESS-{sess_no:02d} ({r['Room']})", "Day": day, "Slot": sl,
                "Room": f"{r['Room']} ({r['Layout']}, Cap: {r['Capacity']})",
                "Assigned Students": len(placed), "Courses Seated": ", ".join(courses_here),
                "Invigilators": inv_str})
            room_allocs[f"{r['Room']} ({r['Layout']}) - {day} {sl}"] = {
                "room_name": r["Room"], "layout": r["Layout"], "capacity": r["Capacity"],
                "invigilators": inv_str, "inv_list": list(invs[key]), "students": placed,
                "grid": grid, "risk_pairs": pairs, "near_pairs": near, "day": day, "slot": sl}
    state["duties"] = new_duties
    return {"df_sch": pd.DataFrame(rows), "room_allocs": room_allocs, "overflow": overflow}


def run_validation(state, alloc):
    checks = []

    def add(name, status, detail, kind):
        checks.append({"Check": name, "Status": {"pass": "✅ Pass", "warn": "⚠️ Warning", "fail": "❌ Fail"}[status],
                       "Detail": detail, "kind": kind})

    plan, pending = state["plan"], state["pending"]
    ra = alloc["room_allocs"]
    add("All courses have an exam slot", "pass" if not pending and not alloc["overflow"] else "fail",
        "All courses scheduled" if not pending and not alloc["overflow"]
        else f"Unscheduled: {', '.join(pending) or '-'} {('| overflow: ' + ', '.join(alloc['overflow'])) if alloc['overflow'] else ''}", "complete")

    open_slots = set(active_chrono(state))
    closed = [f"{c} ({d} {s})" for c, (d, s) in plan.items() if (d, s) not in open_slots]
    add("Exams only in configured shifts", "pass" if not closed else "fail",
        "Every exam sits in an open day/shift" if not closed else f"In closed shifts: {', '.join(closed[:3])}", "integrity")

    expected = {(e["RollNo"], e["Course"]) for e in state["students"] if e["Course"] in plan}
    seated = Counter((s["RollNo"], s["Course"]) for r in ra.values() for s in r["students"])
    missing, dups = expected - set(seated), [k for k, v in seated.items() if v > 1]
    ok = not missing and not dups
    add("Every student-exam seated exactly once", "pass" if ok else "fail",
        f"{len(seated)}/{len(expected)} exam seats allocated" if ok else f"{len(missing)} missing, {len(dups)} duplicated", "integrity")

    rolls_slots = defaultdict(list)
    for c, sl in plan.items():
        for r in get_stats(state)[0][c]:
            rolls_slots[r].append(sl)
    clashes = [r for r, v in rolls_slots.items() if len(v) != len(set(v))]
    add("No student has two exams in one slot", "pass" if not clashes else "fail",
        "Zero student clashes" if not clashes else f"Clashing students: {', '.join(clashes[:3])}", "integrity")

    over = [k for k, r in ra.items() if len(r["students"]) > r["capacity"]]
    add("Room capacity respected", "pass" if not over else "fail",
        "No room exceeds capacity" if not over else f"Over capacity: {over[0]}", "integrity")

    bad_pos = [k for k, r in ra.items() if len({s["Position"] for s in r["students"]}) != len(r["students"])]
    add("No seat double-booked", "pass" if not bad_pos else "fail",
        "All seat positions unique" if not bad_pos else f"Duplicate seats in {bad_pos[0]}", "integrity")

    per_slot, leave_bad = defaultdict(list), []
    for r in ra.values():
        for f in r["inv_list"]:
            per_slot[(r["day"], r["slot"])].append(f)
            if unavailable(state, f, r["day"]):
                leave_bad.append(f)
    dbl = [f"{f} @ {k[0]} {k[1]}" for k, v in per_slot.items() for f, n in Counter(v).items() if n > 1]
    add("Invigilators not double-booked / on leave", "pass" if not dbl and not leave_bad else "fail",
        "No invigilator conflicts" if not dbl and not leave_bad else f"{(dbl + leave_bad)[0]}", "integrity")

    zero = [k for k, r in ra.items() if not r["inv_list"]]
    add("Every room has an invigilator", "pass" if not zero else "fail",
        "All rooms staffed" if not zero else f"{len(zero)} room(s) unstaffed - not enough faculty", "integrity")
    one = [k for k, r in ra.items() if len(r["inv_list"]) == 1]
    add("Two invigilators per room", "pass" if not one else "warn",
        "All rooms have 2 invigilators" if not one else f"{len(one)} room(s) have only 1 (add faculty)", "soft")

    pairs = sum(r["risk_pairs"] for r in ra.values())
    add("Anti-collusion separation", "pass" if pairs == 0 else "warn",
        "0 same-course neighbours (side-by-side / front-back)" if pairs == 0 else f"{pairs} same-course neighbour pair(s) remain", "soft")
    return checks


def integrity_fails(checks):
    return {c["Check"] for c in checks if c["kind"] == "integrity" and c["Status"].startswith("❌")}
