import streamlit as st
import pandas as pd
import io
import os
import re
import json
import math
import time
import copy
import random
import datetime
import requests
from collections import defaultdict, Counter
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# =====================================================================
#  PAGE CONFIG + STYLE (unchanged look)
# =====================================================================
st.set_page_config(page_title="JKLU AI Exam & Seating Scheduler", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
    <style>
    .main { background-color: #0a0e1a; color: #f8fafc; font-family: 'Inter', sans-serif; }
    .stMetric {
        background: linear-gradient(135deg, #111827 0%, #1f2937 100%);
        padding: 18px;
        border-radius: 12px;
        border: 1px solid #374151;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
    }
    h1, h2, h3 { color: #f1f5f9; font-weight: 600; }
    [data-testid="stSidebar"] { background-color: #0d1322; border-right: 1px solid #1f2937; }
    </style>
""", unsafe_allow_html=True)

st.title("🎓 JKLU Autonomous Exam & Seating Arrangement System")
st.markdown("---")

# =====================================================================
#  CONSTANTS
# =====================================================================
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOTS = ["Morning", "Afternoon"]
CHRONO = [(d, s) for d in DAYS for s in SLOTS]                      # real-time order
SPREAD = [(d, "Morning") for d in DAYS] + [(d, "Afternoon") for d in DAYS]  # give students rest days first
def _secret(name):
    """API keys come from env vars or .streamlit/secrets.toml - never hard-code them in source."""
    v = os.environ.get(name, "")
    if not v:
        try:
            v = st.secrets.get(name, "")
        except Exception:
            v = ""
    return v


GEMINI_KEY_DEFAULT = _secret("GEMINI_API_KEY")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "admin123")                 # demo PIN; set env var ADMIN_PIN to change
TITLES = {"dr", "prof", "mr", "mrs", "ms"}
PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#06b6d4", "#a855f7", "#84cc16", "#ec4899"]
DEFAULT_SHIFTS = {d: len(SLOTS) for d in DAYS}      # shifts per day: 0 = no exams, 1 = Morning only, 2 = Morning + Afternoon


def active_chrono(state):
    """Exam slots that really exist, given the shifts-per-day configured in the sidebar / chat."""
    sh = (state.get("opts") or {}).get("shifts") or DEFAULT_SHIFTS
    return [(d, s) for d in DAYS for i, s in enumerate(SLOTS) if i < sh.get(d, len(SLOTS))]


def active_spread(state):
    act = set(active_chrono(state))
    return [x for x in SPREAD if x in act]        # rest-day-first order, but only for open shifts

# =====================================================================
#  SESSION + AUDIT LOG
# =====================================================================
for _k, _v in {"chat_history": [], "audit_log": [], "status": "DRAFT", "version": 0}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


def log(action, detail, actor="system"):
    st.session_state["audit_log"].append({
        "Time": datetime.datetime.now().strftime("%H:%M:%S"),
        "Actor": actor, "Action": action, "Detail": str(detail)[:400]})


# =====================================================================
#  INPUT PARSING + VALIDATION
# =====================================================================
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


# =====================================================================
#  SEAT LAYOUTS (curved amphitheatre + straight rows) + ADJACENCY
# =====================================================================
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


def seat_neighbors(seats):
    idx = {s: i for i, s in enumerate(seats)}
    nb = defaultdict(list)
    for i, (w, b, k) in enumerate(seats):
        for key, wt in (((w, b, k + 1), 2), ((w, b + 1, k), 1)):   # side-by-side=2, front/back=1
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


def risk_pairs(occ, nb):
    cnt = 0
    for i in range(len(occ)):
        if occ[i] is None:
            continue
        for j, _ in nb[i]:
            if j > i and occ[j] is not None and occ[j]["Course"] == occ[i]["Course"]:
                cnt += 1
    return cnt


def optimise(occ, nb, rng, iters=6000):
    """Hill-climb: swap seats until no same-course students are adjacent (side-by-side / front-back)."""
    cap = len(occ)
    if total_cost(occ, nb) == 0:
        return occ
    for it in range(iters):
        i, j = rng.randrange(cap), rng.randrange(cap)
        if i == j or (occ[i] is None and occ[j] is None):
            continue
        before = local_cost(i, occ, nb) + local_cost(j, occ, nb)
        occ[i], occ[j] = occ[j], occ[i]
        if local_cost(i, occ, nb) + local_cost(j, occ, nb) > before:
            occ[i], occ[j] = occ[j], occ[i]
        if it % 100 == 0 and total_cost(occ, nb) == 0:
            break
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


def seat_room(room, students, rng):
    wings, seats = make_layout(room["Layout"], room["Capacity"])
    cap, n = len(seats), len(students)
    nb = seat_neighbors(seats)
    chosen = list(range(cap)) if n >= cap else sorted({int(i * cap / n) for i in range(n)})  # spread out empty seats
    occ = [None] * cap
    for idx, e in zip(chosen, students):
        occ[idx] = e
    occ = optimise(occ, nb, rng)
    pairs = risk_pairs(occ, nb)
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
    return placed, grid, pairs


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


def initial_plan(state):
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
            placed, grid, pairs = seat_room(r, stu, rng)
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
                "grid": grid, "risk_pairs": pairs, "day": day, "slot": sl}
    state["duties"] = new_duties
    return {"df_sch": pd.DataFrame(rows), "room_allocs": room_allocs, "overflow": overflow}


# =====================================================================
#  VERIFICATION (the agent checks its own output)
# =====================================================================
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


def refresh():
    S = st.session_state
    S["alloc"] = build_allocations(S["state"])
    S["checks"] = run_validation(S["state"], S["alloc"])


# =====================================================================
#  AGENT TOOLS
# =====================================================================
DAY_RE = r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)\b"


def norm_day(x):
    if not x:
        return None
    key = str(x).strip().lower()[:3]
    for d in DAYS:
        if d.lower()[:3] == key:
            return d
    raise ValueError(f"'{x}' is not an exam day (exams run Monday-Friday)")


def norm_slot(x):
    if not x:
        return None
    k = str(x).lower()
    if k.startswith(("morn", "am", "fore")):
        return "Morning"
    if k.startswith(("after", "pm", "even")):
        return "Afternoon"
    raise ValueError(f"Unknown slot '{x}' (use Morning or Afternoon)")


def norm_course(x, state):
    x = str(x).upper().replace(" ", "")
    _, sizes = get_stats(state)
    if x not in sizes:
        raise ValueError(f"Unknown course '{x}'. Known: {', '.join(sorted(sizes))}")
    return x


def match_faculty(query, faculty):
    """Best faculty match(es): a full-name match beats a partial (surname-only) one; ties are returned as a list."""
    low = str(query).lower()
    scored = []
    for f in faculty:
        toks = [t for t in re.findall(r"[a-z]+", f.lower()) if t not in TITLES and len(t) > 2]
        hits = sum(1 for t in toks if re.search(rf"\b{t}\b", low))
        if hits:
            scored.append((hits == len(toks), hits, f))
    if not scored:
        return []
    best = max((x[0], x[1]) for x in scored)
    return [f for full, h, f in scored if (full, h) == best]


def valid_slots(course, plan, state, limit=3):
    rolls, sizes = get_stats(state)
    out = []
    for sl in active_chrono(state):
        ok, _ = slot_check(course, sl, {c: s for c, s in plan.items() if c != course}, rolls, sizes, total_cap(state))
        if ok:
            out.append(f"{sl[0]} {sl[1]}")
        if len(out) >= limit:
            break
    return out


def act_cancel(a, state):
    plan = state["plan"]
    if a.get("course"):
        c = norm_course(a["course"], state)
        if c not in plan:
            return {"ok": False, "msg": f"{c} is not currently scheduled."}
        targets = [c]
    else:
        day, slot = norm_day(a.get("day")), norm_slot(a.get("slot"))
        if not day:
            return {"ok": False, "msg": "Which day (or course) should I cancel?"}
        targets = [c for c, (d, s) in plan.items() if d == day and (not slot or s == slot)]
        if not targets:
            return {"ok": False, "msg": f"No exams are scheduled on {day}{' ' + slot if slot else ''}."}
    rolls, _ = get_stats(state)
    n = len(set().union(*[rolls[c] for c in targets]))
    for c in targets:
        state["pending"].append(c)
        del plan[c]
    return {"ok": True, "changed": True,
            "msg": f"Cancelled **{', '.join(targets)}** ({n} students affected). They are now *unscheduled* - say **'auto reschedule'** and I will re-slot them clash-free."}


def act_move(a, state):
    rolls, sizes = get_stats(state)
    cap, plan = total_cap(state), dict(state["plan"])
    td, ts = norm_day(a.get("to_day")), norm_slot(a.get("to_slot"))
    if a.get("course"):
        c = norm_course(a["course"], state)
        cur = plan.get(c)
        day = td or (cur[0] if cur else None)
        if not day:
            return {"ok": False, "msg": f"Which day should {c} move to?"}
        moves = [(c, day, ts or (cur[1] if cur else None))]
    else:
        fd, fs = norm_day(a.get("from_day")), norm_slot(a.get("from_slot"))
        if not fd or not td:
            return {"ok": False, "msg": "Please give both the source day and the destination day."}
        sel = [c for c, (d, s) in plan.items() if d == fd and (not fs or s == fs)]
        if not sel:
            return {"ok": False, "msg": f"No exams are scheduled on {fd}, so there is nothing to move."}
        moves = [(c, td, ts or plan[c][1]) for c in sel]
    for c, _, _ in moves:
        plan.pop(c, None)
    notes = []
    for c, d, s in moves:
        act = active_chrono(state)
        tries = [t for t in (([(d, s)] if s else []) + [(d, x) for x in SLOTS if (d, x) != (d, s)]) if t in act]
        if not tries:
            return {"ok": False, "msg": f"{d} has 0 exam shifts configured, so nothing can be placed there. Open a shift first, e.g. 'Set {d} shifts to 2'."}
        placed, reasons = None, []
        for sl in tries:
            ok, why = slot_check(c, sl, plan, rolls, sizes, cap)
            if ok:
                placed = sl
                break
            reasons.append(f"{sl[0]} {sl[1]}: {why}")
        if placed is None:
            alts = valid_slots(c, plan, state)
            return {"ok": False, "msg": f"Cannot move **{c}** to {d} - " + "; ".join(reasons)
                    + (f". Nearest valid slots: {', '.join(alts)}." if alts else ".")}
        plan[c] = placed
        notes.append(f"**{c}** -> {placed[0]} {placed[1]}" + (" *(auto-switched slot to avoid a clash)*" if s and placed != (d, s) else ""))
    state["plan"] = plan
    state["pending"] = [p for p in state["pending"] if p not in plan]
    return {"ok": True, "changed": True, "msg": "Rescheduled: " + "; ".join(notes) + "."}


def act_leave(a, state):
    cands = match_faculty(a.get("faculty", ""), state["faculty"])
    if len(cands) != 1:
        return {"ok": False, "msg": ("Which faculty member? Matches: " + ", ".join(cands)) if cands
                else f"I could not find '{a.get('faculty', '')}' in the faculty list."}
    f, day = cands[0], norm_day(a.get("day"))
    state["leave"].setdefault(f, set()).add(day or "ALL")
    old = copy.deepcopy(state["duties"])
    build_allocations(state)
    swaps = []
    for key, invs in old.items():
        new = state["duties"].get(key, [])
        if f in invs and f not in new:
            rep = [x for x in new if x not in invs]
            d, s, room = key.split("|")
            swaps.append(f"{d} {s} / {room}: " + (rep[0] if rep else f"no spare faculty (room runs with {len(new)})"))
    if not swaps:
        return {"ok": True, "changed": True, "msg": f"Marked **{f}** unavailable{' on ' + day if day else ''}. No existing duties were affected."}
    return {"ok": True, "changed": True, "msg": f"**{f}** unavailable{' on ' + day if day else ''}. Replacements (least-loaded, clash-free): " + "; ".join(swaps) + "."}


def act_place_pending(a, state):
    if not state["pending"]:
        return {"ok": False, "msg": "There are no unscheduled courses."}
    rolls, sizes = get_stats(state)
    cap, cps, notes = total_cap(state), state["opts"]["cps"], []
    for c in list(state["pending"]):
        cands = []
        for sl in active_chrono(state):
            ok, _ = slot_check(c, sl, state["plan"], rolls, sizes, cap)
            if ok:
                n = sum(1 for s in state["plan"].values() if s == sl)
                if state["opts"].get("spread_all"):
                    cands.append(((0 if n == 0 else (1 if n < cps else 2)), CHRONO.index(sl), sl))   # prefer empty shifts
                else:
                    cands.append(((0 if 0 < n < cps else (1 if n == 0 else 2)), CHRONO.index(sl), sl))
        if not cands:
            return {"ok": False, "msg": f"No valid slot exists for {c} (clashes/capacity)."}
        sl = min(cands)[2]
        state["plan"][c] = sl
        state["pending"].remove(c)
        notes.append(f"**{c}** -> {sl[0]} {sl[1]}")
    return {"ok": True, "changed": True, "msg": "Auto-scheduled (clash-free, capacity-checked): " + "; ".join(notes) + "."}


def act_lookup(a, state):
    roll = str(a.get("roll", "") or "").strip().lower()
    name = str(a.get("name", "") or "").strip().lower()
    if not roll and not name:
        return {"ok": False, "changed": False, "msg": "Which student? Give a roll number (e.g. 2026BTech005) or a full name."}
    alloc = build_allocations(state)
    out = []
    for r in alloc["room_allocs"].values():
        for s in r["students"]:
            if (roll and s["RollNo"].lower() == roll) or (name and not roll and s["Name"].lower() == name):
                out.append(f"{s['Name']} ({s['RollNo']}) - **{s['Course']}**: {r['day']} {r['slot']}, {r['room_name']}, {s['Position']}")
    if len(out) > 8:
        out = out[:8] + [f"...and {len(out) - 8} more - add a roll number to narrow it down."]
    return {"ok": bool(out), "changed": False,
            "msg": "\n\n".join(out) if out else f"No seat found for '{a.get('roll') or a.get('name')}'. Check the roll number / full name."}


def act_duties(a, state):
    cands = match_faculty(a.get("faculty", ""), state["faculty"])
    if len(cands) != 1:
        return {"ok": False, "msg": ("Which faculty member? Matches: " + ", ".join(cands)) if cands
                else f"I could not find '{a.get('faculty', '')}' in the faculty list ({', '.join(state['faculty'])})."}
    alloc = build_allocations(state)
    out = [f"{r['day']} {r['slot']} - {r['room_name']}" for r in alloc["room_allocs"].values() if cands[0] in r["inv_list"]]
    lv = state["leave"].get(cands[0])
    extra = f" (on leave: {', '.join(sorted(lv))})" if lv else ""
    return {"ok": True, "changed": False, "msg": f"**{cands[0]}**{extra} - {len(out)} duty slot(s): " + ("; ".join(out) if out else "none")}


def act_verify(a, state):
    checks = run_validation(state, build_allocations(state))
    passed = sum(1 for c in checks if c["Status"].startswith("✅"))
    bad = [f"{c['Check']}: {c['Detail']}" for c in checks if not c["Status"].startswith("✅")]
    return {"ok": True, "changed": False, "msg": f"{passed}/{len(checks)} checks passed." + (" Open items: " + " | ".join(bad) if bad else "")}


def act_shifts(a, state):
    """Change how many exam shifts run per day (0-2). Courses sitting in a closed shift are re-slotted clash-free."""
    new = dict(state["opts"].get("shifts") or DEFAULT_SHIFTS)
    ch = a.get("shifts")
    if isinstance(ch, dict) and ch:
        items = list(ch.items())
    elif a.get("day") is not None and a.get("count") is not None:
        items = [(a["day"], a["count"])]
    else:
        return {"ok": False, "msg": "Tell me the day and the number of shifts, e.g. 'Set Monday shifts to 2 and Tuesday to 1'."}
    for d, n in items:
        day = norm_day(d)
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {"ok": False, "msg": f"'{n}' is not a valid shift count for {day} (use 0, 1 or 2)."}
        if not 0 <= n <= len(SLOTS):
            return {"ok": False, "msg": f"{day}: shifts must be between 0 and {len(SLOTS)} (1 = Morning only, 2 = Morning + Afternoon)."}
        new[day] = n
    if sum(new.values()) == 0:
        return {"ok": False, "msg": "At least one day needs one shift - otherwise there is nowhere to hold exams."}
    state["opts"]["shifts"] = new
    act = set(active_chrono(state))
    displaced = [c for c, sl in state["plan"].items() if sl not in act]
    for c in displaced:
        state["pending"].append(c)
        del state["plan"][c]
    msg = "Shifts per day now: " + ", ".join(f"{d} {new[d]}" for d in DAYS) + "."
    if displaced:
        msg += f" Courses in closed shifts: **{', '.join(sorted(displaced))}**."
        res = act_place_pending({}, state)
        msg += " " + res["msg"]
    return {"ok": True, "changed": True, "msg": msg}


def act_show(a, state):
    day = norm_day(a.get("day")) if a.get("day") else None
    _, sizes = get_stats(state)
    sh = state["opts"].get("shifts") or DEFAULT_SHIFTS
    rows = [f"**{c}** - {d} {s} ({sizes[c]} students)"
            for c, (d, s) in sorted(state["plan"].items(), key=lambda kv: (CHRONO.index(kv[1]), kv[0])) if not day or d == day]
    head = f"Shifts per day: {', '.join(f'{d} {sh.get(d, len(SLOTS))}' for d in DAYS)}."
    if day and not rows:
        return {"ok": True, "changed": False, "msg": f"No exams are scheduled on {day} ({sh.get(day, len(SLOTS))} shift(s) open). {head}"}
    pend = f"\n\nUnscheduled: {', '.join(state['pending'])}" if state["pending"] and not day else ""
    return {"ok": True, "changed": False, "msg": head + "\n\n" + "\n\n".join(rows) + pend}


def act_reset(a, state):
    base = copy.deepcopy(st.session_state["base_state"])
    state.clear()
    state.update(base)
    return {"ok": True, "changed": True, "msg": "Restored the original generated schedule (leave records cleared)."}


def act_swap(a, state):
    """Swap the exam slots of two courses, or of two whole days - validated clash-free before anything changes."""
    rolls, sizes = get_stats(state)
    cap, plan = total_cap(state), dict(state["plan"])
    if a.get("course_a") and a.get("course_b"):
        ca, cb = norm_course(a["course_a"], state), norm_course(a["course_b"], state)
        if ca == cb:
            return {"ok": False, "msg": "Please name two different courses to swap."}
        missing = [c for c in (ca, cb) if c not in plan]
        if missing:
            return {"ok": False, "msg": f"{', '.join(missing)} is not scheduled right now, so it cannot be swapped. Say 'auto reschedule' first."}
        sa, sb = plan[ca], plan[cb]
        if sa == sb:
            return {"ok": False, "msg": f"{ca} and {cb} already share {sa[0]} {sa[1]} - swapping would change nothing."}
        rest = {c: s for c, s in plan.items() if c not in (ca, cb)}
        for c, dest in ((ca, sb), (cb, sa)):
            ok, why = slot_check(c, dest, rest, rolls, sizes, cap)
            if not ok:
                return {"ok": False, "msg": f"Cannot swap: **{c}** cannot go to {dest[0]} {dest[1]} - {why}."}
            rest[c] = dest
        state["plan"] = rest
        return {"ok": True, "changed": True,
                "msg": f"Swapped **{ca}** ({sa[0]} {sa[1]}) with **{cb}** ({sb[0]} {sb[1]}): {ca} is now {sb[0]} {sb[1]}, {cb} is now {sa[0]} {sa[1]}."}
    if a.get("day_a") and a.get("day_b"):
        da, db = norm_day(a["day_a"]), norm_day(a["day_b"])
        if da == db:
            return {"ok": False, "msg": "Please name two different days to swap."}
        moving = [(c, (db, s)) for c, (d, s) in plan.items() if d == da] + [(c, (da, s)) for c, (d, s) in plan.items() if d == db]
        if not moving:
            return {"ok": False, "msg": f"No exams are scheduled on {da} or {db}, so there is nothing to swap."}
        names = {m[0] for m in moving}
        rest, open_slots = {c: s for c, s in plan.items() if c not in names}, set(active_chrono(state))
        for c, dest in moving:
            if dest not in open_slots:
                return {"ok": False, "msg": f"Cannot swap: {c} would land in {dest[0]} {dest[1]}, which is not an open shift."}
            ok, why = slot_check(c, dest, rest, rolls, sizes, cap)
            if not ok:
                return {"ok": False, "msg": f"Cannot swap {da} and {db}: **{c}** -> {dest[0]} {dest[1]} fails - {why}."}
            rest[c] = dest
        state["plan"] = rest
        return {"ok": True, "changed": True, "msg": f"Swapped all exams between **{da}** and **{db}**: " + "; ".join(f"{c} -> {d[0]} {d[1]}" for c, d in moving) + "."}
    return {"ok": False, "msg": "Tell me which two courses (or two days) to swap, e.g. 'Swap physics and maths'."}


def act_reassign(a, state):
    """Hand one invigilator's duties (optionally one day/slot only) to a named substitute, with clash + leave checks."""
    src_c, dst_c = match_faculty(a.get("from_faculty", ""), state["faculty"]), match_faculty(a.get("to_faculty", ""), state["faculty"])
    if len(src_c) != 1:
        return {"ok": False, "msg": "Which faculty member is being replaced? Use a name from the list: " + ", ".join(state["faculty"])}
    if len(dst_c) != 1:
        return {"ok": False, "msg": "Which faculty member should take over? Use a name from the list: " + ", ".join(state["faculty"])}
    src, dst = src_c[0], dst_c[0]
    if src == dst:
        return {"ok": False, "msg": "The replacement must be a different person."}
    day = norm_day(a.get("day")) if a.get("day") else None
    slot = norm_slot(a.get("slot")) if a.get("slot") else None
    mark = bool(a.get("mark_leave"))
    build_allocations(state)                                   # make duties reflect the current plan
    if mark:
        state["leave"].setdefault(src, set()).add(day or "ALL")
    moved, skipped, scoped = [], [], 0
    for key, invs in list(state["duties"].items()):
        d, s, room = key.split("|")
        if src not in invs or (day and d != day) or (slot and s != slot):
            continue
        scoped += 1
        where = f"{d} {s} / {room}"
        if dst in invs:
            skipped.append(f"{where}: {dst} is already invigilating there")
        elif unavailable(state, dst, d):
            skipped.append(f"{where}: {dst} is on leave on {d}")
        else:
            clash = [k2 for k2, v2 in state["duties"].items() if k2 != key and k2.startswith(f"{d}|{s}|") and dst in v2]
            if clash:
                skipped.append(f"{where}: {dst} is already invigilating {clash[0].split('|')[2]} at that time")
            else:
                invs[invs.index(src)] = dst
                moved.append(where)
    scope = f" on {day}" if day else ""
    if not scoped:
        if mark:
            return {"ok": True, "changed": True, "msg": f"**{src}** marked unavailable{scope}. They had no invigilation duties{scope}, so nothing needed re-assigning."}
        return {"ok": False, "msg": f"**{src}** has no invigilation duties{scope} to hand over."}
    msg = (f"**{src}** marked unavailable{scope}. " if mark else "") + (f"**{dst}** now covers: " + "; ".join(moved) + "." if moved else f"No duty could be given to **{dst}**.")
    if skipped:
        msg += " Could not give: " + "; ".join(skipped) + (" (those rooms were filled by the least-loaded free invigilator)." if mark else ".")
    return {"ok": bool(moved) or mark, "changed": bool(moved) or mark, "msg": msg}


def act_course(a, state):
    c = norm_course(a.get("course"), state)
    _, sizes = get_stats(state)
    if c in state["pending"] or c not in state["plan"]:
        return {"ok": True, "changed": False, "msg": f"**{c}** ({sizes[c]} students) is currently *unscheduled*. Say **'auto reschedule'** to place it."}
    alloc = build_allocations(state)
    d, s = state["plan"][c]
    rooms = sorted({r["room_name"] for r in alloc["room_allocs"].values() for stu in r["students"] if stu["Course"] == c})
    invs = sorted({f for r in alloc["room_allocs"].values() if r["day"] == d and r["slot"] == s for f in r["inv_list"]})
    same = sorted(x for x, sl in state["plan"].items() if sl == (d, s) and x != c)
    return {"ok": True, "changed": False,
            "msg": f"**{c}** - {d} {s}, {sizes[c]} students, rooms: {', '.join(rooms) or '-'}."
                   + (f" Same slot: {', '.join(same)}." if same else " Alone in its slot.")
                   + (f" Invigilators that slot: {', '.join(invs)}." if invs else "")}


def act_stats(a, state):
    alloc = build_allocations(state)
    ra = alloc["room_allocs"]
    sh = state["opts"].get("shifts") or DEFAULT_SHIFTS
    return {"ok": True, "changed": False,
            "msg": f"**{len({e['RollNo'] for e in state['students']})}** students, **{len(get_stats(state)[1])}** courses "
                   f"({len(state['plan'])} scheduled, {len(state['pending'])} unscheduled), **{len({r['room_name'] for r in ra.values()})}** of "
                   f"{len(state['rooms'])} rooms used across **{len({(r['day'], r['slot']) for r in ra.values()})}** exam slots, "
                   f"**{len(state['faculty'])}** invigilators. Shifts per day: {', '.join(f'{d} {sh.get(d, len(SLOTS))}' for d in DAYS)}."}


def act_info(a, state):
    return {"ok": True, "changed": False, "msg": a.get("msg") or HELP_TEXT}


TOOLS = {"cancel": act_cancel, "move": act_move, "leave": act_leave, "place_pending": act_place_pending,
         "lookup_student": act_lookup, "faculty_duties": act_duties, "verify": act_verify, "reset": act_reset,
         "set_shifts": act_shifts, "show_schedule": act_show, "swap": act_swap, "reassign_duty": act_reassign,
         "show_course": act_course, "stats": act_stats, "help": act_info, "info": act_info}


def execute_action(a, state):
    fn = TOOLS.get(a.get("tool"))
    if not fn:
        return {"ok": False, "changed": False, "msg": f"Unknown tool '{a.get('tool')}'."}
    try:
        res = fn(a, state)
        res.setdefault("changed", False)
        return res
    except Exception as e:
        return {"ok": False, "changed": False, "msg": str(e)}


# =====================================================================
#  PLANNERS: LLM (optional) + rule-based fallback
# =====================================================================
# =====================================================================
#  NATURAL-LANGUAGE UNDERSTANDING (typo tolerance, entity extraction, intents)
# =====================================================================
import difflib

DAY_WORDS = {"monday": "Monday", "mon": "Monday", "tuesday": "Tuesday", "tues": "Tuesday", "tue": "Tuesday",
             "wednesday": "Wednesday", "wed": "Wednesday", "thursday": "Thursday", "thurs": "Thursday",
             "thur": "Thursday", "thu": "Thursday", "friday": "Friday", "fri": "Friday"}
WEEKEND_WORDS = {"saturday", "sunday"}
FULL_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
COURSE_ALIASES = {
    "MATH": ["maths", "mathematics", "math"], "CS": ["computer science", "computing", "programming"],
    "PHY": ["physics", "phy"], "CHEM": ["chemistry", "chem"], "ECO": ["economics", "eco"],
    "AI": ["artificial intelligence"], "ENG": ["english"], "BIO": ["biology", "bio"],
    "STAT": ["statistics", "stats"], "DSA": ["data structures"], "DBMS": ["database", "databases"],
    "OS": ["operating systems", "operating system"], "CN": ["networks", "networking"],
}
VOCAB_WORDS = {
    "cancel", "cancelled", "canceled", "reschedule", "postpone", "prepone", "verify", "validate", "morning",
    "afternoon", "schedule", "scheduled", "timetable", "invigilator", "invigilators", "invigilation", "exchange",
    "interchange", "replace", "assign", "unavailable", "leave", "exams", "physics", "maths", "mathematics",
    "chemistry", "economics", "computer", "science", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "shifts", "summary", "status", "conflicts", "pending", "unscheduled", "transfer",
    "substitute", "duties", "students", "faculty", "rooms", "evening", "reassign", "switch", "approve",
    "publish", "statistics", "english", "swap", "where", "which", "check", "explain", "reset", "morning",
}

SPLIT_RE = re.compile(
    r"\s*(?:;|,?\s+and\s+then\b|,?\s+then\b|\bafter that\b|"
    r"\s+and\s+(?=(?:please\s+)?(?:cancel|move|reschedule|re-schedule|postpone|prepone|set|show|list|verify|validate|"
    r"check|swap|exchange|reset|auto|where|find|place|publish|approve|undo)\b))\s*", re.I)

HELP_TEXT = (
    "I'm the exam-scheduling agent. I **plan -> call tools -> verify -> commit or roll back**, and every change stays a DRAFT until you approve it.\n\n"
    "- **Move / reschedule:** 'Move Monday to Wednesday', 'Move physics to Friday afternoon', 'Postpone CS101 by 1 day'\n"
    "- **Swap:** 'Swap physics and maths', 'Swap Monday and Tuesday'\n"
    "- **Cancel + re-slot:** 'Cancel Tuesday then auto reschedule'\n"
    "- **Shifts:** 'Set Monday shifts to 2 and Tuesday to 1' (or just 'Monday 2, Tuesday 1')\n"
    "- **Invigilators:** 'Dr. Suresh Kumar is on leave, give his duty to Dr. Vikram Singh', 'Mehta is on leave on Monday', 'Dr. Vikram Singh' (shows duties)\n"
    "- **Look-ups:** 'Where does 2026BTech005 sit?', 'When is CS101?', 'Show Monday exams', 'How many students?'\n"
    "- **Checks:** 'Verify', 'Any clashes?', and 'Reset' to restore the original schedule.\n\n"
    "Publishing is never done by chat - use the **Approve & Publish** button (administrator approval gate).")


def normalize_text(text, state):
    """Fix typos in command words (never in names) so 'wensday', 'cancle', 'reshedule' still work."""
    protect = set()
    for f in state["faculty"]:
        protect.update(re.findall(r"[a-z]+", f.lower()))
    for e in state["students"]:
        protect.update(re.findall(r"[a-z]+", e["Name"].lower()))

    def fix(m):
        w, lw = m.group(0), m.group(0).lower()
        if len(lw) < 5 or lw in VOCAB_WORDS or lw in protect or lw in DAY_WORDS or lw in WEEKEND_WORDS:
            return w
        hit = difflib.get_close_matches(lw, VOCAB_WORDS, n=1, cutoff=0.82)
        return hit[0] if hit else w
    return re.sub(r"[A-Za-z]+", fix, text)


def day_mentions(text):
    out, weekend = [], False
    for m in re.finditer(r"[a-z]+", text.lower()):
        w = m.group(0)
        if w in DAY_WORDS:
            out.append((m.start(), DAY_WORDS[w]))
        elif w in WEEKEND_WORDS:
            weekend = True
        elif len(w) >= 5:
            hit = difflib.get_close_matches(w, FULL_DAYS, n=1, cutoff=0.78)
            if hit:
                if hit[0] in WEEKEND_WORDS:
                    weekend = True
                else:
                    out.append((m.start(), DAY_WORDS[hit[0]]))
    return out, weekend


def detect_slot(low):
    pm = re.search(r"\b(afternoon|pm|evening|noon|second (?:shift|session|slot)|2nd (?:shift|session|slot)|(?:shift|session|slot) (?:2|two))\b", low)
    am = re.search(r"\b(morning|forenoon|(?<!i )am|first (?:shift|session|slot)|1st (?:shift|session|slot)|(?:shift|session|slot) (?:1|one))\b", low)
    if pm and not am:
        return "Afternoon"
    if am and not pm:
        return "Morning"
    return None


def faculty_mentions(text, faculty):
    """All faculty named in the text, in order of appearance. Returns (resolved list, ambiguous [(token, [names])])."""
    low = text.lower()
    info = []
    for f in faculty:
        toks = [t for t in re.findall(r"[a-z]+", f.lower()) if t not in TITLES and len(t) > 2]
        hits = []
        for t in toks:
            m = re.search(rf"\b{t}\b", low)
            if m:
                hits.append((t, m.start()))
        if hits:
            info.append({"f": f, "hits": hits, "full": len(hits) == len(toks)})
    chosen, used = [], set()
    for x in info:
        if x["full"]:
            chosen.append((min(p for _, p in x["hits"]), x["f"]))
            used.update(t for t, _ in x["hits"])
    by_tok = defaultdict(list)
    for x in info:
        if not x["full"]:
            for t, p in x["hits"]:
                if t not in used:
                    by_tok[t].append((p, x["f"]))
    ambiguous = []
    for t, lst in by_tok.items():
        names = sorted({f for _, f in lst})
        if len(names) == 1:
            if all(names[0] != c[1] for c in chosen):
                chosen.append((lst[0][0], names[0]))
        else:
            ambiguous.append((t, names))
    chosen.sort()
    return [f for _, f in chosen], ambiguous


def course_mentions(low, state):
    """Course codes ('CS101', 'cs 101') and nicknames ('physics', 'maths'). Returns (ordered codes, ambiguous)."""
    _, sizes = get_stats(state)
    prefixes, found, ambiguous = defaultdict(list), {}, []
    for code in sizes:
        m = re.match(r"([A-Za-z]+)(\d.*)", code)
        pre, num = (m.group(1), m.group(2)) if m else (code, "")
        prefixes[pre.upper()].append(code)
        hit = re.search(rf"\b{re.escape(pre.lower())}\s?-?{re.escape(num.lower())}\b", low)
        if hit:
            found[code] = min(found.get(code, 10 ** 6), hit.start())
    for pre, codes in prefixes.items():
        for al in {pre.lower()} | set(COURSE_ALIASES.get(pre, [])):
            for hit in re.finditer(rf"\b{re.escape(al)}\b", low):
                if re.match(r"\s?-?\d", low[hit.end():]):
                    continue
                if len(codes) == 1:
                    found[codes[0]] = min(found.get(codes[0], 10 ** 6), hit.start())
                else:
                    ambiguous.append((al, sorted(codes)))
    ambiguous = [(al, cs) for al, cs in ambiguous if not any(c in found for c in cs)]
    return [c for c, _ in sorted(found.items(), key=lambda kv: kv[1])], ambiguous


def student_name_mentions(low, state):
    names = {e["Name"].lower() for e in state["students"]}
    return [n for n in names if re.search(rf"\b{re.escape(n)}\b", low)]


def pick_roles(low, fac):
    """Who is absent and who substitutes, from phrases like 'A on leave, give duty to B' / 'B will cover for A'."""
    a, b = fac[0], fac[1]
    if re.search(r"\binstead of\b|\b(cover|covers|covering|take|takes|taking|substitute|substitutes)\b", low):
        return b, a                      # "B will cover for A" / "assign B instead of A" -> A is out, B is in
    return a, b                          # "A is on leave give his duty to B" / "replace A with B"


def safe_rule_plan(text, state):
    try:
        return rule_plan(text, state)
    except Exception as e:                # the parser must never crash the chat
        return {"clarify": f"I couldn't parse that ({type(e).__name__}). Please rephrase, or type 'help' to see what I can do."}


def rule_plan(text, state):
    S = st.session_state
    ctx = dict(S.get("last_ctx") or {})
    _, sizes = get_stats(state)
    norm = normalize_text(text, state)
    t0 = norm.lower()
    num_words = {"zero": "0", "none": "0", "no": "0", "one": "1", "single": "1", "two": "2", "double": "2", "both": "2", "a": "1", "three": "3", "four": "4"}

    # ---------- whole-sentence shift configuration: "Monday 2, Tuesday 1" ----------
    t = re.sub(r"\b(" + "|".join(num_words) + r")\b", lambda m: num_words[m.group(1)], t0)
    bare = re.sub(DAY_RE + r"|\d|\band\b|\bset\b|[\W_]+", "", t0)
    blocked = re.search(r"\b(move|cancel|reschedule|postpone|prepone|swap|exchange|replace|show|list|what|which|how many|when|where)\b", t0)
    if re.search(r"\d", t) and (re.search(r"\bshifts?\b|\bsessions? per day\b", t0) or not bare) and not blocked:
        found = {}
        m_all = re.search(r"\b(all|every|each)\b\D{0,20}?(\d)", t)
        if m_all:
            found = {d: int(m_all.group(2)) for d in DAYS}
        for m in re.finditer(DAY_RE + r"\D{0,25}?(\d)", t):
            if m.group(1)[:3] in ("sat", "sun"):
                return {"clarify": "Exams run Monday-Friday only. Which weekday do you mean?"}
            found[norm_day(m.group(1))] = int(m.group(2))
        if not found:
            return {"clarify": "Tell me the day and shift count, e.g. 'Monday 2, Tuesday 1' (0 = no exams, 1 = Morning only, 2 = Morning + Afternoon)."}
        return {"reasoning": "Parsed a shift-configuration request.", "actions": [{"tool": "set_shifts", "shifts": found}]}

    clauses = [c.strip(" ,.!?") for c in SPLIT_RE.split(norm) if c and c.strip(" ,.!?")]
    expanded = []
    for cl in clauses:                    # "move A to X and B to Y" -> two clauses
        parts = [p for p in re.split(r"\s+and\s+", cl, flags=re.I) if p.strip()]
        if len(parts) > 1 and all(re.search(r"\bto\b", p, re.I) for p in parts):
            expanded.extend(parts)
        else:
            expanded.append(cl)
    if not expanded:
        return {"clarify": "Type a command, e.g. 'Move Monday to Wednesday', or 'help' to see everything I can do."}

    leave_re = r"\b(leave|absent|unavailable|not available|sick|ill|cannot come|can't come|cant come|won't be|wont be|on holiday|medical|emergency|away)\b"
    give_re = r"\b(give|assign|hand|handover|transfer|reassign|allocate|cover|covers|covering|substitute|instead|replace|replaces|replacing|take|takes|taking|swap|put)\b"
    actions, last_verb = [], None

    for cl in expanded:
        low = cl.lower()
        days_pos, weekend = day_mentions(low)
        days = [d for _, d in days_pos]
        courses, amb_c = course_mentions(low, state)
        rolls = re.findall(r"\b(20\d{2}[a-z]+\d+)\b", low)
        names = [] if rolls else student_name_mentions(low, state)
        fac, amb_f = ([], []) if (rolls or names) else faculty_mentions(low, state["faculty"])
        slot = detect_slot(low)
        if courses:
            ctx["course"] = courses[-1]
        if fac:
            ctx["faculty"] = fac[-1]

        if weekend:
            return {"clarify": "Exams run Monday-Friday only. Which weekday do you mean?"}
        if amb_c:
            return {"clarify": f"'{amb_c[0][0]}' matches more than one course ({', '.join(amb_c[0][1])}). Which one do you mean?"}
        if amb_f:
            return {"clarify": f"'{amb_f[0][0]}' matches more than one faculty member ({', '.join(amb_f[0][1])}). Which one do you mean?"}

        # ---------- housekeeping ----------
        if re.search(r"\b(reset|restore|revert|undo|start over|original)\b", low):
            actions.append({"tool": "reset"}); last_verb = None; continue
        if re.fullmatch(r"\W*(hi+|hello|hey|yo|thanks?|thank you|thx|ok|okay|cool|great|nice|good (?:morning|afternoon|evening))\W*", low):
            actions.append({"tool": "info", "msg": "Hello! I'm ready. Try 'Show Monday exams', 'Swap physics and maths', 'Dr. Suresh Kumar is on leave, give his duty to Dr. Vikram Singh', or type **help**."}); continue
        if re.search(r"\b(help|what can you do|commands|how do i use|how to use|capabilities|examples)\b", low):
            actions.append({"tool": "help"}); continue
        if re.search(r"\b(publish|approve|approval|go live|finali[sz]e)\b", low):
            actions.append({"tool": "info", "msg": "I can't publish from chat - publishing is a human approval gate. Review the checks, then use the **Approve & Publish** button (it is blocked while any check fails)."}); continue

        # ---------- swap two courses / two days ----------
        swap_any = re.search(r"\b(swap|swaps|swapping|exchange|interchange|switch|replace|replaces|trade)\b", low)
        swap_strict = re.search(r"\b(swap|swaps|swapping|exchange|interchange|trade)\b", low)
        if swap_any and len(courses) >= 2:
            actions.append({"tool": "swap", "course_a": courses[0], "course_b": courses[1]}); last_verb = "move"; continue
        if swap_strict and len(days) >= 2 and not courses:
            actions.append({"tool": "swap", "day_a": days[0], "day_b": days[1]}); last_verb = "move"; continue

        # ---------- student look-up ----------
        seat_words = re.search(r"\b(where|seat|seats|sit|sits|sitting|room|find|lookup|look up|locate|my)\b", low)
        if rolls:
            actions.append({"tool": "lookup_student", "roll": rolls[0]}); last_verb = "show"; continue
        if names and seat_words:
            actions.append({"tool": "lookup_student", "name": names[0]}); last_verb = "show"; continue

        # ---------- faculty: leave / substitution / duties ----------
        if fac:
            wants_leave = bool(re.search(leave_re, low))
            day = days[0] if days else None
            if len(fac) >= 2:
                if wants_leave or re.search(give_re, low):
                    absent, sub = pick_roles(low, fac)
                    actions.append({"tool": "reassign_duty", "from_faculty": absent, "to_faculty": sub,
                                    "day": day, "slot": slot, "mark_leave": wants_leave})
                    last_verb = "leave"; ctx["faculty"] = sub; continue
                return {"clarify": f"I see {fac[0]} and {fac[1]}. What should I do? e.g. '{fac[0]} is on leave, give his duty to {fac[1]}'."}
            f1 = fac[0]
            if wants_leave:
                if re.search(r"\b(give|assign|hand|transfer|reassign)\b.*\b(to|with)\b", low):
                    return {"clarify": f"I found {f1}, but I couldn't recognise who should take over. Please use a name from the faculty list: {', '.join(state['faculty'])}."}
                actions.append({"tool": "leave", "faculty": f1, "day": day}); last_verb = "leave"; continue
            if re.search(give_re, low) and re.search(r"\b(duty|duties|invigilat)", low):
                return {"clarify": f"Who should take over {f1}'s duties? e.g. 'Give {f1}'s duties to <another faculty member>'."}
            actions.append({"tool": "faculty_duties", "faculty": f1}); last_verb = "show"; continue

        # ---------- re-slot cancelled / unscheduled courses ----------
        auto_re = re.search(r"\b(auto|automatic|automatically|pending|unscheduled|cancelled|canceled|unplaced|leftover)\b", low)
        sched_re = re.search(r"\b(re-?schedule|schedule|place|fix|resolve|slot|re-?slot|assign|allocate|handle|sort)\b", low)
        if auto_re and sched_re and not courses:
            actions.append({"tool": "place_pending"}); last_verb = None; continue
        if re.search(r"\b(re-?schedule|re-?slot|place|schedule)\b", low) and not (courses or days) \
                and not re.search(r"\b(move|shift|postpone|prepone)\b", low):
            if state["pending"] or last_verb == "cancel":
                actions.append({"tool": "place_pending"}); last_verb = None; continue
            return {"clarify": "Nothing is waiting to be rescheduled. Tell me what to move, e.g. 'Move physics to Friday morning'."}

        # ---------- verify ----------
        if re.search(r"\b(verify|validate|validation|conflicts?|clash|clashes|clash-?free|issues|errors?|problems?|status)\b", low):
            actions.append({"tool": "verify"}); last_verb = "show"; continue

        cancel_re = re.search(r"\b(cancel|cancels|cancelled|canceled|remove|delete|drop|skip|scrap|abort|call off|no exams?)\b", low)
        move_re = re.search(r"\b(move|moves|moving|re-?schedule|postpone|prepone|delay|advance|relocate|put|push|pull)\b", low)
        shift_move = bool(re.search(r"\bshift\b", low) and re.search(r"\bto\b", low)
                          and not re.search(r"\b(morning|afternoon|first|second)\s+shift\b|\bshift\s+(\d|one|two)\b", low))
        change_move = bool(re.search(r"\bchange\b", low) and re.search(r"\bto\b", low) and (courses or days))
        is_cancel, is_move = bool(cancel_re), bool(move_re or shift_move or change_move)

        # ---------- questions / read-only look-ups ----------
        q_start = re.match(r"\s*(?:please\s+|can you\s+|could you\s+)?(what|which|when|where|how|who|show|list|display|view|tell|give me|is|are|do|does)\b", low)
        show_words = re.search(r"\b(show|list|display|view|see|schedule|scheduled|timetable|exams?|papers?|shifts?)\b", low)
        if (q_start and not (is_move or is_cancel)) or (not (is_move or is_cancel) and (courses or days) and show_words):
            if courses:
                for c in courses:
                    actions.append({"tool": "show_course", "course": c})
            elif re.search(r"\bhow many\b", low) and re.search(r"\b(students|courses|rooms|invigilators|faculty|sessions|slots|seats|exams)\b", low) and not days:
                actions.append({"tool": "stats"})
            else:
                actions.append({"tool": "show_schedule", "day": days[0] if days else None})
            last_verb = "show"; continue
        if re.search(r"\b(summary|summarize|summarise|overview|report)\b", low) and not (courses or days):
            actions += [{"tool": "stats"}, {"tool": "verify"}]; last_verb = "show"; continue
        if re.search(r"\b(check|explain)\b", low) and not (courses or days):
            actions.append({"tool": "verify"}); last_verb = "show"; continue

        # lone entity after a verb: "cancel Monday and Tuesday", "move physics to X and maths to Y"
        if not (is_cancel or is_move) and (days or courses):
            if last_verb == "cancel":
                is_cancel = True
            elif last_verb == "move":
                is_move = True

        # ---------- cancel ----------
        if is_cancel:
            if not days and not courses:
                return {"clarify": "Which day or course should I cancel? e.g. 'Cancel Monday' or 'Cancel physics'."}
            if courses:
                for c in courses:
                    actions.append({"tool": "cancel", "course": c})
            else:
                busy = [d for d in days if any(dd == d for dd, _ in state["plan"].values())]
                for d in (busy or days[:1]):
                    actions.append({"tool": "cancel", "day": d, "slot": slot})
            last_verb = "cancel"; continue

        # ---------- move / reschedule / postpone ----------
        if is_move:
            if not courses and re.search(r"\b(it|that|this|same)\b", low) and ctx.get("course") in sizes:
                courses = [ctx["course"]]
            m_by = re.search(r"\bby\s+(\d+|one|two|three|four|a)\s+days?\b", low)
            if m_by:
                n = int(num_words.get(m_by.group(1), m_by.group(1)))
                direction = -1 if re.search(r"\b(prepone|advance|earlier|back)\b", low) else 1
                if courses:
                    for c in courses:
                        cur = state["plan"].get(c)
                        if not cur:
                            return {"clarify": f"{c} is not scheduled right now. Say 'auto reschedule' first."}
                        idx = DAYS.index(cur[0]) + direction * n
                        if not 0 <= idx < len(DAYS):
                            return {"clarify": f"Moving {c} by {n} day(s) would fall outside Monday-Friday. Which day do you want instead?"}
                        actions.append({"tool": "move", "course": c, "to_day": DAYS[idx], "to_slot": None})
                elif days:
                    idx = DAYS.index(days[0]) + direction * n
                    if not 0 <= idx < len(DAYS):
                        return {"clarify": f"Moving {days[0]} by {n} day(s) would fall outside Monday-Friday. Which day do you want instead?"}
                    actions.append({"tool": "move", "from_day": days[0], "to_day": DAYS[idx], "to_slot": None})
                else:
                    return {"clarify": "Which day or course should I move?"}
                last_verb = "move"; continue

            m_to = re.search(r"\bto\b", low)
            before, after = (low[:m_to.start()], low[m_to.end():]) if m_to else (low, "")
            fs, ts = (detect_slot(before), detect_slot(after)) if m_to else (None, slot)
            d_before = [d for _, d in day_mentions(before)[0]]
            d_after = [d for _, d in day_mentions(after)[0]]
            if courses:
                dest = (d_after[0] if d_after else None) if m_to else (days[-1] if days else None)
                if not dest and not ts:
                    alts = valid_slots(courses[0], state["plan"], state, limit=5)
                    return {"clarify": f"Where should **{courses[0]}** go? Clash-free options right now: {', '.join(alts) if alts else 'none found'}. e.g. 'Move {courses[0]} to {alts[0] if alts else 'Friday Morning'}'."}
                for c in courses:
                    actions.append({"tool": "move", "course": c, "to_day": dest, "to_slot": ts})
                last_verb = "move"; continue
            if days or m_to:
                if m_to and re.search(r"\bfrom\b", after):
                    dst = d_after[0] if d_after else None
                    src = d_after[1] if len(d_after) > 1 else None
                elif m_to:
                    src = d_before[0] if d_before else (ctx.get("day_src"))
                    dst = d_after[0] if d_after else None
                else:
                    if len(days) < 2:
                        return {"clarify": f"Where should {days[0] if days else 'that day'}'s exams move to? e.g. 'Move {days[0] if days else 'Monday'} to Wednesday'."}
                    src, dst = days[0], days[1]
                    if re.search(r"\bfrom\b", low) and re.search(r"\bto\b", low):
                        pass
                if not src or not dst:
                    return {"clarify": "Please give both the source day and the destination day, e.g. 'Move Monday to Wednesday'."}
                if src == dst:
                    return {"clarify": f"{src} is both the source and the destination - which day should it move to?"}
                actions.append({"tool": "move", "from_day": src, "from_slot": fs, "to_day": dst, "to_slot": ts})
                last_verb = "move"; continue
            return {"clarify": "What should I move? e.g. 'Move Monday to Wednesday' or 'Move physics to Friday afternoon'."}

        # ---------- lone entity: just show it ----------
        if courses:
            actions.append({"tool": "show_course", "course": courses[0]}); last_verb = "show"; continue
        if days:
            actions.append({"tool": "show_schedule", "day": days[0]}); last_verb = "show"; continue

        noticed = []
        if courses: noticed.append("course " + ", ".join(courses))
        if days: noticed.append("day " + ", ".join(days))
        return {"clarify": "I couldn't map that to an action" + (f" (I noticed {'; '.join(noticed)})" if noticed else "")
                + ". Try 'Swap physics and maths', 'Move CS101 to Friday afternoon', 'Cancel Monday then auto reschedule', "
                  "'Set Monday shifts to 2', '<Faculty> is on leave, give duty to <Faculty>', 'Where does 2026BTech005 sit?', or type **help**."}

    S["last_ctx"] = ctx
    if not actions:
        return {"clarify": "I couldn't find an action in that. Type **help** to see what I can do."}
    return {"reasoning": f"Parsed with the rule-based planner ({len(actions)} step(s)).", "actions": actions}


class LLMFatal(Exception):
    """Errors that retrying cannot fix (bad key, bad model name, blocked request)."""


def call_llm(cfg, system, user, retries=1):
    last = None
    for attempt in range(retries + 1):
        try:
            if cfg["provider"] == "Anthropic":
                r = requests.post("https://api.anthropic.com/v1/messages",
                                  headers={"x-api-key": cfg["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                  json={"model": cfg["model"], "max_tokens": 700, "temperature": 0, "system": system,
                                        "messages": [{"role": "user", "content": user}]}, timeout=15)
            else:
                gen = {"temperature": 0, "maxOutputTokens": 900, "responseMimeType": "application/json"}
                if "2.5-flash" in cfg["model"]:
                    gen["thinkingConfig"] = {"thinkingBudget": 0}      # fast, deterministic planning
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['model']}:generateContent",
                                  headers={"x-goog-api-key": cfg["key"], "content-type": "application/json"},
                                  json={"systemInstruction": {"parts": [{"text": system}]},
                                        "contents": [{"role": "user", "parts": [{"text": user}]}],
                                        "generationConfig": gen}, timeout=15)
            if r.status_code in (400, 401, 403, 404):
                raise LLMFatal(f"HTTP {r.status_code}: {r.text[:160]}")
            r.raise_for_status()
            if cfg["provider"] == "Anthropic":
                return r.json()["content"][0]["text"]
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except LLMFatal:
            raise
        except Exception as e:
            last = e
            time.sleep(0.6 * (attempt + 1))      # retry with back-off (timeouts, 429, 5xx)
    raise RuntimeError(f"LLM call failed after {retries + 1} tries: {last}")


LLM_SYSTEM = """You are the planning module of an exam-scheduling agent. Convert the administrator's request into tool calls.
Reply with ONLY JSON: {"reasoning": "<one short sentence>", "actions": [ ... ]} or {"clarify": "<one question>"} if the request is truly ambiguous.
Tools (use only these, never invent data):
 {"tool":"cancel","day":"Monday","slot":"Morning|Afternoon|null","course":"CS101|null"}
 {"tool":"move","course":"CS101|null","from_day":"Monday|null","from_slot":null,"to_day":"Friday","to_slot":"Morning|Afternoon|null"}
 {"tool":"swap","course_a":"PHY101","course_b":"MATH101"}   (exchange the slots of two courses)   or   {"tool":"swap","day_a":"Monday","day_b":"Tuesday"}
 {"tool":"leave","faculty":"<name from list>","day":"Monday|null"}   (faculty unavailable; replacements are auto-picked)
 {"tool":"reassign_duty","from_faculty":"<name>","to_faculty":"<name>","day":"Monday|null","slot":null,"mark_leave":true|false}   (give A's duties to a named B)
 {"tool":"place_pending"}   (re-slot cancelled/unscheduled courses)
 {"tool":"lookup_student","roll":"<roll no>"}  or  {"tool":"lookup_student","name":"<full name>"}
 {"tool":"faculty_duties","faculty":"<name>"}
 {"tool":"show_course","course":"CS101"}   {"tool":"show_schedule","day":"Monday|null"}   {"tool":"stats"}
 {"tool":"set_shifts","shifts":{"Monday":2,"Tuesday":1}}   (shifts per day: 0 = no exams, 1 = Morning only, 2 = Morning + Afternoon)
 {"tool":"verify"}   {"tool":"reset"}   {"tool":"help"}
Exam days are Monday-Friday, slots Morning/Afternoon; only the shifts in "shifts_per_day" exist. Multi-step requests become several actions in order.
Course nicknames: map "physics"/"maths"/"chemistry"/"economics"/"AI"/"computer science" to the matching code that exists in State (e.g. PHY101, MATH101). If two codes fit, ask a clarify question.
Faculty: always copy the exact name from State.faculty. "replace the physics exam with maths and the maths exam with physics" means a swap.
Never publish or approve - that is a human-only button; answer with {"tool":"help"} style info instead.
Examples:
 "cancel tuesday then reschedule" -> {"reasoning":"Cancel Tuesday then re-slot","actions":[{"tool":"cancel","day":"Tuesday","slot":null,"course":null},{"tool":"place_pending"}]}
 "monday 2 shifts, tuesday only 1" -> {"reasoning":"Set shifts","actions":[{"tool":"set_shifts","shifts":{"Monday":2,"Tuesday":1}}]}
 "what is on friday?" -> {"reasoning":"Read-only lookup","actions":[{"tool":"show_schedule","day":"Friday"}]}
 "swap physics and maths" -> {"reasoning":"Exchange two courses","actions":[{"tool":"swap","course_a":"PHY101","course_b":"MATH101"}]}
 "Dr. Suresh Kumar is on leave give his duty to Dr. Vikram Singh" -> {"reasoning":"Suresh out, Vikram covers","actions":[{"tool":"reassign_duty","from_faculty":"Dr. Suresh Kumar","to_faculty":"Dr. Vikram Singh","day":null,"slot":null,"mark_leave":true}]}
 "Dr. Vikram Singh" -> {"reasoning":"Show his duties","actions":[{"tool":"faculty_duties","faculty":"Dr. Vikram Singh"}]}
Never invent course codes, roll numbers or faculty names - use only those present in the State."""


def state_summary(state):
    plan = {c: f"{d} {s}" for c, (d, s) in state["plan"].items()}
    return json.dumps({"scheduled": plan, "unscheduled": state["pending"], "faculty": state["faculty"],
                       "shifts_per_day": state["opts"].get("shifts") or DEFAULT_SHIFTS,
                       "on_leave": {f: sorted(v) for f, v in state["leave"].items()}})


def extract_json(raw):
    try:
        return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        return None


def plan_with_engine(prompt, state, trace, feedback=None):
    cfg = st.session_state.get("llm_cfg")
    if cfg and cfg.get("key"):
        try:
            hist = st.session_state.get("chat_history", [])[-5:-1]
            ctx = "\n".join(f"{h['role']}: {str(h['text'])[:200]}" for h in hist)
            user = (f"State: {state_summary(state)}\n" + (f"Recent conversation:\n{ctx}\n" if ctx else "")
                    + f"Request: {prompt}" + (f"\nYour previous plan failed: {feedback}. Fix it." if feedback else ""))
            data = extract_json(call_llm(cfg, LLM_SYSTEM, user))
            if not isinstance(data, dict):
                raise ValueError("planner returned invalid JSON")
            acts = data.get("actions")
            if acts is not None and not (isinstance(acts, list) and all(isinstance(x, dict) for x in acts)):
                raise ValueError("planner returned malformed actions")
            if not acts and not data.get("clarify"):
                raise ValueError("planner returned neither actions nor a question")
            if data.get("clarify"):                      # the deterministic planner may still understand it
                rp = safe_rule_plan(prompt, state)
                if rp.get("actions"):
                    trace.append("ℹ️ LLM asked for clarification, but the rule-based planner could resolve the request.")
                    return rp, "Rule-based"
            return data, f"LLM ({cfg['provider']})"
        except Exception as e:
            trace.append(f"⚠️ LLM planner unavailable ({e}). Falling back to the rule-based planner.")
    return safe_rule_plan(prompt, state), "Rule-based"


def commit(new_state, reason):
    S = st.session_state
    was_published = S["status"] == "PUBLISHED"
    S["state"] = new_state
    S["status"] = "DRAFT"
    S["version"] += 1
    refresh()
    log("COMMIT", reason, "agent")
    return was_published


def _run_agent(prompt, actor="Admin", forced=None):
    S = st.session_state
    trace, final, feedback = [], "", None
    baseline = integrity_fails(S["checks"])
    log("COMMAND", prompt, actor)
    for attempt in (1, 2):
        data, engine = (forced, "Sidebar control") if forced else plan_with_engine(prompt, S["state"], trace, feedback)
        if data.get("clarify"):
            final = "🤔 " + data["clarify"]
            trace.append(f"❓ Clarification needed ({engine}): {data['clarify']}")
            break
        actions = data.get("actions") or []
        trace.append(f"🧠 Plan ({engine}): {data.get('reasoning', '')} -> {len(actions)} step(s)")
        log("AGENT_PLAN", json.dumps(actions), "agent")
        if not actions:
            final = "ℹ️ I did not find an action to perform in that request."
            break
        work, results, failed = copy.deepcopy(S["state"]), [], None
        for a in actions:
            trace.append(f"🔧 Tool call: {json.dumps(a)}")
            res = execute_action(a, work)
            trace.append(f"   {'✅' if res['ok'] else '❌'} {res['msg']}")
            log("TOOL_CALL", f"{json.dumps(a)} -> {'OK' if res['ok'] else 'FAIL'}: {res['msg']}", "agent")
            results.append(res)
            if not res["ok"]:
                failed = res
                break
        if failed:
            if engine.startswith("LLM") and attempt == 1:
                feedback = failed["msg"]
                trace.append("♻️ Re-planning with the failure as feedback...")
                continue
            final = f"❌ **No changes applied.** {failed['msg']}"
            break
        alloc = build_allocations(work)
        checks = run_validation(work, alloc)
        newfails = integrity_fails(checks) - baseline
        trace.append(f"🔍 Verification: {sum(1 for c in checks if c['Status'].startswith('✅'))}/{len(checks)} checks passed")
        if newfails:
            log("ROLLBACK", ", ".join(newfails), "agent")
            detail = "; ".join(f"{c['Check']}: {c['Detail']}" for c in checks if c["Check"] in newfails)
            if engine.startswith("LLM") and attempt == 1:
                feedback = detail
                trace.append("♻️ Verification failed - re-planning...")
                continue
            final = f"⛔ **Rolled back - this change would break constraints.** {detail}"
            break
        msgs = "\n\n".join("✅ " + r["msg"] for r in results)
        if any(r["changed"] for r in results):
            was_pub = commit(work, prompt)
            final = msgs + "\n\n📝 Schedule is now **DRAFT** - re-approval required before publishing." + (" (Previous publication revoked.)" if was_pub else "")
        else:
            final = msgs
        break
    S["chat_history"].append({"role": "assistant", "text": final, "trace": trace})
    log("RESPONSE", final.replace("\n", " "), "agent")


def run_agent(prompt, actor="Admin", forced=None):
    """Crash-proof wrapper: any unexpected error becomes a chat message and the schedule stays untouched."""
    try:
        _run_agent(prompt, actor, forced)
    except Exception as e:
        st.session_state["chat_history"].append({
            "role": "assistant", "trace": [f"💥 {type(e).__name__}: {e}"],
            "text": f"⚠️ Something unexpected happened while handling that ({type(e).__name__}: {e}). **Nothing was changed.** Please rephrase, or type 'help'."})
        log("ERROR", f"{type(e).__name__}: {e}", "agent")


# =====================================================================
#  VISUAL MAPS (in-app) + PDF
# =====================================================================
def room_map_html(r, color_of):
    legend = "".join(f"<span style='display:inline-block;margin-right:10px;font-size:11px;color:#cbd5e1'><span style='display:inline-block;width:10px;height:10px;border-radius:2px;background:{col};margin-right:4px'></span>{c}</span>"
                     for c, col in color_of.items())
    parts = [f"<div style='margin:4px 0 8px'>{legend}</div><div style='text-align:center;background:#1e293b;border:1px solid #475569;border-radius:6px;padding:5px;font-size:12px;color:#f8fafc;margin-bottom:6px'>FRONT - BLACKBOARD / TEACHER PODIUM</div>"]

    def cell(e):
        if e is None:
            return "<div style='min-width:96px;padding:4px 6px;border-radius:6px;font-size:10px;border:1px dashed #475569;color:#64748b'>vacant</div>"
        col = color_of.get(e["Course"], "#64748b")
        return (f"<div style='min-width:96px;padding:4px 6px;border-radius:6px;font-size:10px;background:{col}33;border:1px solid {col};color:#e2e8f0'>"
                f"<b>{xml_escape(e['RollNo'])}</b><br>{xml_escape(e['Name'].split()[0])} | {e['Course']}</div>")

    for w, benches in r["grid"].items():
        parts.append(f"<div style='margin:8px 0 2px;color:#94a3b8;font-size:12px;font-weight:600'>{w if w != 'Straight' else 'Straight rows'}</div>")
        for b, row in enumerate(benches, 1):
            label = f"Row {b}" if w == "Straight" else f"Bench {b}"
            parts.append(f"<div style='display:flex;gap:4px;margin-bottom:4px;align-items:center'><span style='width:58px;color:#64748b;font-size:11px'>{label}</span>{''.join(cell(e) for e in row)}</div>")
    return "".join(parts)


def generate_room_pdf(room_allocs, approved_at=""):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=25, leftMargin=25, topMargin=25, bottomMargin=25)
    story, styles = [], getSampleStyleSheet()
    esc = lambda s: xml_escape(str(s))
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=12, textColor=colors.HexColor('#1f2937'), spaceAfter=2)
    subtitle_style = ParagraphStyle('SubTitleStyle', parent=styles['Normal'], fontSize=8.5, textColor=colors.HexColor('#4b5563'), spaceAfter=8)
    section_style = ParagraphStyle('SectionStyle', parent=styles['Heading2'], fontSize=10, textColor=colors.HexColor('#4f46e5'), spaceBefore=6, spaceAfter=3)
    cell_style = ParagraphStyle('CellStyle', parent=styles['Normal'], fontSize=6.5, leading=8, alignment=1)
    aisle_style = ParagraphStyle('AisleCellStyle', parent=styles['Normal'], fontSize=7, fontName='Helvetica-Bold', textColor=colors.HexColor('#94a3b8'), alignment=1)
    board_style = ParagraphStyle('BoardTextStyle', parent=styles['Normal'], fontSize=9.5, leading=12, alignment=1, textColor=colors.HexColor('#f8fafc'))
    wing_style = ParagraphStyle('WingTitleStyle', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#1e3a8a'), spaceBefore=4, spaceAfter=2)

    def sc(e):
        if not e:
            return Paragraph("<i>(Vacant)</i>", cell_style)
        return Paragraph(f"<b>{esc(e['RollNo'])}</b><br/>{esc(e['Name'].split()[0])} | {esc(e['Course'])}", cell_style)

    base_grid = [('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                 ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#3b82f6')),
                 ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                 ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e0e7ff')),
                 ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, 0), 7.5)]

    items = list(room_allocs.items())
    for idx, (room_key, r) in enumerate(items):
        story.append(Paragraph(f"<b>JKLU CLASSROOM FLOOR SEATING MAP ({esc(r['layout'].upper())})</b>", title_style))
        story.append(Paragraph(f"<b>Room &amp; Slot:</b> {esc(room_key)} | <b>Invigilators:</b> {esc(r['invigilators'])} | <b>Total Students:</b> {len(r['students'])} | <b>Approved:</b> {esc(approved_at)}", subtitle_style))
        t_board = Table([[Paragraph("<b>[ FRONT: CLASSROOM BLACKBOARD / TEACHER PODIUM ]</b>", board_style)]], colWidths=[550])
        t_board.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#1e293b')), ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                                     ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                                     ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#475569'))]))
        story += [t_board, Spacer(1, 6), Paragraph("<b>Top-Down Floor Plan (adjacent seats hold different courses):</b>", section_style)]

        for w, benches in r["grid"].items():
            if w == "Straight":
                data = [["Row", "Seat Box 1", "Seat Box 2", "Seat Box 3", "Seat Box 4"]]
                for b, row in enumerate(benches, 1):
                    data.append([f"Row {b}"] + [sc(row[k]) if k < len(row) else "-" for k in range(4)])
                t = Table(data, colWidths=[70, 120, 120, 120, 120], repeatRows=1)
                t.setStyle(TableStyle(base_grid))
            elif w == "Middle Wing":
                story.append(Paragraph(f"<b>{w} (Equally Distributed Bench Seating)</b>", wing_style))
                data, extra = [["Bench", "Seat Box 1", "Seat Box 2", "Seat Box 3"]], []
                for b, row in enumerate(benches, 1):
                    ri = len(data)
                    if len(row) == 1:
                        data.append([f"Bench {b}", sc(row[0]), "", ""])
                        extra.append(('SPAN', (1, ri), (3, ri)))
                    elif len(row) == 2:
                        data.append([f"Bench {b}", sc(row[0]), Paragraph("<b>[ GAP ]</b>", aisle_style), sc(row[1])])
                        extra.append(('BACKGROUND', (2, ri), (2, ri), colors.HexColor('#f1f5f9')))
                    else:
                        data.append([f"Bench {b}"] + [sc(x) for x in row[:3]])
                t = Table(data, colWidths=[70, 160, 160, 160], repeatRows=1)
                t.setStyle(TableStyle(base_grid + extra))
            else:
                story.append(Paragraph(f"<b>{w} (Side Arc Layout with Walkway Aisle Gap)</b>", wing_style))
                data = [["Bench", "Left Section Seats", "Walkway Aisle / Gap", "Right Section Seats"]]
                for b, row in enumerate(benches, 1):
                    data.append([f"Bench {b}", sc(row[0]), Paragraph("<b>[ AISLE / GAP ]</b>", aisle_style), sc(row[1]) if len(row) > 1 else "-"])
                t = Table(data, colWidths=[70, 200, 80, 200], repeatRows=1)
                t.setStyle(TableStyle(base_grid + [('BACKGROUND', (2, 1), (2, -1), colors.HexColor('#f1f5f9'))]))
            story += [t, Spacer(1, 4)]

        story.append(PageBreak())
        story.append(Paragraph("<b>JKLU MASTER ATTENDANCE ROLL CALL &amp; SIGNATURE SHEET</b>", title_style))
        story.append(Paragraph(f"<b>Room &amp; Slot:</b> {esc(room_key)} | <b>Invigilators:</b> {esc(r['invigilators'])} | <b>Total Seated:</b> {len(r['students'])}", subtitle_style))
        story.append(Spacer(1, 4))
        td = [["S.No", "Roll Number", "Student Name", "Course", "Box Position", "Signature"]]
        for i, s in enumerate(r["students"]):
            td.append([str(i + 1), s["RollNo"], s["Name"], s["Course"], s["Position"], ""])
        tm = Table(td, colWidths=[30, 90, 115, 70, 140, 65], repeatRows=1)
        tm.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2563eb')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                                ('ALIGN', (0, 0), (-1, -1), 'LEFT'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('FONTSIZE', (0, 0), (-1, 0), 8),
                                ('BOTTOMPADDING', (0, 0), (-1, 0), 4), ('TOPPADDING', (0, 0), (-1, 0), 4),
                                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f9fafb')),
                                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')), ('FONTSIZE', (0, 1), (-1, -1), 7.5)]))
        story.append(tm)
        if idx < len(items) - 1:
            story.append(PageBreak())
    if not items:
        story.append(Paragraph("No sessions to print.", title_style))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def course_table(state, alloc):
    rows = []
    where = defaultdict(set)
    for r in alloc["room_allocs"].values():
        for s in r["students"]:
            where[s["Course"]].add(r["room_name"])
    _, sizes = get_stats(state)
    for c, (d, s) in sorted(state["plan"].items(), key=lambda kv: (CHRONO.index(kv[1]), kv[0])):
        rows.append({"Course": c, "Day": d, "Slot": s, "Students": sizes[c], "Rooms": ", ".join(sorted(where[c]))})
    for c in state["pending"]:
        rows.append({"Course": c, "Day": "UNSCHEDULED", "Slot": "-", "Students": sizes.get(c, 0), "Rooms": "-"})
    return pd.DataFrame(rows)


