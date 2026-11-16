"""
tools.py - the agent's tool belt (no Streamlit imports).

Each `act_*` function takes (action dict, working state) and returns {ok, changed, msg}. The agent loop runs them on a
copy of the state, then verifies the result before committing - so a bad tool call can never corrupt the schedule.
"""
import copy
import re
from engine import CHRONO, DAYS, DEFAULT_SHIFTS, SLOTS, TITLES, active_chrono, build_allocations, copy, get_stats, run_validation, slot_check, total_cap, unavailable


_base_state_provider = lambda: None      # the app registers where the baseline schedule lives


def set_base_state_provider(fn):
    global _base_state_provider
    _base_state_provider = fn


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
    base = _base_state_provider()
    if base is None:
        return {"ok": False, "msg": "There is no baseline schedule to restore yet."}
    base = copy.deepcopy(base)
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
