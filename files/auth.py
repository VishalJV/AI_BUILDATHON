"""
auth.py - role-based access helpers (no Streamlit imports).

* Admin   : PIN from the ADMIN_PIN env var / secrets (never shown in the UI).
* Faculty : Faculty ID (FAC001...) + personal access code.
* Student : Roll number + personal access code.

Access codes are derived with HMAC-SHA256 from a per-installation secret, so nothing has to be stored,
codes can't be guessed from a roll number, and the admin can export them to hand out.
"""
import base64
import hashlib
import hmac

CODE_LEN = 6


def make_code(secret, kind, ident):
    digest = hmac.new(secret.encode(), f"{kind}|{str(ident).strip().lower()}".encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode()[:CODE_LEN]          # A-Z / 2-7: no 0/1/8/9 look-alikes


def check_code(secret, kind, ident, supplied):
    expected = make_code(secret, kind, ident)
    return hmac.compare_digest(expected, str(supplied).strip().upper())


def check_pin(expected, supplied):
    return hmac.compare_digest(str(expected).encode(), str(supplied).encode())


def faculty_id(index):
    return f"FAC{index + 1:03d}"


def faculty_by_id(fid, faculty):
    """'fac003' -> the third faculty name, or None."""
    fid = str(fid).strip().upper()
    for i, name in enumerate(faculty):
        if faculty_id(i) == fid:
            return name
    return None


def credentials_table(secret, faculty, students):
    """Rows for the admin-only credentials export."""
    rows = [{"Role": "Faculty", "Login ID": faculty_id(i), "Name": f, "Access code": make_code(secret, "faculty", f)}
            for i, f in enumerate(faculty)]
    seen = set()
    for e in students:
        roll = e["RollNo"]
        if roll.lower() in seen:
            continue
        seen.add(roll.lower())
        rows.append({"Role": "Student", "Login ID": roll, "Name": e["Name"], "Access code": make_code(secret, "student", roll)})
    return rows
