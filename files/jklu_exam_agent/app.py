"""
JKLU Autonomous Exam & Seating Arrangement System  (PS-05)   -   Streamlit UI + agent loop

Layout of the project
    app.py      UI, role-based access, approval gates, and the plan -> tools -> verify -> commit/rollback agent loop
    engine.py   scheduling, seating optimiser, invigilator allocation, self-verification   (pure, unit-tested)
    solver.py   optional OR-Tools CP-SAT timetable solver + independent plan verifier
    tools.py    the agent's tool belt (cancel / move / swap / leave / reassign / shifts / lookups ...)
    nlu.py      rule-based natural-language planner (fallback when no LLM key is set)
    llm.py      optional LLM planner (Anthropic / Gemini)
    reports.py  floor maps, print-ready PDFs, course table
    storage.py  SQLite persistence: schedule snapshot, audit log, login lock-outs
    auth.py     access codes for faculty / students, PIN check
"""
import streamlit as st
import pandas as pd
import datetime
import io
import os
import json
import copy
import random
from collections import Counter

import auth
import storage
import tools
from engine import (
    CHRONO, DAYS, DEFAULT_SHIFTS, SLOTS, active_chrono, build_allocations, get_stats,
    initial_plan, integrity_fails, parse_inputs, run_validation, solver_available,
)
from llm import LLM_SYSTEM, call_llm, extract_json, state_summary
from nlu import safe_rule_plan
from reports import course_table, generate_room_pdf, room_map_html
from tools import execute_action

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
#  CONSTANTS + SECRETS
# =====================================================================
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
# Admin PIN comes from the ADMIN_PIN env var / secrets. It is never displayed in the UI.
# The fallback below exists only so a local demo starts; the admin sees a warning while it is active.
ADMIN_PIN = os.environ.get("ADMIN_PIN") or _secret("ADMIN_PIN") or "admin123"
DEFAULT_PIN_IN_USE = ADMIN_PIN == "admin123"
PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#06b6d4", "#a855f7", "#84cc16", "#ec4899"]
MAX_ATTEMPTS = 3          # plan -> execute -> verify attempts per command (failures are fed back to the planner)
MAX_LOGIN_FAILS = 5       # failed logins per account / 5 minutes before a temporary lock-out


# =====================================================================
#  SESSION + AUDIT LOG (audit entries are persisted to SQLite)
# =====================================================================
for _k, _v in {"chat_history": [], "status": "DRAFT", "version": 0}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


def log(action, detail, actor="system"):
    storage.append_audit(actor, action, str(detail)[:1000])


def refresh():
    S = st.session_state
    S["alloc"] = build_allocations(S["state"])
    S["checks"] = run_validation(S["state"], S["alloc"])


def persist():
    """Save the current schedule + approval status so it survives refreshes/restarts and is shared with the portals."""
    S = st.session_state
    S["db_sig"] = storage.save_snapshot(S["state"], S.get("base_state"), S["status"], S["version"], S.get("published_at", ""))


tools.set_base_state_provider(lambda: st.session_state.get("base_state"))     # lets the 'reset' tool find the baseline


def sync_from_db(force=False):
    """Load the shared schedule from SQLite (only when it changed, unless forced)."""
    S = st.session_state
    sig = storage.snapshot_sig()
    if sig is None or (not force and S.get("db_sig") == sig):
        return
    snap = storage.load_snapshot()
    if not snap:
        return
    S["state"] = snap["state"]
    S["base_state"] = snap["base_state"] or copy.deepcopy(snap["state"])
    S["status"], S["version"], S["published_at"] = snap["status"], snap["version"], snap["published_at"]
    S["db_sig"] = snap["sig"]
    refresh()


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
                rp = safe_rule_plan(prompt, state, st.session_state)
                if rp.get("actions"):
                    trace.append("ℹ️ LLM asked for clarification, but the rule-based planner could resolve the request.")
                    return rp, "Rule-based"
            return data, f"LLM ({cfg['provider']})"
        except Exception as e:
            trace.append(f"⚠️ LLM planner unavailable ({e}). Falling back to the rule-based planner.")
    return safe_rule_plan(prompt, state, st.session_state), "Rule-based"


