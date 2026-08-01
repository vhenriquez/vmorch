"""Norton Commander style TUI for vmorch.

Layout follows the original closely, because the muscle memory is worth having:
two framed panels side by side, Tab moves between them, and a function-key strip
is pinned to the bottom line. F8 deletes, F9 opens the pull-down menu, F10 quits
-- the keys land where a Commander user expects.

The adaptation: the LEFT panel lists boxes, the RIGHT panel lists the selected
box's grants -- its folders, services and snapshots. Actions are contextual on
the focused panel, exactly as Commander's F5/F8 act on whichever side is active.
Delete on the left destroys a box; delete on the right revokes the one grant
under the cursor.

Every destructive path routes through a confirmation, and a writable share is
called out in yellow before it is granted, matching what the CLI prints.
"""

from __future__ import annotations

import curses
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import boxes as boxlib
from .. import config, consoletext, images, network
from ..spec import BoxSpec
from . import ui
from .ui import attr, frame, fill, put

KEYBAR = [
    ("1", "Help"), ("2", "Snap"), ("3", "View"), ("4", "Edit"), ("5", "Share"),
    ("6", "Srvc"), ("7", "New"), ("8", "Del"), ("9", "Menu"), ("10", "Quit"),
]


@dataclass
class Row:
    """A line in the right panel. `kind` decides what F8 and Enter do."""
    kind: str          # header | folder | service | snapshot | info
    text: str
    value: object = None
    warn: bool = False

    @property
    def selectable(self) -> bool:
        return self.kind in ("folder", "service", "snapshot")


