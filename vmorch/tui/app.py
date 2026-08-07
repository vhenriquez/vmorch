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
from .. import virsh
from .. import config, consoletext, images, network
from .. import nets as netlib
from ..spec import BoxSpec
from . import ui
from .ui import attr, frame, fill, put

#: How often the box list re-checks run states, in milliseconds.
POLL_MS = 1500

#: One description per box option, used by BOTH the detail panel and the
#: creation form. Single source so an option cannot be explained in one place
#: and left bare in the other.
OPTION_HELP = {
    "image":    "Base image the box is built from. Golden images carry software already installed.",
    "cpus":     "Virtual CPUs given to the box.",
    "memory":   "RAM given to the box.",
    "disk":     "Maximum disk size. A ceiling, not an allocation -- a box uses only what it writes. Can be grown later (F9 -> Grow the disk), never shrunk, so err generous.",
    "internet": "Reach the PUBLIC internet. Does not include your local network.",
    "lan":      "Also reach the local network: router, NAS, other machines. Off by default.",
    "sudo":     "What the agent inside may escalate to. The boundary is the VM, so this is defence in depth.",
    "nested":   "Expose vmx/svm so the box can run its OWN virtual machines. Needed for the Android emulator and Genymotion; redroid does not need it. Real extra attack surface -- leave off unless required.",
    "start":    "Start the box immediately after creating it.",
}

#: Options where "on" widens what the box can reach or do. Shown in warning
#: colour so the risky setting is never the quiet one.
RISKY_WHEN_ON = {"lan", "nested"}

# The bottom strip. Ten slots, and the scarcest space in the interface, so a
# slot has to earn itself twice over: by being reached often, and by not being
# somewhere obvious already.
#
# F1/F3/F4/F7/F8/F9/F10 keep their Norton Commander meanings, because the muscle
# memory is real and free. The three with no Commander equivalent are chosen on
# frequency:
#
#   F2  Apply     the core loop of a *reconfigurable* sandbox is edit-then-apply.
#                 It sat in the menu with no key while F2 held Snap.
#   F5  Share     Commander's F5 is copy; attaching a folder is the nearest
#                 thing, and it is the commonest way to give an agent its work.
#   F6  Net       attach a box to a local network. Pairs with F5: both are
#                 "give this box a resource".
#
# Demoted, deliberately: Snap and Srvc. Both are set-once-per-box operations --
# a keybar slot for something used once in a box's life is a slot wasted, and
# both are one keystroke away in the menu.
KEYBAR = [
    ("1", "Help"), ("2", "Apply"), ("3", "View"), ("4", "Edit"), ("5", "Share"),
    ("6", "Net"), ("7", "New"), ("8", "Del"), ("9", "Menu"), ("10", "Quit"),
]


# --------------------------------------------------------------------------
# The menu
#
# Declared as data, not built inline, so it can be read at a glance and checked
# by a test rather than by a regular expression over this file.
#
# It grew to twenty-four flat entries covering four unrelated scopes -- one box,
# all boxes, the image catalogue, the host -- in a single undifferentiated list.
# Long menus are not the problem; unsorted ones are. So:
#
#   * the top level holds the handful of actions people reach for constantly,
#     then one entry per group for everything else;
#   * groups are named for the *thing being changed*, because that is how
#     someone looks for an action they have not used before;
#   * every leaf carries a letter, so any action is F9 plus one keystroke and
#     the menu never has to be read twice;
#   * entries that act on a box are marked, and go dim with a reason when no
#     box is selected instead of being picked and silently doing nothing.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    label: str
    value: str = ""
    key: str = ""
    hint: str = ""
    header: bool = False
    #: Needs a selected box. The four global groups do not.
    box: bool = True

    @staticmethod
    def sep(label: str) -> "Entry":
        return Entry(label=label, header=True)


