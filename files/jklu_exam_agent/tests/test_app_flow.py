"""End-to-end test of app.py driven through a fake Streamlit: login, lock-out, generate, chat agent, approval gate,
persistence across sessions, and the student / faculty portals."""
import os
import tempfile
import unittest

from helpers import ROOT
from fake_streamlit import FakeSt, run_app
import auth
import storage

APP = os.path.join(ROOT, "app.py")
DEMO = {"🚀 Quick-Load Demo Dataset": True}


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        storage.DB_PATH = os.path.join(tempfile.mkdtemp(), "flow.db")
        storage._use_mem = False
        os.environ["ADMIN_PIN"] = "test-pin-123"
        self.admin = {}
        run_app(APP, FakeSt(inputs={"Admin PIN": "test-pin-123"}, clicks={"🔓 Login"}, session=self.admin))
        run_app(APP, FakeSt(inputs=DEMO, clicks={"🚀 Generate Space-Optimized Seating"}, session=self.admin))

    def chat(self, text):
        run_app(APP, FakeSt(inputs={**DEMO, "Give instruction to chatbot:": text}, clicks={"Send to AI Agent"}, session=self.admin))
        return self.admin["chat_history"][-1]["text"]

    def approve(self):
        run_app(APP, FakeSt(inputs=DEMO, clicks={"✅ Approve & Publish Timetable"}, session=self.admin))

    def test_wrong_pin_locks_admin_login(self):
        s = {}
        for _ in range(5):
            run_app(APP, FakeSt(inputs={"Admin PIN": "wrong"}, clicks={"🔓 Login"}, session=s))
        run_app(APP, FakeSt(inputs={"Admin PIN": "test-pin-123"}, clicks={"🔓 Login"}, session=s))
        self.assertFalse(s.get("admin_ok"))

    def test_generated_draft_passes_all_checks_and_is_not_published(self):
        self.assertEqual(self.admin["status"], "DRAFT")
        self.assertTrue(all(c["Status"].startswith("✅") for c in self.admin["checks"]))

    def test_agent_changes_force_reapproval_and_chat_cannot_publish(self):
        self.approve()
        self.assertEqual(self.admin["status"], "PUBLISHED")
        self.chat("Swap physics and maths")
        self.assertEqual(self.admin["status"], "DRAFT")                       # any change revokes publication
        self.assertIn("can't publish", self.chat("approve and publish the timetable"))
        self.assertEqual(self.admin["status"], "DRAFT")

    def test_unsafe_command_is_refused_and_schedule_untouched(self):
        before = dict(self.admin["state"]["plan"])
        self.chat("Set all shifts to 0")
        self.assertEqual(self.admin["state"]["plan"], before)

    def test_audit_log_records_agent_steps_and_approval(self):
        self.chat("Show Monday exams")
        self.approve()
        actions = {r["Action"] for r in storage.load_audit()}
        self.assertTrue({"GENERATE", "COMMAND", "AGENT_PLAN", "TOOL_CALL", "APPROVE", "LOGIN"} <= actions)

    def test_state_persists_to_a_new_session(self):
        self.approve()
        fresh = {}
        run_app(APP, FakeSt(inputs={"Admin PIN": "test-pin-123"}, clicks={"🔓 Login"}, session=fresh))
        run_app(APP, FakeSt(session=fresh))
        self.assertEqual((fresh["status"], len(fresh["state"]["students"])), ("PUBLISHED", 126))

    def test_student_portal_requires_published_timetable_and_valid_code(self):
        roll = "2026BTech005"
        s = {}
        run_app(APP, FakeSt(inputs={"Role": "Student", "Roll Number": roll, "Access code": "x"}, clicks={"🔓 View my exam seats"}, session=s))
        self.assertFalse(s.get("student_ok"))                                 # draft: nothing is exposed
        self.approve()
        code = auth.make_code(storage.get_secret(), "student", roll)
        bad = FakeSt(inputs={"Role": "Student", "Roll Number": roll, "Access code": "WRONG1"}, clicks={"🔓 View my exam seats"}, session=s)
        run_app(APP, bad)
        self.assertFalse(s.get("student_ok"))
        run_app(APP, FakeSt(inputs={"Role": "Student", "Roll Number": roll, "Access code": code}, clicks={"🔓 View my exam seats"}, session=s))
        self.assertEqual(s.get("student_ok"), roll.lower())
        other = {}
        run_app(APP, FakeSt(inputs={"Role": "Student", "Roll Number": "2026BTech006", "Access code": code}, clicks={"🔓 View my exam seats"}, session=other))
        self.assertFalse(other.get("student_ok"))                              # one student's code never opens another's seats

    def test_faculty_portal_login(self):
        self.approve()
        name = self.admin["state"]["faculty"][1]
        fid = auth.faculty_id(1)
        s = {}
        run_app(APP, FakeSt(inputs={"Role": "Faculty", "Faculty ID": fid, "Access code": auth.make_code(storage.get_secret(), "faculty", name)},
                            clicks={"🔓 View my duties"}, session=s))
        self.assertEqual(s.get("faculty_ok"), name)


if __name__ == "__main__":
    unittest.main()
