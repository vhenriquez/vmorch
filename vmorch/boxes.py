"""Box lifecycle: create, start, stop, apply, destroy.

A box outlives its first session. Its spec is a durable artifact on disk, the
domain XML is regenerated from it, and `apply` reconciles a box that already
exists. That is what separates this from a one-shot `virt-install` command line.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import (alloc, cloudinit, config, domain, guest, hostaccess, images,
               network, services, snapshots, spec as spec_mod, sshconf,
               virsh)
from .spec import BoxSpec


class BoxError(RuntimeError):
    pass


@dataclass
class Box:
    spec: BoxSpec
    state: str
    ip: str
    cid: int

    @property
    def name(self) -> str:
        return self.spec.name


def box_dir(name: str) -> Path:
    return config.BOXES_DIR / name


def spec_path(name: str) -> Path:
    return box_dir(name) / "box.toml"


def disk_path(name: str) -> Path:
    return box_dir(name) / f"{name}.qcow2"


def exists(name: str) -> bool:
    return spec_path(name).exists()


def list_names() -> list[str]:
    if not config.BOXES_DIR.exists():
        return []
    return sorted(
        d.name for d in config.BOXES_DIR.iterdir() if (d / "box.toml").exists()
    )


def load(name: str) -> Box:
    if not exists(name):
        raise BoxError(f"no such box: {name}")
    box_spec = spec_mod.load(spec_path(name))
    allocation = alloc.allocate(name)
    return Box(
        spec=box_spec,
        state=virsh.domain_state(box_spec.domain),
        ip=allocation.ip,
        cid=allocation.cid,
    )


def list_boxes() -> list[Box]:
    """Stopped boxes list alongside running ones -- "which boxes do I have" is
    the question being asked, and a stopped box costs only its overlay."""
    return [load(n) for n in list_names()]


def console_log(name: str) -> Path:
    return box_dir(name) / "console.log"


#: Trim the console log past this, keeping the tail. The domain XML uses
#: append='on' so history survives restarts, which would otherwise grow without
#: bound on a box that lives for months.
CONSOLE_MAX_BYTES = 4 * 1024 * 1024
CONSOLE_KEEP_BYTES = 512 * 1024


def _ensure_console_log(name: str) -> Path:
    """Pre-create the console log, owned by us, before libvirt opens it.

    This is load-bearing. The domain uses <console type='pty'> with a <log>
    element, and libvirt *appends* to that file rather than creating it -- so a
    file we make first keeps our ownership and stays readable.

    A file libvirt creates itself instead comes out root:root 0600, which is
    how this went wrong before: the owner of a box could not read its own boot
    output. Recreating the file here also repairs a box that still carries a
    stale root-owned log from that era.
    """
    log = console_log(name)
    log.parent.mkdir(parents=True, exist_ok=True)

    if log.exists() and not os.access(log, os.R_OK):
        # A root-owned leftover. We cannot chown it, but we own the directory,
        # so replacing it is both possible and the only way to fix the box.
        try:
            log.unlink()
        except OSError:
            return log

    if not log.exists():
        log.touch(mode=0o644)
    elif log.stat().st_size > CONSOLE_MAX_BYTES:
        tail = log.read_bytes()[-CONSOLE_KEEP_BYTES:]
        log.write_bytes(b"[... earlier console output trimmed ...]\n" + tail)

    return log


#: Disk layers need the group bits set. With an ACL present those bits ARE the
#: mask, and a r-- mask silently caps libvirt-qemu's rwx entry at read-only --
#: qemu then cannot write its own disk and the box fails to start with a bare
#: "Permission denied".
DISK_MODE = 0o660


def _ensure_disk_perms(name: str) -> None:
    """Repair permissions on a box's whole backing chain before starting it.

    Done on every start, not only at creation, because boxes made by an earlier
    version carry a 0644 disk whose ACL mask blocks qemu. Fixing it here means
    an existing box heals itself rather than needing to be rebuilt.
    """
    targets = [disk_path(name), *(box_dir(name) / "snapshots").glob("*.qcow2")]
    for path in targets:
        try:
            if path.exists() and path.owner() == getpass.getuser():
                if path.stat().st_mode & 0o070 != 0o060:
                    path.chmod(DISK_MODE)
        except (OSError, KeyError):
            # Not ours to fix; let libvirt report the real error on start.
            continue


def _graceful_restart_stop(name: str, timeout: int = 90) -> None:
    """Shut a box down properly before rebuilding or reseeding it.

    `virsh destroy` is a power cut. Using it to restart a box for a routine
    reconfiguration risks whatever was mid-write -- and almost certainly did:
    a box lost its ssh host keys this way, which makes ssh.service fail to
    start, so the socket accepts a connection and immediately drops it. The
    symptom is "Connection refused" on a box that pings fine, with nothing in
    the console log to explain it.

    Falls back to the power cut only if the guest ignores the request.
    """
    domain = config.DOMAIN_PREFIX + name
    if virsh.domain_state(domain) != "running":
        return
    virsh.run("shutdown", domain, check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if virsh.domain_state(domain) != "running":
            return
        time.sleep(2)
    virsh.run("destroy", domain, check=False)


def _bytes_of(size: str) -> int:
    text = size.strip().upper()
    units = {"G": 1024 ** 3, "M": 1024 ** 2, "K": 1024, "T": 1024 ** 4}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text)) * 1024 ** 3


def _check_disk_fits(box_spec: BoxSpec, base: Path) -> None:
    """Refuse a disk smaller than the image's own partition layout.

    An image's partition table is baked in. Kali's cloud image declares a root
    partition ending around 25G; give the box a 20G disk and root runs off the
    end of the device, so the initramfs cannot mount it and the box drops into
    emergency mode with no ssh and no obvious cause. Caught here, it is one
    clear sentence instead of an afternoon.
    """
    info = json.loads(subprocess.run(
        ["qemu-img", "info", "--output=json", str(base)],
        capture_output=True, text=True, check=True,
    ).stdout)
    needed = int(info.get("virtual-size", 0))
    asked = _bytes_of(box_spec.disk)
    if asked < needed:
        raise BoxError(
            f"image {box_spec.image!r} needs at least "
            f"{needed / 1024**3:.0f}G but --disk is {box_spec.disk}. "
            "The image's partition table is larger than the disk, so it would "
            f"boot into emergency mode. Use --disk {needed / 1024**3:.0f}G or more."
        )


def _create_overlay(box_spec: BoxSpec, base: Path) -> Path:
    """Thin copy-on-write overlay over the golden base.

    Near-instant and tiny: the base stays pristine and shared by every box.
    """
    disk = disk_path(box_spec.name)
    disk.parent.mkdir(parents=True, exist_ok=True)
    if disk.exists():
        return disk

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2",
         "-F", "qcow2", "-b", str(base),
         str(disk), box_spec.disk],
        check=True, capture_output=True,
    )
    disk.chmod(DISK_MODE)   # see DISK_MODE: the ACL mask depends on it
    return disk


def create(box_spec: BoxSpec, start: bool = True) -> Box:
    if exists(box_spec.name):
        raise BoxError(
            f"box {box_spec.name!r} already exists -- use `vm apply` to change it"
        )
    if virsh.domain_exists(box_spec.domain):
        raise BoxError(f"libvirt already has a domain named {box_spec.domain!r}")

    network.ensure_base()
    hostaccess.ensure()

    entry = images.get(box_spec.image)
    base = images.ensure_base(entry)

    # Validate before anything is written. A check that runs after the spec
    # file exists leaves a half-made box that blocks the corrected retry.
    _check_disk_fits(box_spec, base)

    allocation = alloc.allocate(box_spec.name)
    network.reserve_address(box_spec.name, allocation.mac, allocation.ip)

    box_dir(box_spec.name).mkdir(parents=True, exist_ok=True)
    spec_path(box_spec.name).write_text(spec_mod.dump(box_spec))

    services.ensure_box_filter(box_spec)

    disk = _create_overlay(box_spec, base)
    seed = cloudinit.build_seed(box_spec, box_dir(box_spec.name),
                                allocation.mac, allocation.wan_mac)
    _ensure_console_log(box_spec.name)

    xml = domain.build(
        box_spec,
        disk_path=str(disk),
        mac=allocation.mac,
        wan_mac=allocation.wan_mac,
        cid=allocation.cid,
        console_log=str(box_dir(box_spec.name) / "console.log"),
        seed_iso=str(seed),
    )
    (box_dir(box_spec.name) / "domain.xml").write_text(xml)
    virsh.define_domain(xml)

    _regenerate_ssh()

    if start:
        virsh.run("start", box_spec.domain)

    return load(box_spec.name)


def apply(name: str) -> Box:
    """Regenerate the domain from the spec and reconcile.

    Deliberately does not touch cloud-init: that ran once, at first boot. Later
    changes go through domain XML and in-guest commands.
    """
    box = load(name)
    allocation = alloc.allocate(name)
    services.ensure_box_filter(box.spec)

    xml = domain.build(
        box.spec,
        disk_path=str(disk_path(name)),
        mac=allocation.mac,
        wan_mac=allocation.wan_mac,
        cid=allocation.cid,
        console_log=str(box_dir(name) / "console.log"),
        seed_iso=str(box_dir(name) / "seed.iso"),
        uuid=virsh.domain_uuid(box.spec.domain),
    )
    (box_dir(name) / "domain.xml").write_text(xml)

    was_running = box.state == "running"
    if was_running:
        _graceful_restart_stop(name)

    virsh.define_domain(xml)
    _regenerate_ssh()

    if was_running:
        _ensure_console_log(name)
        _ensure_disk_perms(name)
        virsh.run("start", box.spec.domain)

    return load(name)


def start(name: str) -> None:
    box = load(name)
    _ensure_console_log(name)
    _ensure_disk_perms(name)
    if box.state != "running":
        virsh.run("start", box.spec.domain)


def stop(name: str, force: bool = False) -> None:
    box = load(name)
    if box.state == "running":
        virsh.run("destroy" if force else "shutdown", box.spec.domain)


def destroy(name: str, keep_disk: bool = False) -> None:
    """Remove a box, and everything the host was holding for it."""
    box = load(name)
    allocation = alloc.allocate(name)

    if virsh.domain_exists(box.spec.domain):
        if virsh.domain_state(box.spec.domain) == "running":
            virsh.run("destroy", box.spec.domain)
        virsh.run("undefine", box.spec.domain, "--nvram")

    sshconf.forget_host(box.ip)

    if not keep_disk:
        shutil.rmtree(box_dir(name), ignore_errors=True)

    services.delete_box_filter(name)
    network.unreserve_address(name, allocation.mac, box.ip)
    alloc.release(name)
    _regenerate_ssh()


def _wait_reachable(name: str, timeout: int = 300) -> None:
    """Block until the box answers SSH, so reconfiguration does not race boot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if guest.reachable(name):
            return
        time.sleep(3)
    raise BoxError(f"{name} did not become reachable over ssh within {timeout}s")


