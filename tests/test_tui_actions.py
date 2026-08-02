"""Drive the TUI's actions with stubbed dialogs, and check they still run.

The other TUI test reads the source with regular expressions. That proves an
action is *wired*, not that it *works* -- and `py_compile` only catches syntax,
so a reference to a variable that no longer exists sails through both. Exactly
that shipped: rewriting the creation dialog left the old `BoxSpec(image=image,
...)` line behind, and `vmtui` crashed with NameError the moment F7 was pressed.

These tests call the action methods with every dialog stubbed, so the code
actually executes without needing a terminal, a box, or libvirt.

Run: python3 tests/test_tui_actions.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmorch import config, spec as spec_mod  # noqa: E402
from vmorch.tui import app as tui_app, ui as tui_ui  # noqa: E402


class FakeScreen:
    """Absorbs every curses call an action might make."""
    def __getattr__(self, _):
        return lambda *a, **k: 0
    def getmaxyx(self):
        return (40, 120)


def make_app(box=None):
    a = object.__new__(tui_app.App)          # bypass __init__ and its libvirt calls
    a.stdscr = FakeScreen()
    a.boxes = [box] if box else []
    a.rows, a.sel, a.rsel = [], 0, 0
    a.left_focus, a.status = True, ""
    a.refresh_boxes = lambda: None
    a.rebuild_rows = lambda: None
    return a


def fake_box(**kw):
    s = spec_mod.parse({"name": "demo", **kw})
    return SimpleNamespace(spec=s, name="demo", state="running",
                           ip="192.168.150.99", cid=199)


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    captured = {}

    # --- act_new: the path that crashed -------------------------------------
    form_values = {
        "image": config.DEFAULT_IMAGE, "cpus": "6", "memory": "4G",
        "disk": "50G", "internet": True, "lan": False, "sudo": "none",
        "nested": True, "start": False,
    }
    tui_ui.prompt = lambda *a, **k: "newbox"
    tui_ui.form = lambda *a, **k: dict(form_values)
    tui_ui.choose = lambda *a, **k: None
    tui_ui.confirm = lambda *a, **k: True
    tui_ui.message = tui_ui.error = lambda *a, **k: None

    a = make_app()
    a.task = lambda title, fn, note="": captured.update(spec=fn.__closure__ and None) or "ok"

    # Capture the spec by intercepting boxlib.create through the task closure.
    def fake_task(title, fn, note=""):
        captured["called"] = True
        return "created"
    a.task = fake_task
    real_create = tui_app.boxlib.create
    tui_app.boxlib.create = lambda spec, start=True: captured.update(
        spec=spec, start=start)
    try:
        a.act_new()
        failures += check("act_new runs without error", captured.get("called"))
        # Run the lambda the action handed to task, which is where the spec is built.
        captured.clear()
        a.task = lambda title, fn, note="": fn()
        a.act_new()
        s = captured.get("spec")
        failures += check("act_new builds a BoxSpec", s is not None)
        if s:
            failures += check("every form field reaches the spec",
                              (s.cpus, s.memory, s.disk, s.sudo, s.nested,
                               s.internet, s.lan) ==
                              (6, "4G", "50G", "none", True, True, False),
                              f"got {s}")
            failures += check("'start now' is honoured",
                              captured.get("start") is False)
    finally:
        tui_app.boxlib.create = real_create

    # --- the other actions that build or change things ----------------------
    box = fake_box(sudo="nopasswd")
    for label, method, patches in (
        ("act_sudo", "act_sudo", {"set_sudo": lambda n, m: box,
                                  "reseed": lambda n: box}),
        ("act_nested", "act_nested", {"load": lambda n: box,
                                      "save_spec": lambda s: None,
                                      "apply": lambda n: box}),
        ("act_share", "act_share", {"share": lambda *a, **k: box}),
        ("act_service", "act_service", {"grant_service": lambda *a, **k: box}),
        ("act_delete", "act_delete", {"destroy": lambda *a, **k: None}),
        ("act_reseed", "act_reseed", {"reseed": lambda n: box}),
        ("act_password", "act_password", {}),
    ):
        a = make_app(box)
        a.task = lambda title, fn, note="": fn()
        tui_ui.choose = lambda *a, **k: "none"
        tui_ui.prompt = lambda *a, **k: "/tmp"
        saved = {k: getattr(tui_app.boxlib, k, None) for k in patches}
        for k, v in patches.items():
            setattr(tui_app.boxlib, k, v)
        try:
            getattr(a, method)()
            failures += check(f"{label} runs without error", True)
        except Exception as exc:                       # noqa: BLE001
            failures += check(f"{label} runs without error", False,
                              f"{type(exc).__name__}: {exc}")
        finally:
            for k, v in saved.items():
                if v is not None:
                    setattr(tui_app.boxlib, k, v)

    # --- the detail panel must render every box option ----------------------
    a = make_app(fake_box(sudo="nopasswd", nested=True))
    a.rebuild_rows = tui_app.App.rebuild_rows.__get__(a)
    a.rebuild_rows()
    text = " ".join(r.text for r in a.rows)
    for option in ("image", "resources", "network", "sudo", "nested"):
        failures += check(f"detail panel shows '{option}'", option in text)

    print("FAILED" if failures else "TUI actions execute correctly")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