# =====================================================================
#  SIDEBAR : ACCESS CONTROL + INPUTS
# =====================================================================
S = st.session_state
st.sidebar.header("🔐 Access Control")
role = st.sidebar.selectbox("Role", ["Admin", "Faculty", "Student"])
is_admin = False
if role == "Admin":
    if not S.get("admin_ok"):
        with st.sidebar.form("login_form"):
            pin = st.text_input("Admin PIN", type="password")
            go = st.form_submit_button("🔓 Login")
        if go:
            if pin == ADMIN_PIN:
                S["admin_ok"] = True
                st.rerun()
            else:
                st.sidebar.error("Wrong PIN")
        st.sidebar.caption("Demo PIN: admin123 (type it, then click Login)")
    else:
        if st.sidebar.button("Logout"):
            S["admin_ok"] = False
            st.rerun()
    is_admin = bool(S.get("admin_ok"))
actor = role

if is_admin:
    st.sidebar.header("⚙️ Live Demo Configuration")
    load_600 = st.sidebar.checkbox("🚀 Quick-Load Demo Dataset", value=False)

    if load_600:
        courses_pool = ["MATH101", "CS101", "PHY101", "CHEM101", "ECO101", "AI101"]
        first_names = ["Aaradhya", "Abhishek", "Aditya", "Akshara", "Ananya", "Devanshi", "Divyanshu", "Gautam", "Kanishka", "Manas", "Mayank", "Pradeep", "Rishin", "Sanjay", "Shagun", "Shaurya", "Uday", "Vishal"]
        last_names = ["Sharma", "Aheer", "Singh", "Jain", "Gupta", "Rathore", "Reddy", "Chauhan", "Kaur", "Agarwal", "Choudhary", "Khandelwal"]
        generated_lines, student_names = [], {}
        random.seed(42)
        for i in range(1, 121):
            fn, ln = random.choice(first_names), random.choice(last_names)
            roll = f"2026BTech{i:03d}"
            student_names[roll] = f"{fn} {ln}"
            generated_lines.append(f"{fn} {ln}, {roll}, {courses_pool[(i - 1) % len(courses_pool)]}")
        for i in range(1, 7):   # 6 students enrolled in two courses -> forces real clash handling
            roll = f"2026BTech{i:03d}"
            generated_lines.append(f"{student_names[roll]}, {roll}, {courses_pool[i % len(courses_pool)]}")
        default_students_text = "\n".join(generated_lines)
        default_faculty = "Dr. Anita Mehta\nDr. Suresh Kumar\nDr. Rajesh Sharma\nDr. Priya Verma\nDr. Alok Nath\nDr. Neha Joshi\nDr. Vikram Singh\nDr. Kavita Rao\nDr. Manish Gupta\nDr. Sunita Agarwal"
        default_rooms = "TB1-102, 40, Curved\nLH-1, 80, Straight\nLT-301, 40, Curved"
    else:
        default_students_text = default_faculty = default_rooms = ""

    st.sidebar.subheader("📋 1. Student Dataset")
    raw_students = st.sidebar.text_area("Name, RollNo, Course", value=default_students_text, height=130, placeholder="e.g. Vishal Sharma, 2026BTech001, MATH101")
    st.sidebar.subheader("👨‍🏫 2. Faculty / Invigilators List")
    raw_faculty = st.sidebar.text_area("Faculty Names", value=default_faculty, height=100, placeholder="e.g. Dr. Anita Mehta")
    st.sidebar.subheader("🏛️ 3. Room Capacities & Layouts")
    raw_rooms = st.sidebar.text_area("Rooms Configuration", value=default_rooms, height=120, placeholder="e.g. TB1-102, 40, Curved")

    st.sidebar.subheader("🗓️ 4. Exam Days & Shifts")
    st.sidebar.caption("Shifts per day: 0 = no exams, 1 = Morning only, 2 = Morning + Afternoon.")
    shifts = {}
    for _d in DAYS:
        _c1, _c2 = st.sidebar.columns([3, 2])
        _c1.markdown(f"<div style='padding-top:8px'><b>{_d}</b></div>", unsafe_allow_html=True)
        shifts[_d] = int(_c2.number_input(_d, min_value=0, max_value=len(SLOTS), value=len(SLOTS), step=1, key=f"shift_{_d}", label_visibility="collapsed"))
    st.sidebar.caption(f"Total exam slots available: **{sum(shifts.values())}**")
    if "state" in S:
        _cur = S["state"]["opts"].get("shifts") or DEFAULT_SHIFTS
        st.sidebar.caption("Live schedule uses: " + ", ".join(f"{d[:3]} {_cur.get(d, len(SLOTS))}" for d in DAYS))
        if st.sidebar.button("🔁 Apply shifts to current schedule"):
            _p = "Set shifts: " + ", ".join(f"{d} {n}" for d, n in shifts.items())
            S["chat_history"].append({"role": "user", "text": _p})
            with st.spinner("Re-planning with the new shifts..."):
                run_agent(_p, actor, forced={"reasoning": "Shift configuration changed from the sidebar.", "actions": [{"tool": "set_shifts", "shifts": shifts}]})
            st.rerun()

    with st.sidebar.expander("🎛️ Scheduling Options"):
        cps = st.slider("Max courses per exam slot", 1, 4, 3, help="Courses sharing a slot can be interleaved in the same room (anti-collusion).")
        strategy_label = st.radio("Room strategy", ["Maximize separation (use all rooms)", "Minimize rooms used"])
        spread_all = st.checkbox("Spread exams across all open shifts", value=True, help="ON: uses every open day/shift (more rest days between exams). OFF: packs several courses into few slots.")
    with st.sidebar.expander("🧠 Agent Engine (optional LLM)"):
        provider = st.selectbox("Provider", ["Anthropic", "Google Gemini"], index=1)
        default_model = "claude-sonnet-5-5" if provider == "Anthropic" else "gemini-2.5-flash"
        model = st.text_input("Model", value=default_model, key=f"model_{provider}")
        env_key = _secret("ANTHROPIC_API_KEY") if provider == "Anthropic" else GEMINI_KEY_DEFAULT
        api_key = st.text_input("API key", value=env_key, type="password", key=f"key_{provider}")
        st.caption("No key = rule-based planner. With a key, an LLM plans multi-step actions; the same tools and safety checks still run.")
    S["llm_cfg"] = {"provider": provider, "key": api_key, "model": model} if api_key else None

    if st.sidebar.button("🚀 Generate Space-Optimized Seating", type="primary"):
        try:
            students, faculty_list, rooms, warns = parse_inputs(raw_students, raw_faculty, raw_rooms)
            if not students or not faculty_list or not rooms:
                st.error("Please provide valid data for students, faculty, and rooms to generate the schedule.")
            elif sum(shifts.values()) == 0:
                st.error("Set at least one exam shift in '4. Exam Days & Shifts'.")
            else:
                state = {"students": students, "faculty": faculty_list, "rooms": rooms, "plan": {}, "pending": [],
                         "leave": {}, "duties": {}, "opts": {"cps": cps, "strategy": "min" if strategy_label.startswith("Minimize") else "spread", "shifts": dict(shifts), "spread_all": spread_all}}
                plan, unplaced = initial_plan(state)
                state["plan"], state["pending"] = plan, unplaced
                S["state"], S["base_state"] = state, copy.deepcopy(state)
                S["status"], S["version"] = "DRAFT", S["version"] + 1
                refresh()
                S["audit_log"] = []
                log("GENERATE", f"{len({s['RollNo'] for s in students})} students, {len(rooms)} rooms, {len(plan)} courses scheduled", actor)
                S["chat_history"] = [{"role": "assistant", "text": "Generated a clash-free draft timetable and verified it against all constraints. Review it, then **approve & publish**. You can also instruct me below.", "trace": []}]
                for w in warns:
                    st.warning(w)
                if unplaced:
                    st.warning(f"Could not schedule: {', '.join(unplaced)} (too large for the rooms / no free slot).")
                st.success(f"Allocated {len(S['state']['students'])} exam entries across {len(plan)} courses with clash-free slots, anti-collusion seating and verified constraints!")
        except Exception as e:
            st.error(f"Error during execution: {e}")