def save_spec(box_spec: BoxSpec) -> None:
    spec_path(box_spec.name).write_text(spec_mod.dump(box_spec))


def share(name: str, host_path: Path, tag: str | None = None,
          mode: str = "ro") -> Box:
    """Grant a box access to a host folder, then apply.

    Read-only unless the caller explicitly asks otherwise -- the CLI makes rw
    opt-in, and spec parsing independently defaults to ro, so a slip in either
    layer still fails closed.
    """
    box = load(name)
    resolved = host_path.expanduser().resolve()
    if not resolved.is_dir():
        raise BoxError(f"not a directory: {resolved}")

    tag = tag or resolved.name
    if any(f.tag == tag for f in box.spec.folders):
        raise BoxError(f"box {name!r} already shares tag {tag!r}")

    entry = {"host": str(resolved), "tag": tag, "mode": mode}
    folder = spec_mod._parse_folder(entry, len(box.spec.folders) + 1)
    box.spec.folders.append(folder)
    save_spec(box.spec)

    running = box.state == "running"

    if running:
        # Hot-attach. Rebuilding the domain would reboot the box, and a box
        # that is busy doing work should not be restarted just to gain a
        # folder. Every box carries the shared-memory backing from creation so
        # that this works live.
        try:
            virsh.attach_device(box.spec.domain,
                                domain.filesystem_device_xml(folder),
                                live=True, persist=True)
        except virsh.VirshError as exc:
            # Undo the spec change. A grant recorded but not attached would
            # show in `vm show` while doing nothing -- the same silent lie the
            # unimplemented service mechanisms used to tell.
            box.spec.folders = [f for f in box.spec.folders if f.tag != tag]
            save_spec(box.spec)
            if "PCI slot" in exc.stderr:
                raise BoxError(
                    f"{name} has no free PCI slots for another share. Boxes "
                    "created before this was fixed have no spares; restart it "
                    f"(`vm stop {name} && vm apply {name} && vm start {name}`) "
                    "to pick up the extra ports."
                ) from None
            raise
        try:
            guest.mount_folder(name, folder)
        except guest.GuestError as exc:
            raise BoxError(
                f"{name}: the share is attached and saved, but mounting it "
                f"inside the box failed ({exc}). Run `vm mount {name}` once the "
                "box is reachable."
            ) from None
    else:
        # Stopped: regenerate the domain so the device is there at next boot.
        apply(name)

    return load(name)


