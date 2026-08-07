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
                           ip="10.150.0.99", cid=199)


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
        ("act_disk", "act_disk", {"resize_disk": lambda n, s: {
            "name": n, "was": "20G", "now": "30G", "running": True,
            "filesystem": "/dev/vda1  28G  2G  26G  8% /"}}),
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

    # --- act_rmimage: deletes files, so it must be executed, not grepped -----
    #
    # It reaches into cli.describe_removal and into a RemovalPlan's fields. Both
    # are easy to break from the other side of the codebase, and the failure
    # mode is a traceback in the middle of a dialog that is about to delete
    # several gigabytes.
    from vmorch import images as images_mod
    demo_entry = images_mod.CatalogueEntry(
        key="demo-image", description="a test image", url="https://x/y.qcow2")
    fake_plan = images_mod.RemovalPlan(
        key="demo-image", description="a test image", entry=demo_entry,
        base=Path("/tmp/does-not-exist/demo-image.qcow2"),
        in_catalogue=True, sizes=(("base", 1234567),),
    )
    real_catalogue = images_mod.catalogue
    images_mod.catalogue = lambda **kw: {"demo-image": demo_entry}
    a = make_app()
    a.task = lambda title, fn, note="": fn()
    tui_ui.choose = lambda *a, **k: "demo-image"
    tui_ui.form = lambda *a, **k: {"keep_cache": False, "keep_entry": False,
                                   "force": False}
    tui_ui.pager = lambda *a, **k: None
    tui_ui.confirm = lambda *a, **k: True
    saved = (images_mod.plan_removal, images_mod.remove)
    removed = {}
    images_mod.plan_removal = lambda key, **kw: fake_plan
    images_mod.remove = lambda p, force=False: removed.setdefault("plan", p) or p
    try:
        a.act_rmimage()
        failures += check("act_rmimage runs and removes", "plan" in removed)
        failures += check("act_rmimage reports what it freed",
                          "1.2M" in a.status, a.status)
    except Exception as exc:                           # noqa: BLE001
        failures += check("act_rmimage runs and removes", False,
                          f"{type(exc).__name__}: {exc}")
    finally:
        images_mod.plan_removal, images_mod.remove = saved

    # Cancelling at the confirmation must delete nothing.
    a = make_app()
    a.task = lambda title, fn, note="": fn()
    tui_ui.confirm = lambda *a, **k: False
    cancelled = {}
    images_mod.plan_removal = lambda key, **kw: fake_plan
    images_mod.remove = lambda p, force=False: cancelled.setdefault("ran", True)
    try:
        a.act_rmimage()
        failures += check("declining the confirmation removes nothing",
                          "ran" not in cancelled)
    finally:
        images_mod.plan_removal, images_mod.remove = saved
        images_mod.catalogue = real_catalogue
    tui_ui.confirm = lambda *a, **k: True

    # --- act_menu must actually follow a submenu through to an action -------
    #
    # The menu is data now, and the menu test checks that data. This checks the
    # code that walks it: that "menu:storage" descends instead of being handed
    # to the dispatcher as an action name, that Esc in a submenu returns to the
    # menu above rather than closing everything, and that the leaf runs.
    box = fake_box()
    a = make_app(box)
    a.task = lambda title, fn, note="": fn()
    ran = []
    a.act_disk = lambda: ran.append("disk")

    picks = iter(["menu:storage", "disk"])
    tui_ui.choose = lambda *a, **k: next(picks, None)
    a.act_menu()
    failures += check("act_menu descends into a submenu and runs the action",
                      ran == ["disk"], str(ran))

    # Esc (None) in the submenu, then Esc again at the top: nothing runs, and
    # it must terminate rather than reopening the submenu forever.
    ran.clear()
    picks = iter(["menu:storage", None, None])
    tui_ui.choose = lambda *a, **k: next(picks, None)
    a.act_menu()
    failures += check("Esc backs out of a submenu without acting", ran == [])

    # Every submenu must be openable: a bad link would raise KeyError here.
    for name in tui_app.MENUS:
        if name == "main":
            continue
        ran.clear()
        picks = iter([f"menu:{name}", None, None])
        tui_ui.choose = lambda *a, **k: next(picks, None)
        try:
            a.act_menu()
            failures += check(f"submenu '{name}' opens", True)
        except Exception as exc:                       # noqa: BLE001
            failures += check(f"submenu '{name}' opens", False,
                              f"{type(exc).__name__}: {exc}")

    # --- the detail panel must render every box option ----------------------
    a = make_app(fake_box(sudo="nopasswd", nested=True))
    a.rebuild_rows = tui_app.App.rebuild_rows.__get__(a)
    a.rebuild_rows()
    text = " ".join(r.text for r in a.rows)
    for option in ("image", "resources", "disk", "network", "sudo", "nested"):
        failures += check(f"detail panel shows '{option}'", option in text)

    print("FAILED" if failures else "TUI actions execute correctly")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