# =====================================================================
#  MAIN VIEWS
# =====================================================================
if "state" not in S:
    if role == "Admin" and not is_admin:
        st.info("🔐 Enter the Admin PIN in the sidebar (demo PIN: **admin123**).")
    elif role == "Admin":
        st.info("👈 Use the sidebar to input your clean dataset or check **'Quick-Load Demo Dataset'** for the live presentation, then click **'Generate Space-Optimized Seating'**!")
    else:
        st.info("No timetable has been published yet. Please check back after the administrator publishes it.")
    st.stop()

state, alloc, checks = S["state"], S["alloc"], S["checks"]
df_sch, ra = alloc["df_sch"], alloc["room_allocs"]
published = S["status"] == "PUBLISHED"

# ---------------- Faculty / Student portals (RBAC) ----------------
if role in ("Faculty", "Student"):
    if not published:
        st.warning("🔒 The timetable is still a DRAFT. It will be visible here once the administrator approves and publishes it.")
        st.stop()
    if role == "Student":
        st.markdown("### 🎓 Student Portal")
        roll_in = st.text_input("Enter your Roll Number", placeholder="e.g. 2026BTech001").strip().lower()
        if roll_in:
            mine = [{"Course": s["Course"], "Day": r["day"], "Slot": r["slot"], "Room": r["room_name"], "Seat": s["Position"]}
                    for r in ra.values() for s in r["students"] if s["RollNo"].lower() == roll_in]
            if mine:
                st.dataframe(pd.DataFrame(mine), use_container_width=True)
            else:
                st.error("No exam seat found for that roll number.")
    else:
        st.markdown("### 👨‍🏫 Faculty Portal - My Invigilation Duties")
        me = st.selectbox("Select your name", state["faculty"])
        duties = [{"Day": r["day"], "Slot": r["slot"], "Room": r["room_name"], "Students": len(r["students"]),
                   "Courses": ", ".join(sorted({s["Course"] for s in r["students"]}))} for r in ra.values() if me in r["inv_list"]]
        st.dataframe(pd.DataFrame(duties) if duties else pd.DataFrame([{"Info": "No duties assigned"}]), use_container_width=True)
    st.markdown("### 📅 Published Exam Timetable")
    st.dataframe(course_table(state, alloc), use_container_width=True)
    st.stop()