def reseed(name: str) -> Box:
    """Make an existing box run its first-boot configuration again.

    cloud-init only acts once per *instance*, identified by instance-id. Giving
    the seed a fresh one makes the box look new to cloud-init, so it regenerates
    ssh host keys, re-applies the agent user and its key, and redoes mounts.

    The repair path for a box you can no longer ssh into. It re-applies what the
    tool configured; it does not revert anything you changed inside the box, but
    files cloud-init owns (netplan config, /etc/hosts) are rewritten.
    """
    box = load(name)
    allocation = alloc.allocate(name)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    cloudinit.build_seed(box.spec, box_dir(name), allocation.mac,
                         allocation.wan_mac,
                         instance_id=f"{box.spec.domain}-{stamp}")

    # Reseeding regenerates the box's ssh host keys, so the entry we already
    # trust is about to become wrong. Left in place it produces "Host key
    # verification failed" and the repair command leaves the box unreachable --
    # the opposite of its job.
    sshconf.forget_host(box.ip)

    if box.state == "running":
        _graceful_restart_stop(name)
    _ensure_console_log(name)
    _ensure_disk_perms(name)
    virsh.run("start", box.spec.domain)
    return load(name)


def set_sudo(name: str, mode: str) -> Box:
    """Change what the agent user may do with sudo.

    Only written to the spec here. The rule lives in a sudoers file that
    cloud-init writes, and cloud-init runs once -- so this needs `vm reseed` to
    reach the box. Saying so is the caller's job.
    """
    box = load(name)
    box.spec.sudo = spec_mod._parse_sudo(mode)
    save_spec(box.spec)
    return load(name)


