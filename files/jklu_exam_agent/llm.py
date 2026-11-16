"""
llm.py - optional LLM planner client (Anthropic / Google Gemini) + the planner prompt.
The LLM only ever *plans* (JSON tool calls); every tool call is still validated, verified and human-approved.
"""
import json
import requests
import time
from engine import DEFAULT_SHIFTS


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