#: value -> (title, entries). "main" is what F9 opens; the rest are submenus,
#: reached by a `menu:<name>` value.
MENUS: dict[str, tuple[str, list[Entry]]] = {
    "main": ("Menu", [
        Entry.sep("This box"),
        Entry("Connect over ssh", "ssh", "c", "Enter"),
        Entry("Start / stop", "toggle", "s", "Space"),
        Entry("Apply spec (reconcile)", "apply", "a", "F2"),
        Entry("Edit box spec", "edit", "e", "F4"),
        Entry.sep("More for this box"),
        Entry("Folders and services...", "menu:sharing", "f"),
        Entry("Networks...", "menu:networks", "n", "F6"),
        Entry("Disk and snapshots...", "menu:storage", "d"),
        Entry("Privileges and hardware...", "menu:privilege", "p"),
        Entry("Troubleshoot...", "menu:trouble", "t"),
        Entry("Destroy box", "del", "x", "F8"),
        Entry.sep("Everything else"),
        Entry("New box", "new", "w", "F7", box=False),
        Entry("Images...", "menu:images", "i", box=False),
        Entry("Host and settings...", "menu:system", "h", box=False),
        Entry("Help", "help", "?", "F1", box=False),
        Entry("Quit", "quit", "q", "F10", box=False),
    ]),
    # Both scopes live here because they are the same subject, which is the
    # distinction that matters: the earlier menu mixed things with no relation
    # at all. Headers keep "this box" and "all networks" apart.
    "networks": ("Networks", [
        Entry.sep("This box"),
        Entry("Attach to a local network", "netattach", "a"),
        Entry("Detach from a local network", "netdetach", "d"),
        Entry("Change internet / LAN access", "edit", "i", "F4"),
        Entry.sep("Local networks — members-only, no gateway or internet"),
        Entry("List networks and their members", "netls", "l", box=False),
        Entry("Create a local network", "netcreate", "c", box=False),
        Entry("Delete a local network", "netrm", "r", box=False),
    ]),
    "storage": ("Disk and snapshots", [
        Entry.sep("Disk"),
        Entry("Grow the disk (never shrinks)", "disk", "g"),
        Entry.sep("Snapshots — box must be stopped"),
        Entry("Take a snapshot", "snap", "t"),
        Entry("Roll back to a snapshot", "rollback", "r"),
    ]),
    "sharing": ("Folders and services", [
        Entry.sep("Host folders"),
        Entry("Share a folder", "share", "s", "F5"),
        Entry("Re-mount shared folders", "mount", "m"),
        Entry.sep("Host services"),
        Entry("Grant a host service", "service", "g"),
        Entry.sep("To revoke either, select it on the right and press F8"),
    ]),
    "privilege": ("Privileges and hardware", [
        Entry.sep("What the agent inside may do"),
        Entry("Set the agent's sudo rights", "sudo", "s"),
        Entry("Show the sudo password", "password", "p"),
        Entry.sep("Hardware exposed to the box"),
        Entry("Toggle nested virtualisation", "nested", "n"),
        Entry.sep("Network access lives under Networks (F6)"),
    ]),
    "trouble": ("Troubleshoot", [
        Entry("View console log", "view", "v", "F3"),
        Entry("Reseed: repair a box that refuses ssh", "reseed", "r"),
        Entry("Audit: what this box looked up and reached", "auditbox", "a"),
    ]),
    "images": ("Images", [
        Entry("Image catalogue", "images", "c", box=False),
        Entry("Remove an image and its files", "rmimage", "r", box=False),
        Entry("Build a golden image", "golden", "g", box=False),
    ]),
    "system": ("Host and settings", [
        Entry("Configuration and disk usage", "config", "c", box=False),
        Entry("Audit: all boxes, last 24h", "audit", "a", box=False),
        Entry("Ensure management network", "net", "n", box=False),
    ]),
}


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

    def refresh_states(self) -> bool:
        """Cheap poll: update just the run states. True if anything changed.

        The panel used to go stale the moment anything happened that the TUI
        did not itself do -- a graceful shutdown finishing ten seconds later, a
        box started from another terminal or virt-manager. One virsh call keeps
        it honest without re-reading every spec.
        """
        try:
            states = virsh.all_domain_states()
        except Exception:                              # noqa: BLE001
            return False
        changed = False
        for box in self.boxes:
            now = states.get(box.spec.domain, "absent")
            if now != box.state:
                box.state = now
                changed = True
        return changed

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
        add(Row("info", f"resources  {s.cpus} cpu · {s.memory} ram"))
        # Disk gets its own line with the way to change it, because the natural
        # place to look for that is this panel, not the F9 menu.
        add(Row("info", f"disk       {s.disk}  ·  grow: F9 → Grow the disk "
                        "(cannot shrink)"))
        add(Row("info", f"address    {box.ip}   vsock cid {box.cid}"))
        net = ("internet + LAN" if s.lan else "internet only") if s.internet \
            else "isolated (no network out)"
        add(Row("info", f"network    {net}", warn=s.lan))
        sudo_text = {
            "nopasswd": "passwordless — agent can become root",
            "password": "password required — secret on host",
            "none": "none — agent cannot escalate",
        }.get(s.sudo, s.sudo)
        add(Row("info", f"sudo       {sudo_text}", warn=s.sudo == "nopasswd"))
        add(Row("info",
                "nested     " + ("yes — box can run its own VMs (emulators)"
                                 if s.nested else "no — no hardware virt inside"),
                warn=s.nested))

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
        fields = [
            {"key": "image", "label": "image", "type": "choice",
             "value": config.DEFAULT_IMAGE,
             "options": [(f"{'x ' if e.broken else ('? ' if not e.verified else '  ')}"
                          f"{k:<14} {e.description[:30]}", k) for k, e in entries],
             "help": OPTION_HELP["image"]},
            {"key": "cpus", "label": "cpus", "type": "text",
             "value": config.DEFAULT_CPUS, "help": OPTION_HELP["cpus"]},
            {"key": "memory", "label": "memory", "type": "text",
             "value": config.DEFAULT_MEMORY, "help": OPTION_HELP["memory"]},
            {"key": "disk", "label": "disk", "type": "text",
             "value": config.DEFAULT_DISK, "help": OPTION_HELP["disk"]},
            {"key": "internet", "label": "internet", "type": "bool",
             "value": False, "help": OPTION_HELP["internet"]},
            {"key": "lan", "label": "lan", "type": "bool", "value": False,
             "help": OPTION_HELP["lan"], "risky": True},
            {"key": "sudo", "label": "sudo", "type": "choice",
             "value": config.AGENT_SUDO,
             "options": [("nopasswd — agent can become root", "nopasswd"),
                         ("password — secret kept on the host", "password"),
                         ("none — agent cannot escalate", "none")],
             "help": OPTION_HELP["sudo"]},
            {"key": "nested", "label": "nested virt", "type": "bool",
             "value": False, "help": OPTION_HELP["nested"], "risky": True},
            {"key": "start", "label": "start now", "type": "bool",
             "value": True, "help": OPTION_HELP["start"]},
        ]
        got = ui.form(self.stdscr, f"New box: {name}", fields,
                      note="Every option is shown with its default. "
                           "Enter changes the highlighted one, C creates.")
        if got is None:
            return

        try:
            spec = BoxSpec(
                name=name, image=str(got["image"]), cpus=int(got["cpus"]),
                memory=str(got["memory"]), disk=str(got["disk"]),
                sudo=str(got["sudo"]), nested=bool(got["nested"]),
                internet=bool(got["internet"]), lan=bool(got["lan"]),
            )
        except (ValueError, KeyError) as exc:
            ui.error(self.stdscr, f"bad value: {exc}")
            return

        res = self.task(
            "Creating box",
            lambda: boxlib.create(spec, start=bool(got["start"])),
            f"Building {name} from {got['image']}. First boot takes a minute.")
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
        if not box:
            return
        row = self.rows[self.rsel] if self.rsel < len(self.rows) else None
        if row is None or row.kind != "snapshot":
            # Reached from the menu rather than by putting the cursor on a
            # snapshot row, so ask which one. Returning silently here is how
            # this entry would look like a dead keypress.
            snaps = boxlib.list_snapshots(box.name)
            if not snaps:
                ui.message(self.stdscr, "Roll back",
                           f"{box.name} has no snapshots yet.\n\n"
                           "Take one with F2 while the box is stopped.")
                return
            index = ui.choose(
                self.stdscr, f"Roll back {box.name} to...",
                [ui.Choice(label=f"{s.index}  {s.label}", value=s.index,
                           hint=s.created[:16]) for s in snaps])
            if index is None:
                return
            row = Row("snapshot", "", index)
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

    def act_disk(self) -> None:
        box = self.current
        if not box:
            return
        size = ui.prompt(
            self.stdscr, f"Grow disk: {box.name}",
            f"New size (now {box.spec.disk}):", f"+10G")
        if size is None or not size.strip():
            return

        note = ("Growing the virtual disk, then the partition and filesystem "
                "inside it.")
        if box.state != "running":
            note = ("Box is stopped, so only the virtual disk grows. Start it "
                    "and repeat to expand the filesystem.")
        res = self.task("Growing disk",
                        lambda: boxlib.resize_disk(box.name, size.strip()), note)
        if res is None:
            return
        if res["was"] == res["now"]:
            self.status = f"{box.name} disk is already {res['now']}"
        elif res["filesystem"]:
            self.status = f"{box.name} disk {res['was']} → {res['now']}"
        else:
            self.status = (f"{box.name} disk {res['was']} → {res['now']} — start "
                           "it and repeat to grow the filesystem")
        self.refresh_boxes()

    # -- local networks ---------------------------------------------------

    def act_netls(self) -> None:
        found = netlib.list_nets()
        if not found:
            ui.message(self.stdscr, "Local networks",
                       "No local networks yet.\n\n"
                       "A local network is a members-only segment: boxes on the "
                       "same one reach each other, and nothing else — no "
                       "gateway, no host, no internet.\n\n"
                       "Create one from this menu, then attach boxes to it.")
            return
        lines = []
        for net in found:
            members = boxlib.boxes_on_net(net.name)
            router = boxlib.router_on_net(net.name)
            lines.append(f"{net.name:<12} {net.subnet:<20} {net.bridge}")
            for box in members:
                role = "  ROUTER (source pin dropped)" if box == router else ""
                lines.append(f"{'':<12}   {box} — {net.address(box)}{role}")
            if not members:
                lines.append(f"{'':<12}   (no boxes attached)")
            lines.append("")
        lines.append(f"definitions: {netlib.NETS_FILE}")
        ui.pager(self.stdscr, "Local networks", "\n".join(lines))

    def act_netcreate(self) -> None:
        name = ui.prompt(self.stdscr, "Create a local network",
                         f"Name (max {netlib.NAME_MAX} chars):")
        if not name or not name.strip():
            return
        res = self.task(
            "Creating network", lambda: netlib.create(name.strip()),
            "Defining an isolated segment with no gateway and no host address.")
        if res is None:
            return
        self.status = f"Created {res.name} — {res.subnet}"
        ui.message(self.stdscr, f"Created {res.name}",
                   f"subnet {res.subnet}\nbridge {res.bridge}\n\n"
                   "Members-only: no gateway, no host address, no internet.\n\n"
                   "Attach a box to it from this menu (F6).")

    def act_netrm(self) -> None:
        found = netlib.list_nets()
        if not found:
            ui.message(self.stdscr, "Delete a network", "None defined.")
            return
        name = ui.choose(
            self.stdscr, "Delete which network?",
            [ui.Choice(label=f"{n.name:<12} {n.subnet}", value=n.name,
                       hint=f"{len(boxlib.boxes_on_net(n.name))} box(es)")
             for n in found])
        if name is None:
            return
        attached = boxlib.boxes_on_net(name)
        if attached:
            ui.error(self.stdscr,
                     f"{name} still has {', '.join(attached)} attached. "
                     "Detach them first.")
            return
        if not ui.confirm(self.stdscr, "Delete network",
                          f"Delete the local network {name}?", danger=True):
            return
        if self.task("Deleting", lambda: netlib.remove(name, [])) is not None:
            self.status = f"Deleted network {name}"

    def act_netattach(self) -> None:
        box = self.current
        if not box:
            return
        available = [n for n in netlib.list_nets() if n.name not in box.spec.nets]
        if not available:
            ui.message(
                self.stdscr, "Attach to a network",
                "Nothing to attach to.\n\n"
                + ("This box is already on every local network."
                   if netlib.list_nets() else
                   "No local networks exist yet — create one first."))
            return
        name = ui.choose(
            self.stdscr, f"Attach {box.name} to...",
            [ui.Choice(label=f"{n.name:<12} {n.subnet}", value=n.name,
                       hint=n.address(box.name)) for n in available])
        if name is None:
            return
        net = netlib.get(name)

        got = ui.form(self.stdscr, f"Attach {box.name} to {name}", [
            {"key": "router", "label": "router", "type": "bool", "value": False,
             "help": "This box FORWARDS for the others on the net — the "
                     "firewall role. Its own source-address pin on this net is "
                     "dropped, because forwarding means sending packets that "
                     "are not yours. Every other member stays pinned, and MAC "
                     "and ARP anti-spoofing stay on even here.",
             "risky": True},
        ], note=f"{box.name} gets {net.address(box.name)} on {name}. The box "
                "restarts so it comes up with the interface configured.",
             action="attach")
        if got is None:
            return
        router = bool(got["router"])
        existing = boxlib.router_on_net(name)
        if router and existing and existing != box.name:
            if not ui.confirm(self.stdscr, "Second router",
                              f"{existing} already routes for {name}. Only one "
                              "supplies the default route. Continue?"):
                return
        note = (f"{box.name} will get {net.address(box.name)} on {name}."
                + (" Forwarding and masquerade are configured for it."
                   if router else ""))
        res = self.task("Attaching",
                        lambda: boxlib.attach_net(box.name, name,
                                                  router=router), note)
        self.refresh_boxes()
        if res is not None:
            self.status = (f"{box.name} on {name} at {net.address(box.name)}"
                           + (" — routing for it" if router else ""))

    def act_netdetach(self) -> None:
        box = self.current
        if not box:
            return
        if not box.spec.nets:
            ui.message(self.stdscr, "Detach from a network",
                       f"{box.name} is not on any local network.")
            return
        name = ui.choose(self.stdscr, f"Detach {box.name} from...",
                         [ui.Choice(label=n, value=n) for n in box.spec.nets])
        if name is None:
            return
        res = self.task("Detaching",
                        lambda: boxlib.detach_net(box.name, name),
                        "The box restarts without the interface.")
        self.refresh_boxes()
        if res is not None:
            self.status = f"{box.name} detached from {name}"

    def act_rmimage(self) -> None:
        """Delete an image: golden base, cached download and catalogue entry.

        Every option `vmorch rmimage` takes is here as a form field, because the
        rule is that nothing is CLI-only -- and the choices genuinely matter.
        Keeping the cache is the difference between rebuilding this image
        offline and needing the network again.
        """
        entries = sorted(images.catalogue(include_hidden=True).items())
        if not entries:
            ui.message(self.stdscr, "Remove image", "The catalogue is empty.")
            return

        marks = []
        for key, entry in entries:
            state = []
            if images.base_path(entry).exists():
                state.append("base")
            if entry.url and entry.cached.exists():
                state.append("cached")
            marks.append((f"{key:<16} {', '.join(state) or 'nothing on disk'}",
                          key))
        key = ui.choose(self.stdscr, "Remove which image?", marks)
        if key is None:
            return

        got = ui.form(self.stdscr, f"Remove image: {key}", [
            {"key": "keep_cache", "label": "keep download", "type": "bool",
             "value": False,
             "help": "Keep the verified original in the download cache. The "
                     "golden image can then be rebuilt without the network. "
                     "Turn off to reclaim the cache space too."},
            {"key": "keep_entry", "label": "keep entry", "type": "bool",
             "value": False,
             "help": "Leave the block in images.toml, so the image still "
                     "appears in the catalogue and can be downloaded again. "
                     "Turn off to forget it entirely."},
            {"key": "force", "label": "force", "type": "bool", "value": False,
             "help": "Remove even while boxes are built on this image. Their "
                     "base file is kept regardless — only the catalogue entry "
                     "and cache go — so the boxes keep working.",
             "risky": True},
        ], note="Nothing is deleted until you have seen the file list and "
                "confirmed it.", action="remove")
        if got is None:
            return

        try:
            plan = images.plan_removal(key, keep_cache=bool(got["keep_cache"]),
                                       keep_entry=bool(got["keep_entry"]))
        except images.ImageError as exc:
            ui.error(self.stdscr, str(exc))
            return

        # The warning text is built by the CLI from the same plan object that
        # does the deleting, so what is shown here cannot drift from what goes.
        from .. import cli as _cli
        ui.pager(self.stdscr, f"Remove image: {key}",
                 "\n".join(_cli.describe_removal(plan)))
        if plan.empty:
            return
        if not ui.confirm(self.stdscr, "Remove image",
                          f"Delete {images.human_size(plan.freed)} for {key}? "
                          "This cannot be undone.", danger=True):
            self.status = "cancelled"
            return

        done = self.task("Removing image",
                         lambda: images.remove(plan, force=bool(got["force"])),
                         f"Deleting {key}.")
        if done is None:
            return
        self.status = (f"Removed {key} — freed {images.human_size(done.freed)}"
                       if done.files else f"Removed {key} from the catalogue")

    def act_nested(self) -> None:
        box = self.current
        if not box:
            return
        want = not box.spec.nested
        if want and not ui.confirm(
            self.stdscr, "Enable nested virtualisation",
            f"Let {box.name} run its own virtual machines?\n\n"
            + OPTION_HELP["nested"],
            danger=True,
        ):
            return
        def apply_it():
            b = boxlib.load(box.name)
            b.spec.nested = want
            boxlib.save_spec(b.spec)
            return boxlib.apply(box.name)
        self.task("Applying", apply_it,
                  "Regenerating the domain. A running box restarts.")
        self.status = (f"{box.name}: nested virtualisation "
                       f"{'enabled' if want else 'disabled'}")
        self.refresh_boxes()

    def act_password(self) -> None:
        box = self.current
        if not box:
            return
        if box.spec.sudo != "password":
            ui.message(self.stdscr, "No password",
                       f"{box.name} is in sudo mode '{box.spec.sudo}', so no "
                       "password is set.\n\nOnly 'password' mode uses one — it "
                       "is generated on the host and never stored in readable "
                       "form inside the box, which is what stops an agent "
                       "process from using it.")
            return
        from .. import cloudinit
        path = cloudinit.password_path(box.name)
        if not path.exists():
            ui.message(self.stdscr, "Not generated yet",
                       f"Reseed {box.name} to generate its password.")
            return
        ui.message(self.stdscr, f"sudo password — {box.name}",
                   f"{path.read_text().strip()}\n\nStored on the host at "
                   f"{path}, mode 0600. The box holds only its hash.")

    def act_sudo(self) -> None:
        box = self.current
        if not box:
            return
        mode = ui.choose(
            self.stdscr, f"sudo for {box.name}", [
                ("nopasswd — agent can become root at will", "nopasswd"),
                ("password — secret kept on the host, not in the box", "password"),
                ("none — agent cannot escalate at all", "none"),
            ],
            note=f"currently: {box.spec.sudo}. The agent owning its box is the "
                 "premise, so this is defence in depth, not the boundary — the "
                 "boundary is the VM.",
        )
        if mode is None or mode == box.spec.sudo:
            return
        if mode != "nopasswd" and not ui.confirm(
            self.stdscr, "Reduce agent privilege",
            f"Set {box.name} to sudo={mode}?\n\n"
            "The agent will no longer be able to install packages or change "
            "system config itself — bake what it needs into a golden image.\n\n"
            "vmorch keeps its own root access, so shares, services and imaging "
            "still work.",
        ):
            return

        self.task("Saving", lambda: boxlib.set_sudo(box.name, mode))
        self.refresh_boxes()
        if ui.confirm(self.stdscr, "Apply now",
                      "cloud-init only runs at first boot, so this takes effect "
                      f"after a reseed.\n\nReseed {box.name} now? It restarts "
                      "the box."):
            self.task("Reseeding", lambda: boxlib.reseed(box.name),
                      "Rebuilding the seed and restarting.")
            self.status = f"{box.name} reseeded with sudo={mode}"
        else:
            self.status = (f"{box.name} set to sudo={mode} — takes effect after "
                           f"`vmorch reseed {box.name}`")
        self.refresh_boxes()

    def act_reseed(self) -> None:
        box = self.current
        if not box:
            return
        if not ui.confirm(
            self.stdscr, "Re-run first-boot config",
            f"Reseed {box.name}?\n\n"
            "This restarts the box and makes cloud-init run again: it "
            "regenerates ssh host keys, re-applies the agent user and your key, "
            "and redoes mounts. Use it when a box pings but refuses ssh.\n\n"
            "Installed software and your data are untouched. Files cloud-init "
            "owns (netplan config, /etc/hosts) are rewritten.",
        ):
            return
        res = self.task("Reseeding", lambda: boxlib.reseed(box.name),
                        "Rebuilding the seed and restarting the box.")
        if res is not None:
            self.status = (f"{box.name} reseeded — cloud-init re-runs on boot; "
                           "give it a minute")
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
            self.do_apply(box.name)
        self.refresh_boxes()

    def do_apply(self, name: str) -> None:
        """Reconcile a box with its spec and report anything left undone.

        Shared by F4 and the menu's Apply, because this is the path where the
        internet flag gets changed and `apply` can half-succeed -- the domain
        right, the guest not told about its new NIC. Reporting only "Applied"
        over that is the silent success the reconcile exists to remove.
        """
        res = self.task(
            "Applying", lambda: boxlib.apply(name),
            "Reconciling the domain, the disk and the guest's network with "
            "the spec.")
        self.refresh_boxes()
        if res is None:
            return
        self.status = f"Applied {name}"
        if res.note:
            ui.message(self.stdscr, f"Applied {name}", res.note)
            self.status = f"Applied {name} — {res.note}"

    def _menu_choices(self, name: str) -> list[ui.Choice]:
        """One menu's entries, with box-scoped ones disabled if there is none."""
        box = self.current
        out = []
        for e in MENUS[name][1]:
            out.append(ui.Choice(
                label=e.label, value=e.value, key=e.key, hint=e.hint,
                header=e.header,
                disabled="" if (box or not e.box) else "No box selected",
            ))
        return out

    def act_menu(self, name: str = "main") -> None:
        """Open a menu, following submenus until something is chosen.

        A loop rather than recursion so Esc in a submenu comes back to the menu
        above it instead of closing the whole thing -- backing out of a wrong
        turn should cost one keystroke, not five.
        """
        stack = [name]
        while stack:
            current = stack[-1]
            title, _ = MENUS[current]
            box = self.current
            if current != "main" and box and any(
                    e.box for e in MENUS[current][1] if e.value):
                title = f"{title} — {box.name}"

            choice = ui.choose(self.stdscr, title, self._menu_choices(current))
            if choice is None:
                stack.pop()                    # Esc: back one level
                continue
            if isinstance(choice, str) and choice.startswith("menu:"):
                stack.append(choice.split(":", 1)[1])
                continue
            self._run_menu_action(choice)
            return

    def _run_menu_action(self, choice: str) -> None:
        box = self.current
        actions = {
            "new": self.act_new, "toggle": self.act_toggle, "ssh": self.act_ssh,
            "share": self.act_share, "service": self.act_service,
            "snap": self.act_snapshot, "view": self.act_view,
            "edit": self.act_edit, "del": self.act_delete, "help": self.act_help,
            "reseed": self.act_reseed, "sudo": self.act_sudo,
            "password": self.act_password, "nested": self.act_nested,
            "disk": self.act_disk, "rmimage": self.act_rmimage,
            "rollback": self.act_rollback,
            "netls": self.act_netls, "netcreate": self.act_netcreate,
            "netrm": self.act_netrm, "netattach": self.act_netattach,
            "netdetach": self.act_netdetach,
        }
        if choice in actions:
            actions[choice]()
        elif choice == "apply" and box:
            self.do_apply(box.name)
        elif choice == "auditbox" and box:
            import io, contextlib
            from .. import cli as _cli
            buf = io.StringIO()
            args = type("A", (), {"since": "-24h", "box": box.name,
                                  "blocked": False, "install": False,
                                  "enable_dns": False})()
            with contextlib.redirect_stdout(buf):
                _cli.cmd_audit(args)
            ui.pager(self.stdscr, f"Audit — {box.name}, last 24h", buf.getvalue())
        elif choice == "images":
            lines = []
            for k, e in sorted(images.catalogue().items()):
                marks = []
                base = images.base_path(e)
                if base.exists():
                    marks.append(
                        f"base ready {images.human_size(base.stat().st_size)}")
                if e.url and e.cached.exists():
                    marks.append(
                        f"cached {images.human_size(e.cached.stat().st_size)}")
                lines.append(f"{k:<14} {e.description}\n"
                             f"{'':<14} {', '.join(marks) or 'nothing on disk'}")
            lines.append("")
            lines.append(f"catalogue: {images.USER_CATALOGUE}")
            lines.append("F9 → Images → Remove an image  deletes the files "
                         "and the catalogue entry")
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
                       "  vmorch golden agent-base --packages tmux,git")
        elif choice == "audit":
            import io, contextlib
            from .. import cli as _cli
            buf = io.StringIO()
            args = type("A", (), {"since": "-24h", "box": None, "blocked": False,
                                  "install": False, "enable_dns": False})()
            with contextlib.redirect_stdout(buf):
                _cli.cmd_audit(args)
            ui.pager(self.stdscr, "Audit — last 24h", buf.getvalue())
        elif choice == "config":
            import io
            import contextlib
            from .. import cli as _cli

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _cli.cmd_config(type("A", (), {"write": False})())
            ui.pager(self.stdscr, "Configuration", buf.getvalue())
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
            # Re-applied every pass: run_task flips nodelay while it spins a
            # worker, which clears the timeout on the way out.
            self.stdscr.timeout(POLL_MS)
            k = self.stdscr.getch()

            if k == -1:
                # getch timed out: poll for state changes we did not cause.
                if self.refresh_states():
                    self.rebuild_rows()
                continue
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
                if self.current:
                    self.do_apply(self.current.name)
            elif k == curses.KEY_F3:
                self.act_view()
            elif k == curses.KEY_F4:
                self.act_edit()
            elif k == curses.KEY_F5:
                self.act_share()
            elif k == curses.KEY_F6:
                self.act_menu("networks")
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
  r                reload now (the list also refreshes itself every 1.5s,
                   so boxes started elsewhere show up on their own)