def sync_mounts(name: str) -> list[str]:
    """Mount every folder the spec grants. Idempotent; safe to re-run.

    The recovery path when a share was configured but not mounted -- a box that
    was stopped at the time, or an in-guest step that failed. cloud-init only
    mounts at first boot, so nothing else reconciles this.
    """
    box = load(name)
    if box.state != "running":
        raise BoxError(f"{name} is {box.state}; start it first")
    _wait_reachable(name)
    for folder in box.spec.folders:
        guest.mount_folder(name, folder)
    return [f.tag for f in box.spec.folders]


def _virtual_size(disk: Path) -> int:
    # -U because a running box holds a write lock on its own disk, and without
    # it qemu-img refuses to report anything at all -- which would make `vm disk`
    # work only on stopped boxes. Read-only inspection, so sharing is safe.
    info = json.loads(subprocess.run(
        ["qemu-img", "info", "-U", "--output=json", str(disk)],
        capture_output=True, text=True, check=True,
    ).stdout)
    return int(info["virtual-size"])


def _format_size(size_bytes: int) -> str:
    """Render bytes back as a spec-friendly string, preferring whole GB."""
    if size_bytes % 1024 ** 3 == 0:
        return f"{size_bytes // 1024 ** 3}G"
    return f"{size_bytes / 1024 ** 3:.1f}G"


