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
    "disk":     "Maximum disk size. A ceiling, not an allocation -- a box uses only what it writes.",
    "internet": "Reach the PUBLIC internet. Does not include your local network.",
    "lan":      "Also reach the local network: router, NAS, other machines. Off by default.",
    "sudo":     "What the agent inside may escalate to. The boundary is the VM, so this is defence in depth.",
    "nested":   "Expose vmx/svm so the box can run its OWN virtual machines. Needed for the Android emulator and Genymotion; redroid does not need it. Real extra attack surface -- leave off unless required.",
    "start":    "Start the box immediately after creating it.",
}

#: Options where "on" widens what the box can reach or do. Shown in warning
#: colour so the risky setting is never the quiet one.
RISKY_WHEN_ON = {"lan", "nested"}

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
        add(Row("info", f"resources  {s.cpus} cpu · {s.memory} ram · {s.disk} disk"))
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
                           f"`vm reseed {box.name}`")
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
            ("Grow the disk", "disk"),
            ("View console log              F3", "view"),
            ("Edit box spec                 F4", "edit"),
            ("Image catalogue", "images"),
            ("Configuration and disk usage", "config"),
            ("Audit log: lookups and connections", "audit"),
            ("Re-mount shared folders", "mount"),
            ("Reseed: repair a box that refuses ssh", "reseed"),
            ("Set the agent's sudo rights", "sudo"),
            ("Toggle nested virtualisation", "nested"),
            ("Show the sudo password", "password"),
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
            "reseed": self.act_reseed, "sudo": self.act_sudo,
            "password": self.act_password, "nested": self.act_nested,
            "disk": self.act_disk,
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
  is one step from root. F9 -> "Set the agent's sudo rights" changes it;
  it takes effect after a reseed, which the dialog offers to do.

  vmorch keeps its own root access over a separate key, so reducing the
  agent's privileges never breaks shares, services or imaging.

RECOVERY
  A box that pings but refuses ssh has usually lost its ssh host keys:
  the socket accepts the connection, sshd cannot start, the client sees a
  refusal. F9 -> "Reseed" re-runs the box's first-boot configuration and
  regenerates them. Installed software and data are untouched.

  F9 -> "Re-mount shared folders" fixes a share that was configured while
  the box was stopped and never got mounted.

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