# ---------------- Admin dashboard ----------------
if not is_admin:
    st.stop()

passed = sum(1 for c in checks if c["Status"].startswith("✅"))
utilized_rooms_count = len({r["room_name"] for r in ra.values()})
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Students", len({e["RollNo"] for e in state["students"]}))
col2.metric("Rooms Utilized", utilized_rooms_count)
col3.metric("Active Exam Sessions", len(df_sch))
col4.metric("Exam Slots Used", len({(r["day"], r["slot"]) for r in ra.values()}))
col5.metric("Constraint Checks", f"{passed}/{len(checks)}")

if published:
    st.success(f"✅ PUBLISHED - approved by Admin at {S.get('published_at', '')}. Faculty and student portals are live.")
else:
    st.warning("📝 DRAFT - awaiting administrator approval. Publishing, downloads and portals are locked until you approve.")

st.markdown("### 📅 Course-wise Exam Timetable (Clash-Free)")
st.dataframe(course_table(state, alloc), use_container_width=True)

st.markdown("### 📊 Master Exam Timetable & Room Utilization")
st.dataframe(df_sch, use_container_width=True)

st.markdown("### 🏛️ Room-wise Box-Grid Floor Maps")
all_courses = sorted({e["Course"] for e in state["students"]})
color_of = {c: PALETTE[i % len(PALETTE)] for i, c in enumerate(all_courses)}
for room_key, r_data in ra.items():
    with st.expander(f"📍 {room_key} | Total Seated: {len(r_data['students'])} / {r_data['capacity']} | Invigilators: {r_data['invigilators']}"):
        st.markdown(room_map_html(r_data, color_of), unsafe_allow_html=True)
        st.caption(f"Same-course neighbour pairs in this room: **{r_data['risk_pairs']}**")
        st.dataframe(pd.DataFrame(r_data["students"]), use_container_width=True)