def resize_disk(name: str, size: str) -> dict:
    """Grow a box's disk. Accepts an absolute size ("60G") or an increment ("+20G").

    Three things have to change together, and stopping halfway is what makes a
    resize look broken:

    1. the qcow2 active layer, so the virtual device is bigger;
    2. the guest's partition table and filesystem, or `df` is unchanged;
    3. the spec, or the next `vm apply` reverts the box to its old size.

    **Growing only.** qcow2 can be shrunk, but only by discarding the tail of
    the device -- which is where the filesystem's data lives. There is no safe
    automatic shrink, so it is refused rather than offered with a warning.

    A running box is resized live through `virsh blockresize`, which tells qemu
    to re-read the size, and the filesystem is grown online. Writing to the
    qcow2 underneath a running qemu instead would corrupt it, so the two paths
    are genuinely different rather than merely convenient.

    Only the active layer is touched. Snapshots below stay at their original
    size, which is fine: a qcow2 overlay may be larger than the layer it backs
    onto, and that is exactly what makes rolling back after a resize work.
    """
    box = load(name)
    disk = disk_path(name)
    current = _virtual_size(disk)

    text = size.strip()
    try:
        target = current + _bytes_of(text[1:]) if text.startswith("+") \
            else _bytes_of(text)
    except (ValueError, IndexError) as exc:
        raise BoxError(
            f"cannot read size {size!r}: use e.g. 60G, or +20G to add to the "
            "current size"
        ) from exc

    if target < current:
        raise BoxError(
            f"refusing to shrink {name} from {_format_size(current)} to "
            f"{_format_size(target)}. Shrinking a qcow2 discards the end of the "
            "device, where the filesystem keeps its data. Create a new box with "
            "a smaller --disk and copy what you need across."
        )

    domain = config.DOMAIN_PREFIX + name
    running = virsh.domain_state(domain) == "running"

    if target > current:
        if running:
            # Live: qemu owns the file. Writing to it directly would corrupt it.
            virsh.run("blockresize", domain, "vda", f"{target}B")
        else:
            subprocess.run(["qemu-img", "resize", str(disk), str(target)],
                           check=True, capture_output=True)
        box.spec.disk = _format_size(target)
        save_spec(box.spec)

    grown = ""
    if running and guest.reachable(name):
        # growpart and resize2fs are chatty; the script ends with `df -h /`, and
        # that last line is the only part anyone wants to read.
        out = [ln for ln in guest.grow_root(name).splitlines() if ln.strip()]
        grown = out[-1].strip() if out else ""

    return {
        "name": name,
        "was": _format_size(current),
        "now": _format_size(target),
        "running": running,
        "filesystem": grown,
    }