def commit(new_state, reason):
    S = st.session_state
    was_published = S["status"] == "PUBLISHED"
    S["state"] = new_state
    S["status"] = "DRAFT"
    S["version"] += 1
    refresh()
    persist()
    log("COMMIT", reason, "agent")
    return was_published


def _run_agent(prompt, actor="Admin", forced=None):
    S = st.session_state
    trace, final, feedback = [], "", None
    baseline = integrity_fails(S["checks"])
    log("COMMAND", prompt, actor)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        data, engine_name = (forced, "Sidebar control") if forced else plan_with_engine(prompt, S["state"], trace, feedback)
        if data.get("clarify"):
            final = "🤔 " + data["clarify"]
            trace.append(f"❓ Clarification needed ({engine_name}): {data['clarify']}")
            break
        actions = data.get("actions") or []
        trace.append(f"🧠 Plan ({engine_name}): {data.get('reasoning', '')} -> {len(actions)} step(s)")
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
            if engine_name.startswith("LLM") and attempt < MAX_ATTEMPTS:
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
            if engine_name.startswith("LLM") and attempt < MAX_ATTEMPTS:
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
#  SIDEBAR : ACCESS CONTROL + INPUTS
# =====================================================================
S = st.session_state
st.sidebar.header("🔐 Access Control")
role = st.sidebar.selectbox("Role", ["Admin", "Faculty", "Student"])
if role != "Student":
    S.pop("student_ok", None)
if role != "Faculty":
    S.pop("faculty_ok", None)
is_admin = False
if role == "Admin":
    if not S.get("admin_ok"):
        with st.sidebar.form("login_form"):
            pin = st.text_input("Admin PIN", type="password")
            go = st.form_submit_button("🔓 Login")
        if go:
            if storage.is_locked("admin", MAX_LOGIN_FAILS):
                st.sidebar.error("Too many failed attempts. Try again in a few minutes.")
            elif auth.check_pin(ADMIN_PIN, pin):
                storage.clear_failures("admin")
                S["admin_ok"] = True
                log("LOGIN", "Admin signed in", "Admin")
                st.rerun()
            else:
                storage.record_failure("admin")
                log("LOGIN_FAILED", "Wrong admin PIN", "Admin")
                st.sidebar.error("Wrong PIN")
    else:
        if st.sidebar.button("Logout"):
            S["admin_ok"] = False
            log("LOGOUT", "Admin signed out", "Admin")
            st.rerun()
    is_admin = bool(S.get("admin_ok"))
    if is_admin and DEFAULT_PIN_IN_USE:
        st.sidebar.warning("The built-in demo PIN is active. Set the ADMIN_PIN environment variable (or secret) before sharing this app.")
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
    st.sidebar.caption("Synthetic data only - do not enter real student records (event rule).")
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
        use_solver = st.checkbox("Use OR-Tools CP-SAT solver for the timetable", value=solver_available(), disabled=not solver_available(),
                                 help="Optimises load balance and rest between exams. Every solver answer is re-verified; the greedy planner is the fallback."
                                 + ("" if solver_available() else " (ortools is not installed: pip install ortools)"))
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
                         "leave": {}, "duties": {}, "opts": {"cps": cps, "strategy": "min" if strategy_label.startswith("Minimize") else "spread", "shifts": dict(shifts), "spread_all": spread_all, "use_solver": use_solver}}
                plan, unplaced = initial_plan(state)
                state["plan"], state["pending"] = plan, unplaced
                S["state"], S["base_state"] = state, copy.deepcopy(state)
                S["status"], S["version"], S["published_at"] = "DRAFT", S["version"] + 1, ""
                refresh()
                persist()
                log("GENERATE", f"{len({s['RollNo'] for s in students})} students, {len(rooms)} rooms, {len(plan)} courses scheduled "
                                f"| planner: {state['opts'].get('planner_used', '-')}", actor)
                S["chat_history"] = [{"role": "assistant", "text": "Generated a clash-free draft timetable and verified it against all constraints. Review it, then **approve & publish**. You can also instruct me below.", "trace": []}]
                for w in warns:
                    st.warning(w)
                if unplaced:
                    st.warning(f"Could not schedule: {', '.join(unplaced)} (too large for the rooms / no free slot).")
                st.success(f"Allocated {len(S['state']['students'])} exam entries across {len(plan)} courses with clash-free slots, anti-collusion seating and verified constraints! "
                           f"(Timetable planner: {state['opts'].get('planner_used', '-')})")
        except Exception as e:
            st.error(f"Error during execution: {e}")

