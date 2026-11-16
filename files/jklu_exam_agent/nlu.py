"""
nlu.py - deterministic natural-language understanding (typo tolerance, entity extraction, intents).

This is the rule-based planner: it turns a chat command into a list of tool calls and is the fallback when no LLM
key is configured or the LLM planner fails. No Streamlit imports - conversation memory is passed in.
"""
import difflib
import re
from collections import defaultdict
from engine import DAYS, TITLES, get_stats
from tools import DAY_RE, norm_day, valid_slots


# =====================================================================
#  PLANNERS: LLM (optional) + rule-based fallback
# =====================================================================
# =====================================================================
#  NATURAL-LANGUAGE UNDERSTANDING (typo tolerance, entity extraction, intents)
# =====================================================================
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


def safe_rule_plan(text, state, memory=None):
    try:
        return rule_plan(text, state, memory)
    except Exception as e:                # the parser must never crash the chat
        return {"clarify": f"I couldn't parse that ({type(e).__name__}). Please rephrase, or type 'help' to see what I can do."}


def rule_plan(text, state, memory=None):
    """memory: any dict-like that carries 'last_ctx' between turns (the app passes the session state)."""
    memory = memory if memory is not None else {}
    ctx = dict(memory.get("last_ctx") or {})
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

    memory["last_ctx"] = ctx
    if not actions:
        return {"clarify": "I couldn't find an action in that. Type **help** to see what I can do."}
    return {"reasoning": f"Parsed with the rule-based planner ({len(actions)} step(s)).", "actions": actions}