BOXES PANEL (left)
  Enter            ssh into the box, starting it first if needed
  Space            start or stop the box
  F7               create a new box
  F8               destroy the box and its disk

DETAILS PANEL (right)
  F8               revoke whatever the cursor is on: a shared folder,
                   a granted service
  Enter            on a snapshot row, roll back to it

PRIVILEGE
  The right panel shows each box's sudo mode. "passwordless" is
  highlighted because it means an unprivileged compromise inside the box
  is one step from root. F9 -> p -> s changes it; it takes effect after a
  reseed, which the dialog offers to do.

  vmorch keeps its own root access over a separate key, so reducing the
  agent's privileges never breaks shares, services or imaging.

RECOVERY
  A box that pings but refuses ssh has usually lost its ssh host keys:
  the socket accepts the connection, sshd cannot start, the client sees a
  refusal. F9 -> t -> r re-runs the box's first-boot configuration and
  regenerates them. Installed software and data are untouched.

  F9 -> f -> m re-mounts shared folders, which fixes a share that was
  configured while the box was stopped and never got mounted.

  Granting internet to an existing box needs "Apply spec" AND a reseed:
  apply adds the NIC, but the guest's network config was written at first
  boot and does not know about it.

ACTIONS
  F2               take a snapshot (the box must be stopped)
  F3               view the console log
  F4               edit the box spec in $EDITOR, then apply
  F5               share a host folder
  F6               grant access to a host service
  F9               menu — everything else
  F10 / q          quit