# ---------------- Command assistant ----------------
st.markdown("---")
st.markdown("### 🤖 Autonomous Command Assistant")
llm_on = bool(S.get("llm_cfg"))
st.caption("Examples: **'Move Monday to Wednesday'**, **'Cancel Tuesday and then auto reschedule'**, **'Move CS101 to Friday afternoon'**, **'Set Monday shifts to 2 and Tuesday to 1'**, **'Swap physics and maths'**, **'Dr. Suresh Kumar is on leave, give his duty to Dr. Vikram Singh'**, **'Where does 2026BTech005 sit?'**, **'Show Monday exams'**, **'Verify'**, or **'Reset'**")
st.caption(("🧠 Engine: LLM planner (" + S["llm_cfg"]["provider"] + ") with rule-based fallback" if llm_on else "⚙️ Engine: rule-based planner (add an API key in the sidebar for the LLM planner)")
           + "  |  every command: plan → tool calls → verify → commit or roll back")


def send_to_agent(text):
    S["chat_history"].append({"role": "user", "text": text})
    with st.spinner("🤖 Agent is planning, calling tools and verifying..."):
        run_agent(text, actor)
    st.rerun()


with st.form(key="chat_form", clear_on_submit=True):
    user_prompt = st.text_input("Give instruction to chatbot:")
    submit_chat = st.form_submit_button("Send to AI Agent")