# =====================================================================
#  MAIN VIEWS
# =====================================================================
# ---------------- Load the shared schedule (SQLite) ----------------
if "state" not in S:
    sync_from_db(force=True)
elif role in ("Faculty", "Student"):
    sync_from_db()                       # portals always show the latest published version

if role == "Admin" and not is_admin:
    st.info("🔐 Enter the Admin PIN in the sidebar to continue.")
    st.stop()

if "state" not in S:
    if role == "Admin":
        st.info("👈 Use the sidebar to input your clean dataset or check **'Quick-Load Demo Dataset'** for the live presentation, then click **'Generate Space-Optimized Seating'**!")
    else:
        st.info("No timetable has been published yet. Please check back after the administrator publishes it.")
    st.stop()

state, alloc, checks = S["state"], S["alloc"], S["checks"]
df_sch, ra = alloc["df_sch"], alloc["room_allocs"]
published = S["status"] == "PUBLISHED"

# ---------------- Faculty / Student portals (RBAC: personal access codes) ----------------
if role in ("Faculty", "Student"):
    if not published:
        st.warning("🔒 The timetable is still a DRAFT. It will be visible here once the administrator approves and publishes it.")
        st.stop()
    secret = storage.get_secret()
    if role == "Student":
        st.markdown("### 🎓 Student Portal")
        if not S.get("student_ok"):
            with st.form("student_login"):
                roll_in = st.text_input("Roll Number", placeholder="e.g. 2026BTech001")
                code_in = st.text_input("Access code", type="password", help="Issued to you by the examination cell.")
                go = st.form_submit_button("🔓 View my exam seats")
            if go:
                roll_key = roll_in.strip().lower()
                lock_key = f"student:{roll_key}"
                known = {e["RollNo"].lower() for e in state["students"]}
                if storage.is_locked(lock_key, MAX_LOGIN_FAILS):
                    st.error("Too many failed attempts for this roll number. Try again in a few minutes.")
                elif roll_key in known and auth.check_code(secret, "student", roll_key, code_in):
                    storage.clear_failures(lock_key)
                    S["student_ok"] = roll_key
                    log("LOGIN", "Student portal sign-in", f"Student:{roll_key}")
                    st.rerun()
                else:
                    storage.record_failure(lock_key)
                    log("LOGIN_FAILED", "Student portal sign-in", f"Student:{roll_key[:24]}")
                    st.error("Invalid roll number or access code.")
            st.stop()
        roll_in = S["student_ok"]
        if st.button("Logout", key="student_logout"):
            S.pop("student_ok", None)
            st.rerun()
        mine = [{"Course": s["Course"], "Day": r["day"], "Slot": r["slot"], "Room": r["room_name"], "Seat": s["Position"]}
                for r in ra.values() for s in r["students"] if s["RollNo"].lower() == roll_in]
        if mine:
            st.dataframe(pd.DataFrame(mine), use_container_width=True)
        else:
            st.error("No exam seat found for your roll number.")
    else:
        st.markdown("### 👨‍🏫 Faculty Portal - My Invigilation Duties")
        if S.get("faculty_ok") not in state["faculty"]:
            S.pop("faculty_ok", None)
        if not S.get("faculty_ok"):
            with st.form("faculty_login"):
                fid_in = st.text_input("Faculty ID", placeholder="e.g. FAC001")
                code_in = st.text_input("Access code", type="password", help="Issued to you by the examination cell.")
                go = st.form_submit_button("🔓 View my duties")
            if go:
                fid_key = fid_in.strip().upper()
                lock_key = f"faculty:{fid_key}"
                name = auth.faculty_by_id(fid_key, state["faculty"])
                if storage.is_locked(lock_key, MAX_LOGIN_FAILS):
                    st.error("Too many failed attempts for this ID. Try again in a few minutes.")
                elif name and auth.check_code(secret, "faculty", name, code_in):
                    storage.clear_failures(lock_key)
                    S["faculty_ok"] = name
                    log("LOGIN", "Faculty portal sign-in", f"Faculty:{fid_key}")
                    st.rerun()
                else:
                    storage.record_failure(lock_key)
                    log("LOGIN_FAILED", "Faculty portal sign-in", f"Faculty:{fid_key[:24]}")
                    st.error("Invalid Faculty ID or access code.")
            st.stop()
        me = S["faculty_ok"]
        st.caption(f"Signed in as **{me}**")
        if st.button("Logout", key="faculty_logout"):
            S.pop("faculty_ok", None)
            st.rerun()
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