def snapshot(name: str, label: str | None = None):
    """Freeze the box's current disk state. Requires the box to be stopped."""
    box = load(name)
    if box.state == "running":
        raise BoxError(
            f"stop {name} first: `vm stop {name}`. Snapshotting a running box "
            "means a crash-consistent image at best."
        )
    return snapshots.create(box_dir(name), disk_path(name), label)


def rollback(name: str, index: int):
    box = load(name)
    if box.state == "running":
        raise BoxError(f"stop {name} first: `vm stop {name}`")
    # Pass the spec size so rewinding past a `vm disk` does not quietly undo it.
    return snapshots.rollback(box_dir(name), disk_path(name), index,
                              size=box.spec.disk)


def list_snapshots(name: str):
    return snapshots.load_all(box_dir(name))


def grant_service(name: str, svc_name: str, host_port: int, guest_port: int,
                  via: str = "filter") -> Box:
    """Let a box reach a host service. Every grant is a deliberate hole."""
    box = load(name)
    if any(s.name == svc_name for s in box.spec.from_host):
        raise BoxError(f"box {name!r} already has service {svc_name!r}")

    # The spec models three mechanisms but only one is built. Accepting the
    # others would record a grant in the spec, show it in `vm show`, and deliver
    # nothing -- a silent no-op is worse than a refusal.
    if via != "filter":
        raise BoxError(
            f"via={via!r} is designed but not implemented yet; use via=filter. "
            "See docs/agent-sandbox-use-case.md for what ssh and vsock would do."
        )

    entry = {"name": svc_name, "host": host_port, "guest": guest_port, "via": via}
    box.spec.from_host.append(
        spec_mod._parse_service(entry, len(box.spec.from_host) + 1,
                                spec_mod.VALID_VIA_FROM_HOST, "filter")
    )
    save_spec(box.spec)
    box = apply(name)

    if box.state == "running" and via == "filter":
        _wait_reachable(name)
        script = services.guest_relay_script(box.spec)
        if script:
            guest.run(name, script)
    return box


def revoke_service(name: str, svc_name: str) -> Box:
    box = load(name)
    remaining = [s for s in box.spec.from_host if s.name != svc_name]
    if len(remaining) == len(box.spec.from_host):
        raise BoxError(f"box {name!r} has no service {svc_name!r}")

    if virsh.domain_state(box.spec.domain) == "running" and guest.reachable(name):
        guest.run(name, f"systemctl disable --now vmorch-relay-{svc_name}.service "
                        f"2>/dev/null || true", check=False)

    box.spec.from_host = remaining
    save_spec(box.spec)
    return apply(name)


def unshare(name: str, tag: str) -> Box:
    box = load(name)
    remaining = [f for f in box.spec.folders if f.tag != tag]
    if len(remaining) == len(box.spec.folders):
        raise BoxError(f"box {name!r} does not share tag {tag!r}")
    gone = next(f for f in box.spec.folders if f.tag == tag)
    running = virsh.domain_state(box.spec.domain) == "running"

    if running and guest.reachable(name):
        guest.unmount_folder(name, tag)

    box.spec.folders = remaining
    save_spec(box.spec)

    if running:
        # Detach live, for the same reason share attaches live: revoking a
        # folder should not reboot a box that is in the middle of something.
        virsh.detach_device(box.spec.domain,
                            domain.filesystem_device_xml(gone),
                            live=True, persist=True)
        return load(name)
    return apply(name)


def _regenerate_ssh() -> None:
    entries = []
    for name in list_names():
        box_spec = spec_mod.load(spec_path(name))
        forwards = []
        for svc in box_spec.from_host:
            if svc.via == "ssh":
                forwards.append(
                    f"RemoteForward {svc.guest_port} 127.0.0.1:{svc.host_port}"
                )
        for svc in box_spec.to_host:
            if svc.via == "ssh":
                forwards.append(
                    f"LocalForward {svc.host_port} 127.0.0.1:{svc.guest_port}"
                )
        entries.append((name, box_spec.user, forwards))
    sshconf.regenerate(entries)