quick = ["Verify", "Show Monday exams", "Cancel Monday then auto reschedule", "Help", "Reset"]
for _col, _q in zip(st.columns(len(quick)), quick):
    if _col.button(_q, key=f"quick_{_q}"):
        send_to_agent(_q)

if submit_chat and user_prompt:
    send_to_agent(user_prompt)

# newest conversation turn first, so the reply appears right under the input box
hist = S["chat_history"]
turns = []
for chat in hist:
    if chat["role"] == "user" or not turns:
        turns.append([chat])
    else:
        turns[-1].append(chat)
for ti, turn in enumerate(reversed(turns)):
    for chat in turn:
        if chat["role"] == "user":
            st.markdown(f"**👤 You:** {chat['text']}")
        else:
            st.markdown(f"**🤖 AI Agent:** {chat['text']}")
            if chat.get("trace") and ti == 0:
                with st.expander("🔍 Agent reasoning trace (plan → tools → verification)", expanded=True):
                    for t in chat["trace"]:
                        st.markdown(t)

# ---------------- Verification + approval ----------------
st.markdown("---")
st.markdown("### 🛡️ Constraint Verification & Administrator Approval")
st.dataframe(pd.DataFrame(checks).drop(columns=["kind"]), use_container_width=True)
blockers = [c for c in checks if c["Status"].startswith("❌")]