class App:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.boxes: list[boxlib.Box] = []
        self.rows: list[Row] = []
        self.sel = 0            # index into self.boxes
        self.rsel = 0           # index into self.rows
        self.left_focus = True
        self.status = "Ready"
        self.refresh_boxes()

    # -- data ------------------------------------------------------------

    def refresh_boxes(self) -> None:
        try:
            self.boxes = boxlib.list_boxes()
        except Exception as exc:                       # noqa: BLE001
            self.boxes = []
            self.status = f"Could not list boxes: {exc}"
        self.sel = min(self.sel, max(0, len(self.boxes) - 1))
        self.rebuild_rows()

    @property
    def current(self) -> boxlib.Box | None:
        return self.boxes[self.sel] if self.boxes else None

    def rebuild_rows(self) -> None:
        self.rows = []
        box = self.current
        if box is None:
            self.rsel = 0
            return
        s = box.spec
        add = self.rows.append

        add(Row("header", "Configuration"))
        add(Row("info", f"image      {s.image}"))
        add(Row("info", f"resources  {s.cpus} cpu · {s.memory} ram · {s.disk} disk"))
        add(Row("info", f"address    {box.ip}   vsock cid {box.cid}"))
        net = ("internet + LAN" if s.lan else "internet only") if s.internet \
            else "isolated (no network out)"
        add(Row("info", f"network    {net}"))

        add(Row("header", ""))
        add(Row("header", f"Folders ({len(s.folders)})"))
        if not s.folders:
            add(Row("info", "  (none shared)"))
        for f in s.folders:
            mode = "ro" if f.readonly else "RW"
            add(Row("folder", f"  [{mode}] {f.tag:<10} {f.host}", f.tag,
                    warn=not f.readonly))

        add(Row("header", ""))
        add(Row("header", f"Services ({len(s.from_host)})"))
        if not s.from_host:
            add(Row("info", "  (none granted)"))
        for svc in s.from_host:
            add(Row("service",
                    f"  {svc.name:<10} host:{svc.host_port} → guest:"
                    f"{svc.guest_port}  via {svc.via}", svc.name))

        try:
            snaps = boxlib.list_snapshots(box.name)
        except Exception:                              # noqa: BLE001
            snaps = []
        add(Row("header", ""))
        add(Row("header", f"Snapshots ({len(snaps)}/{config.MAX_SNAPSHOT_LAYERS})"))
        if not snaps:
            add(Row("info", "  (none)"))
        for sn in snaps:
            add(Row("snapshot", f"  {sn.index:>2}  {sn.created}  {sn.label}",
                    sn.index))

        if self.rsel >= len(self.rows):
            self.rsel = 0
        self._snap_to_selectable(1)

    def _snap_to_selectable(self, direction: int) -> None:
        """Keep the right-panel cursor off headers and info lines."""
        if not any(r.selectable for r in self.rows):
            return
        n = len(self.rows)
        for _ in range(n):
            if 0 <= self.rsel < n and self.rows[self.rsel].selectable:
                return
            self.rsel = (self.rsel + direction) % n

    # -- drawing ---------------------------------------------------------

    MIN_W, MIN_H = 72, 16

    def draw(self) -> None:
        stdscr = self.stdscr
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        if w < self.MIN_W or h < self.MIN_H:
            # Two framed panels simply do not fit. Say so plainly rather than
            # drawing something unreadable.
            msg = f"Terminal too small: need {self.MIN_W}x{self.MIN_H}, have {w}x{h}"
            put(stdscr, max(0, h // 2), max(0, (w - len(msg)) // 2),
                msg[:max(0, w)], attr(ui.WARN, bold=True))
            stdscr.refresh()
            return

        fill(stdscr, 0, 0, h, w, attr(ui.FIELD))

        title = " vmorch — agent sandbox boxes "
        put(stdscr, 0, 0, " " * w, attr(ui.TITLE))
        put(stdscr, 0, max(0, (w - len(title)) // 2), title,
            attr(ui.TITLE, bold=True))

        panel_h = h - 4
        lw = max(28, w // 2 - 1)
        rw = w - lw - 1
        self._draw_boxes(1, 0, panel_h, lw)
        self._draw_detail(1, lw + 1, panel_h, rw)

        put(stdscr, h - 3, 0, " " * w, attr(ui.STATUS))
        put(stdscr, h - 3, 1, self.status[: w - 2], attr(ui.STATUS, bold=True))

        hint = "Tab switch · Enter ssh · Space start/stop"
        put(stdscr, h - 2, 0, " " * w, attr(ui.FIELD))
        put(stdscr, h - 2, max(1, w - len(hint) - 1), hint, attr(ui.DIM))

        self._draw_keybar(h - 1, w)
        stdscr.refresh()

    def _draw_keybar(self, y: int, w: int) -> None:
        put(self.stdscr, y, 0, " " * w, attr(ui.KEYBAR_L))
        cell = max(7, w // len(KEYBAR))
        for i, (num, label) in enumerate(KEYBAR):
            x = i * cell
            put(self.stdscr, y, x, num, attr(ui.KEYBAR_N))
            put(self.stdscr, y, x + len(num),
                label.ljust(cell - len(num))[: cell - len(num)],
                attr(ui.KEYBAR_L))

    def _draw_boxes(self, y: int, x: int, h: int, w: int) -> None:
        active = self.left_focus
        frame(self.stdscr, y, x, h, w, f"Boxes ({len(self.boxes)})",
              attr(ui.FRAME, bold=active), double=active)
        put(self.stdscr, y + 1, x + 2,
            "Name          State     Address".ljust(w - 4)[: w - 4],
            attr(ui.DIM, bold=True))
        put(self.stdscr, y + 2, x + 1, "─" * (w - 2), attr(ui.FRAME))

        view_h = h - 4
        top = max(0, min(self.sel - view_h + 1, len(self.boxes) - view_h))
        top = max(0, top)
        for i in range(view_h):
            idx = top + i
            if idx >= len(self.boxes):
                break
            box = self.boxes[idx]
            selected = idx == self.sel
            a = attr(ui.SELECT if active else ui.SELECT_DIM, bold=True) \
                if selected else attr(ui.PANEL)
            running = box.state == "running"
            mark = "●" if running else "○"
            line = f" {mark} {box.name[:12]:<12} {box.state[:9]:<9} {box.ip}"
            put(self.stdscr, y + 3 + i, x + 1, line.ljust(w - 2)[: w - 2], a)
            if not selected:
                put(self.stdscr, y + 3 + i, x + 2, mark,
                    attr(ui.OK if running else ui.DIM, bold=True))

        if not self.boxes:
            put(self.stdscr, y + 4, x + 3, "No boxes yet — press F7",
                attr(ui.DIM))

    def _draw_detail(self, y: int, x: int, h: int, w: int) -> None:
        active = not self.left_focus
        box = self.current
        title = f"Box: {box.name}" if box else "Details"
        frame(self.stdscr, y, x, h, w, title,
              attr(ui.FRAME, bold=active), double=active)
        if box is None:
            return

        state_a = attr(ui.OK if box.state == "running" else ui.DIM, bold=True)
        put(self.stdscr, y + 1, x + 2, f"● {box.state}", state_a)
        put(self.stdscr, y + 2, x + 1, "─" * (w - 2), attr(ui.FRAME))

        view_h = h - 4
        top = max(0, min(self.rsel - view_h + 1, len(self.rows) - view_h))
        top = max(0, top)
        for i in range(view_h):
            idx = top + i
            if idx >= len(self.rows):
                break
            row = self.rows[idx]
            selected = idx == self.rsel and row.selectable
            if selected:
                a = attr(ui.SELECT if active else ui.SELECT_DIM, bold=True)
            elif row.kind == "header":
                a = attr(ui.FRAME, bold=True)
            elif row.warn:
                a = attr(ui.WARN, bold=True)
            else:
                a = attr(ui.PANEL)
            put(self.stdscr, y + 3 + i, x + 1,
                (" " + row.text).ljust(w - 2)[: w - 2], a)

    # -- helpers ---------------------------------------------------------

    def task(self, title: str, fn, note: str = "") -> object | None:
        ok, res = ui.run_task(self.stdscr, title, fn, note)
        if not ok:
            ui.error(self.stdscr, str(res))
            self.refresh_boxes()
            return None
        return res

    def shell_out(self, argv: list[str]) -> None:
        """Drop out of curses to run an interactive command, then restore."""
        curses.endwin()
        try:
            subprocess.call(argv)
        finally:
            self.stdscr.clear()
            curses.doupdate()
            curses.curs_set(0)

    # -- actions ---------------------------------------------------------

    def act_ssh(self) -> None:
        box = self.current
        if not box:
            return
        if box.state != "running":
            if not ui.confirm(self.stdscr, "Start box",
                              f"{box.name} is {box.state}. Start it and connect?"):
                return
            if self.task("Starting", lambda: boxlib.start(box.name)) is None:
                return
            self.task("Waiting for ssh",
                      lambda: boxlib._wait_reachable(box.name, 300),
                      "Waiting for the box to finish booting.")
        self.shell_out(["ssh", box.name])
        self.refresh_boxes()
        self.status = f"Returned from {box.name}"

    def act_toggle(self) -> None:
        box = self.current
        if not box:
            return
        if box.state == "running":
            if not ui.confirm(self.stdscr, "Stop box",
                              f"Shut down {box.name} gracefully?"):
                return
            self.task("Stopping", lambda: boxlib.stop(box.name))
            self.status = f"{box.name} shutting down"
        else:
            self.task("Starting", lambda: boxlib.start(box.name))
            self.status = f"{box.name} starting"
        self.refresh_boxes()

    def act_new(self) -> None:
        name = ui.prompt(self.stdscr, "New box", "Name:")
        if not name:
            return
        # The default image leads, and unverified ones are labelled. Sorting
        # alphabetically would put a known-broken image first, where pressing
        # Enter through the dialog lands on it.
        entries = sorted(
            images.catalogue().items(),
            key=lambda kv: (kv[0] != config.DEFAULT_IMAGE, kv[1].broken,
                            not kv[1].verified, kv[0]),
        )
        image = ui.choose(
            self.stdscr, "Image",
            [(f"{'x ' if e.broken else ('? ' if not e.verified else '  ')}"
              f"{k:<14} {e.description[:32]}", k)
             for k, e in entries],
            note="x = known broken.  ? = added but not booted yet.",
        )
        if image is None:
            return
        net = ui.choose(self.stdscr, "Network access", [
            ("Isolated — no network out (default)", (False, False)),
            ("Internet only — no LAN", (True, False)),
            ("Internet + LAN — router, NAS, other hosts", (True, True)),
        ], note="Isolated boxes can still reach granted host services.")
        if net is None:
            return
        memory = ui.prompt(self.stdscr, "New box", "Memory:", config.DEFAULT_MEMORY)
        if memory is None:
            return
        disk = ui.prompt(self.stdscr, "New box", "Disk:", config.DEFAULT_DISK)
        if disk is None:
            return

        spec = BoxSpec(name=name, image=image, memory=memory, disk=disk,
                       internet=net[0], lan=net[1])
        res = self.task("Creating box", lambda: boxlib.create(spec),
                        f"Building {name} from {image}. First boot takes a minute.")
        self.refresh_boxes()
        if res is not None:
            self.status = f"Created {name} — ssh {name}"
            for i, b in enumerate(self.boxes):
                if b.name == name:
                    self.sel = i
            self.rebuild_rows()

    def act_delete(self) -> None:
        """Contextual, like Commander: acts on the focused panel."""
        box = self.current
        if not box:
            return
        if self.left_focus:
            if not ui.confirm(
                self.stdscr, "Destroy box",
                f"Destroy {box.name} and its disk?\n\n"
                f"Its address {box.ip} and vsock cid {box.cid} stay reserved "
                "and are never reused.", danger=True,
            ):
                return
            self.task("Destroying", lambda: boxlib.destroy(box.name))
            self.status = f"Destroyed {box.name}"
            self.refresh_boxes()
            return

        row = self.rows[self.rsel] if self.rsel < len(self.rows) else None
        if row is None or not row.selectable:
            return
        if row.kind == "folder":
            if ui.confirm(self.stdscr, "Revoke folder",
                          f"Stop sharing '{row.value}' with {box.name}?"):
                self.task("Revoking", lambda: boxlib.unshare(box.name, row.value))
                self.status = f"Unshared {row.value}"
        elif row.kind == "service":
            if ui.confirm(self.stdscr, "Revoke service",
                          f"Revoke '{row.value}' from {box.name}?"):
                self.task("Revoking",
                          lambda: boxlib.revoke_service(box.name, row.value))
                self.status = f"Revoked {row.value}"
        elif row.kind == "snapshot":
            ui.message(self.stdscr, "Snapshots",
                       "Snapshots are pruned automatically once the chain "
                       f"exceeds {config.MAX_SNAPSHOT_LAYERS} layers. "
                       "Use Enter to roll back to one.")
            return
        self.refresh_boxes()

    def act_share(self) -> None:
        box = self.current
        if not box:
            return
        path = ui.prompt(self.stdscr, "Share folder", "Host path:")
        if not path:
            return
        mode = ui.choose(
            self.stdscr, "Access mode", [
                ("Read-only  (recommended)", "ro"),
                ("Read-write (the box can modify the host)", "rw"),
            ],
            note="An agent has root in the box. A writable share is the one "
                 "realistic path back to the host — it can plant a git hook or "
                 "edit a Makefile you later run.",
        )
        if mode is None:
            return
        if mode == "rw" and not ui.confirm(
            self.stdscr, "Grant write access",
            f"Let {box.name} WRITE to {path}?", danger=True,
        ):
            return
        tag = ui.prompt(self.stdscr, "Share folder", "Mount tag:",
                        Path(path).expanduser().name)
        if tag is None:
            return
        res = self.task("Sharing folder",
                        lambda: boxlib.share(box.name, Path(path), tag, mode),
                        "Attaching virtiofs and mounting in the guest.")
        if res is not None:
            self.status = f"Shared {path} → /mnt/{tag} [{mode}]"
        self.refresh_boxes()

    def act_service(self) -> None:
        box = self.current
        if not box:
            return
        name = ui.prompt(self.stdscr, "Grant service", "Service name:", "ollama")
        if not name:
            return
        port = ui.prompt(self.stdscr, "Grant service", "Host port:",
                         "11434" if name == "ollama" else "")
        if not port:
            return
        try:
            port_i = int(port)
        except ValueError:
            ui.error(self.stdscr, f"'{port}' is not a port number")
            return
        if not ui.confirm(
            self.stdscr, "Grant service",
            f"Let {box.name} reach host service '{name}' on port {port_i}?\n\n"
            "This opens a hole in the guest→host block. The service's own "
            "authentication is the only control behind it.",
        ):
            return
        res = self.task("Granting service",
                        lambda: boxlib.grant_service(box.name, name, port_i,
                                                     port_i, "filter"),
                        "Updating the box filter and installing the relay.")
        if res is not None:
            self.status = f"Granted {name} to {box.name}"
        self.refresh_boxes()

    def act_snapshot(self) -> None:
        box = self.current
        if not box:
            return
        if box.state == "running":
            ui.error(self.stdscr,
                     f"Stop {box.name} first. Snapshotting a live box gives a "
                     "crash-consistent image at best.")
            return
        label = ui.prompt(self.stdscr, "Snapshot", "Label:", "")
        if label is None:
            return
        res = self.task("Snapshotting",
                        lambda: boxlib.snapshot(box.name, label or None))
        if res is not None:
            self.status = (f"Snapshot {res.index} ({res.label}) — shared folders "
                           "are NOT covered")
        self.refresh_boxes()

    def act_rollback(self) -> None:
        box = self.current
        row = self.rows[self.rsel] if self.rsel < len(self.rows) else None
        if not box or row is None or row.kind != "snapshot":
            return
        if box.state == "running":
            ui.error(self.stdscr, f"Stop {box.name} first.")
            return
        if not ui.confirm(
            self.stdscr, "Roll back",
            f"Rewind {box.name} to snapshot {row.value}?\n\n"
            "Every newer snapshot and all changes since are discarded. "
            "Shared folders are NOT rolled back — anything the agent wrote "
            "into a writable share is already on the host and stays.",
            danger=True,
        ):
            return
        res = self.task("Rolling back",
                        lambda: boxlib.rollback(box.name, row.value))
        if res is not None:
            self.status = f"Rolled back to {res.index} ({res.label})"
        self.refresh_boxes()

    def act_view(self) -> None:
        box = self.current
        if not box:
            return
        log = boxlib.console_log(box.name)
        if not log.exists():
            ui.error(self.stdscr, "No console log yet.")
            return
        try:
            text = log.read_text(errors="replace")
        except PermissionError:
            ui.error(self.stdscr, f"Cannot read {log} — check the ACL.")
            return
        # Raw console output is a terminal recording: ANSI colour, carriage
        # returns, stray NULs. Strip the control codes and rebuild the meaning
        # from the text markers instead.
        clean = consoletext.sanitize(text)
        kinds = {
            consoletext.FAIL: ui.ERROR,
            consoletext.WARN: ui.WARN,
            consoletext.OK: ui.OK,
            consoletext.PLAIN: ui.DIALOG,
        }
        ui.pager(
            self.stdscr, f"Console — {box.name}", clean or "(empty)",
            line_attr=lambda ln: kinds[consoletext.classify(ln)],
            footer=consoletext.summary(clean),
        )

    def act_edit(self) -> None:
        box = self.current
        if not box:
            return
        path = boxlib.spec_path(box.name)
        editor = os.environ.get("EDITOR", "nano")
        self.shell_out([editor, str(path)])
        # The spec is the source of truth, so an edit means the domain XML is
        # now stale. Offer to reconcile rather than leaving them disagreeing.
        if ui.confirm(self.stdscr, "Apply changes",
                      f"Apply the edited spec to {box.name}?"):
            self.task("Applying", lambda: boxlib.apply(box.name))
            self.status = f"Applied spec for {box.name}"
        self.refresh_boxes()

    def act_menu(self) -> None:
        box = self.current
        choice = ui.choose(self.stdscr, "Menu", [
            ("New box                       F7", "new"),
            ("Start / stop selected      Space", "toggle"),
            ("Connect over ssh           Enter", "ssh"),
            ("Apply spec (reconcile)", "apply"),
            ("Share a folder                F5", "share"),
            ("Grant a host service          F6", "service"),
            ("Take a snapshot               F2", "snap"),
            ("View console log              F3", "view"),
            ("Edit box spec                 F4", "edit"),
            ("Image catalogue", "images"),
            ("Re-mount shared folders", "mount"),
            ("Build a golden image...", "golden"),
            ("Ensure management network", "net"),
            ("Destroy box                   F8", "del"),
            ("Help                          F1", "help"),
            ("Quit                         F10", "quit"),
        ])
        actions = {
            "new": self.act_new, "toggle": self.act_toggle, "ssh": self.act_ssh,
            "share": self.act_share, "service": self.act_service,
            "snap": self.act_snapshot, "view": self.act_view,
            "edit": self.act_edit, "del": self.act_delete, "help": self.act_help,
        }
        if choice in actions:
            actions[choice]()
        elif choice == "apply" and box:
            self.task("Applying", lambda: boxlib.apply(box.name))
            self.refresh_boxes()
            self.status = f"Applied {box.name}"
        elif choice == "images":
            lines = []
            for k, e in sorted(images.catalogue().items()):
                marks = []
                if e.cached.exists():
                    marks.append("cached")
                if images.base_path(e).exists():
                    marks.append("base ready")
                lines.append(f"{k:<14} {e.description}\n"
                             f"{'':<14} {', '.join(marks) or 'not downloaded'}")
            ui.pager(self.stdscr, "Image catalogue", "\n".join(lines))
        elif choice == "mount" and box:
            res = self.task("Mounting", lambda: boxlib.sync_mounts(box.name))
            if res is not None:
                self.status = f"mounted {len(res)} share(s) on {box.name}"
            self.refresh_boxes()
        elif choice == "golden":
            ui.message(self.stdscr, "Golden images",
                       "Building a golden image boots a temporary box, installs "
                       "into it and flattens the result. It takes several "
                       "minutes and is best watched, so run it from the CLI:\n\n"
                       "  vm golden agent-base --packages tmux,git")
        elif choice == "net":
            res = self.task("Network", network.ensure_base)
            if res is not None:
                self.status = (f"{config.MGMT_NET} ready on "
                               f"{config.MGMT_SUBNET}")
        elif choice == "quit":
            raise KeyboardInterrupt

    def act_help(self) -> None:
        ui.pager(self.stdscr, "Help", HELP)

    # -- main loop -------------------------------------------------------

    def loop(self) -> None:
        while True:
            self.draw()
            k = self.stdscr.getch()

            if k == curses.KEY_RESIZE:
                continue
            if k in (ord("\t"),):
                self.left_focus = not self.left_focus
                continue
            if k in (curses.KEY_F10, ord("q")):
                if ui.confirm(self.stdscr, "Quit", "Leave vmorch?"):
                    return
                continue

            if k in (curses.KEY_UP, ord("k")):
                if self.left_focus:
                    if self.boxes:
                        self.sel = (self.sel - 1) % len(self.boxes)
                        self.rsel = 0
                        self.rebuild_rows()
                elif self.rows:
                    self.rsel = (self.rsel - 1) % len(self.rows)
                    self._snap_to_selectable(-1)
            elif k in (curses.KEY_DOWN, ord("j")):
                if self.left_focus:
                    if self.boxes:
                        self.sel = (self.sel + 1) % len(self.boxes)
                        self.rsel = 0
                        self.rebuild_rows()
                elif self.rows:
                    self.rsel = (self.rsel + 1) % len(self.rows)
                    self._snap_to_selectable(1)
            elif k in (curses.KEY_LEFT,):
                self.left_focus = True
            elif k in (curses.KEY_RIGHT,):
                self.left_focus = False
            elif k in (10, 13):
                if not self.left_focus and self.rows and \
                        self.rsel < len(self.rows) and \
                        self.rows[self.rsel].kind == "snapshot":
                    self.act_rollback()
                else:
                    self.act_ssh()
            elif k == ord(" "):
                self.act_toggle()
            elif k in (curses.KEY_F1, ord("?")):
                self.act_help()
            elif k == curses.KEY_F2:
                self.act_snapshot()
            elif k == curses.KEY_F3:
                self.act_view()
            elif k == curses.KEY_F4:
                self.act_edit()
            elif k == curses.KEY_F5:
                self.act_share()
            elif k == curses.KEY_F6:
                self.act_service()
            elif k == curses.KEY_F7:
                self.act_new()
            elif k in (curses.KEY_F8, curses.KEY_DC):
                self.act_delete()
            elif k == curses.KEY_F9:
                self.act_menu()
            elif k == ord("r"):
                self.refresh_boxes()
                self.status = "Refreshed"


HELP = """vmorch — Norton Commander style control panel

NAVIGATION
  Tab / ← →        switch between the Boxes and Details panels
  ↑ ↓ / k j        move the cursor
  r                reload from disk

BOXES PANEL (left)
  Enter            ssh into the box, starting it first if needed
  Space            start or stop the box
  F7               create a new box
  F8               destroy the box and its disk

DETAILS PANEL (right)
  F8               revoke whatever the cursor is on: a shared folder,
                   a granted service
  Enter            on a snapshot row, roll back to it

ACTIONS
  F2               take a snapshot (the box must be stopped)
  F3               view the console log
  F4               edit the box spec in $EDITOR, then apply
  F5               share a host folder
  F6               grant access to a host service
  F9               menu — everything, including the image catalogue
  F10 / q          quit

THINGS WORTH KNOWING

  Read-only is the default for shared folders, and it holds even against
  root inside the box: <readonly/> is enforced host-side by virtiofsd, so
  remounting rw in the guest still cannot write. A writable share is the
  one realistic path back to the host, which is why it is confirmed
  separately and shown in yellow.

  Snapshots do NOT cover shared folders. virtiofs mounts are the live host
  filesystem, so rolling a box back does not undo anything the agent wrote
  into a writable share.

  Isolated boxes still reach granted host services. A box with no internet
  can use the host's Ollama for GPU work.

  Addresses and vsock CIDs are never reused. Destroying a box tombstones
  its identifiers so a stale relay can never be handed a different box.
"""


def main() -> int:
    def run(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        ui.init_colors()
        # Paint the default background too, so any cell we never write to
        # matches the field instead of showing the terminal's own colours.
        stdscr.bkgd(" ", ui.attr(ui.PANEL))
        App(stdscr).loop()

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0
