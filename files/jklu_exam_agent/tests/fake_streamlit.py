"""Minimal fake of the Streamlit API so tests can execute app.py end-to-end (login, generate, chat, approve, portals)."""
import sys, types, contextlib

class RerunException(Exception): pass
class StopException(Exception): pass

class Ctx:
    def __init__(self, st): self.st = st
    def __enter__(self): return self.st
    def __exit__(self, *a): return False

class FakeSt:
    def __init__(self, inputs=None, clicks=None, session=None):
        self.session_state = session if session is not None else {}
        self.inputs, self.clicks = dict(inputs or {}), set(clicks or [])
        self.out = []          # (kind, payload)
        self.secrets = {}
        self.sidebar = self
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def _rec(self, kind, *a, **k):
        self.out.append((kind, a[0] if a else k))
    def _label(self, a, k):
        return a[0] if a else k.get("label", "")
    # --- containers
    def form(self, *a, **k): return Ctx(self)
    def expander(self, *a, **k): self._rec("expander", *a); return Ctx(self)
    def spinner(self, *a, **k): return Ctx(self)
    def columns(self, spec, *a, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [self for _ in range(n)]
    # --- inputs
    def text_input(self, *a, **k):
        lab = self._label(a, k); return self.inputs.get(lab, k.get("value", ""))
    def text_area(self, *a, **k):
        lab = self._label(a, k); return self.inputs.get(lab, k.get("value", ""))
    def selectbox(self, *a, **k):
        lab = self._label(a, k); opts = a[1] if len(a) > 1 else k.get("options")
        return self.inputs.get(lab, opts[k.get("index", 0)] if opts else None)
    def checkbox(self, *a, **k):
        lab = self._label(a, k); return self.inputs.get(lab, k.get("value", False))
    def number_input(self, *a, **k): return k.get("value", 0)
    def slider(self, *a, **k): return a[3] if len(a) > 3 else k.get("value", 1)
    def radio(self, *a, **k): return (a[1] if len(a) > 1 else k["options"])[0]
    def button(self, *a, **k): return self._label(a, k) in self.clicks
    def form_submit_button(self, *a, **k): return self._label(a, k) in self.clicks
    def download_button(self, *a, **k): self._rec("download", self._label(a, k), data=k.get("data")); return False
    # --- control flow
    def rerun(self): raise RerunException()
    def stop(self): raise StopException()
    def set_page_config(self, *a, **k): pass
    # --- outputs (recorded)
    def __getattr__(self, name):
        if name.startswith("__"): raise AttributeError(name)
        def f(*a, **k):
            self._rec(name, *a, **k)
        return f

def run_app(path, st, max_reruns=6):
    """Execute the script like Streamlit does: restart on st.rerun(), end on st.stop()."""
    sys.modules["streamlit"] = st
    with open(path, encoding="utf-8") as fh:
        code = compile(fh.read(), path, "exec")
    for _ in range(max_reruns):
        st.out.clear()
        try:
            exec(code, {"__name__": "__main__"})
            return
        except RerunException:
            st.clicks.clear()          # a rerun after a click no longer sees the click
            continue
        except StopException:
            return
    raise RuntimeError("too many reruns")