ap1, ap2 = st.columns(2)
with ap1:
    if not published:
        if st.button("✅ Approve & Publish Timetable", type="primary", disabled=bool(blockers)):
            S["status"] = "PUBLISHED"
            S["published_at"] = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
            log("APPROVE", f"Timetable v{S['version']} published", actor)
            st.rerun()
        if blockers:
            st.caption("Publishing is blocked until all ❌ checks pass.")
    else:
        if st.button("↩️ Revoke Publication"):
            S["status"] = "DRAFT"
            log("REVOKE", f"Timetable v{S['version']} unpublished", actor)
            st.rerun()
with ap2:
    wl = Counter(f for r in ra.values() for f in r["inv_list"])
    with st.expander("👨‍🏫 Invigilation workload"):
        st.dataframe(pd.DataFrame([{"Faculty": f, "Duties": wl.get(f, 0),
                                    "Availability": ("On leave: " + ", ".join(sorted(state["leave"][f]))) if f in state["leave"] else "Available"}
                                   for f in state["faculty"]]), use_container_width=True)

with st.expander("📜 Audit log (agent reasoning, tool calls, approvals)"):
    st.dataframe(pd.DataFrame(S["audit_log"]) if S["audit_log"] else pd.DataFrame(columns=["Time", "Actor", "Action", "Detail"]), use_container_width=True)