with st.expander("🔑 Access credentials for Faculty & Students (admin only)"):
    st.caption("Hand these out through the exam cell. Codes are derived from a private per-installation secret; "
               "faculty sign in with Faculty ID + code, students with Roll Number + code. Repeated wrong codes lock the account for 5 minutes.")
    cred_df = pd.DataFrame(auth.credentials_table(storage.get_secret(), state["faculty"], state["students"]))
    st.dataframe(cred_df, use_container_width=True)
    st.download_button("⬇️ Download credentials (CSV)", cred_df.to_csv(index=False).encode("utf-8"),
                       file_name="jklu_access_credentials.csv", mime="text/csv")

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
        st.caption(f"Same-course neighbour pairs in this room: **{r_data['risk_pairs']}**  |  diagonal / one-seat-gap pairs: **{r_data['near_pairs']}**")
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
            persist()
            log("APPROVE", f"Timetable v{S['version']} published", actor)
            st.rerun()
        if blockers:
            st.caption("Publishing is blocked until all ❌ checks pass.")
    else:
        if st.button("↩️ Revoke Publication"):
            S["status"] = "DRAFT"
            persist()
            log("REVOKE", f"Timetable v{S['version']} unpublished", actor)
            st.rerun()
with ap2:
    wl = Counter(f for r in ra.values() for f in r["inv_list"])
    with st.expander("👨‍🏫 Invigilation workload"):
        st.dataframe(pd.DataFrame([{"Faculty": f, "Duties": wl.get(f, 0),
                                    "Availability": ("On leave: " + ", ".join(sorted(state["leave"][f]))) if f in state["leave"] else "Available"}
                                   for f in state["faculty"]]), use_container_width=True)

with st.expander("📜 Audit log (agent reasoning, tool calls, approvals)"):
    audit_rows = storage.load_audit()
    st.caption("Stored in SQLite - survives refreshes and restarts." if not storage.using_memory_fallback() else "⚠️ Database unavailable - audit log is held in memory only.")
    st.dataframe(pd.DataFrame(audit_rows) if audit_rows else pd.DataFrame(columns=["Time", "Actor", "Action", "Detail"]), use_container_width=True)

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
            pd.DataFrame(storage.load_audit()).to_excel(writer, sheet_name='Audit_Log', index=False)
        st.download_button(label="📊 Download Complete Master Excel Report", data=output.getvalue(),
                           file_name="jklu_exam_master_report.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")