THE MENU (F9)
  Grouped by what each action changes, so you can find something you
  have not used before without reading the whole list. Every entry has
  a letter: F9 then that letter runs it, no scrolling.

    This box            ssh, start/stop, edit the spec, apply it
    Disk and snapshots  d   grow the disk, snapshot, roll back
    Folders and services f  share a folder, re-mount, grant a service
    Privileges/hardware p   sudo rights, sudo password, nested virt
    Troubleshoot        t   console log, reseed, this box's audit trail
    Images              i   catalogue, remove an image, golden images
    Host and settings   h   config and disk usage, full audit, network

  Esc backs out one level rather than closing the menu.

  Entries that act on a box go dim when no box is selected, instead of
  being pickable and doing nothing.

WORTH KNOWING

  Shared folders are read-only by default, and read-only holds even
  against root inside the box. Remounting the share writable in the guest
  still cannot write to it. A writable share is a real grant, so it is
  confirmed separately and shown in yellow.

  Snapshots do NOT cover shared folders. A shared folder is the live host
  filesystem, not part of the box's disk, so rolling back does not undo
  anything written into a writable share.

  Network grants and service grants are independent. A box with no
  internet at all can still reach a host service you shared with it.

  A box keeps its address for as long as it exists, so `ssh <name>` and
  the entry in your ssh config stay correct across rebuilds.
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