# ---------------- Downloads (locked until approval) ----------------
st.markdown("---")
st.markdown("### 📥 Download Print-Ready Room PDFs & Master Reports")
if not published:
    st.info("🔒 Downloads unlock after the administrator approves & publishes the timetable.")
else:
    if S.get("pdf_cache", (None, None))[0] != S["version"]:
        S["pdf_cache"] = (S["version"], generate_room_pdf(ra, S.get("published_at", "")))
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button(label="📄 Download Diagrammatic Box-Grid PDFs", data=S["pdf_cache"][1],
                           file_name="jklu_box_grid_floor_map_notices.pdf", mime="application/pdf")
    with col_dl2:
        output = io.BytesIO()
        seat_rows = [{"Day": r["day"], "Slot": r["slot"], "Room": r["room_name"], **{k: s[k] for k in ("RollNo", "Name", "Course", "Position")}}
                     for r in ra.values() for s in r["students"]]
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df_sch.to_excel(writer, sheet_name='Master_Timetable', index=False)
            course_table(state, alloc).to_excel(writer, sheet_name='Course_Timetable', index=False)
            pd.DataFrame(seat_rows).to_excel(writer, sheet_name='Seating_Plan', index=False)
            pd.DataFrame(state["students"]).to_excel(writer, sheet_name='All_Students', index=False)
            pd.DataFrame(S["audit_log"]).to_excel(writer, sheet_name='Audit_Log', index=False)
        st.download_button(label="📊 Download Complete Master Excel Report", data=output.getvalue(),
                           file_name="jklu_exam_master_report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")