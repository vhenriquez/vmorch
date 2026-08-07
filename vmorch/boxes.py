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
               nets as netlib, network, services, sizes, snapshots,
               spec as spec_mod, sshconf, virsh)
from .spec import BoxSpec


class BoxError(RuntimeError):
    pass


@dataclass
class Box:
    spec: BoxSpec
    state: str
    ip: str
    cid: int
    #: Something the caller should say out loud about the operation that
    #: produced this Box -- currently only `apply`, when reconciling the guest's
    #: network either happened or could not. Not part of the box's state, and
    #: never set by `load`.
    note: str = ""

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


def arm_filters(box_spec: BoxSpec) -> None:
    """Redefine every filter this box's NICs reference, immediately before a start.

    **Load-bearing.** network.arm_filters() explains the finding: a box started
    without a filter definition just before it is not actually filtered, for
    around a hundred seconds, whatever `nwfilter-binding-list` says. What
    matters is only that a define happens between the previous state and the
    start.

    That call covers the three *shared* filters. It does not cover the ones
    actually bound to the interfaces: nic0 references vmorch-box-<name>, and
    every local-net NIC references vmorch-net-<net>-<box>. Those were redefined
    on create and apply -- which build them anyway -- but not on `vmorch start` or
    `vmorch reseed`, so those two paths still started boxes whose own filters had
    not been touched. Same hole, on the paths the original fix did not reach.

    Everything a start needs is therefore defined here, in one place, and every
    start path calls this rather than assembling its own subset.
    """
    network.arm_filters()
    services.ensure_box_filter(box_spec)
    _ensure_net_filters(box_spec)


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


#: Kept as thin aliases so call sites read the same as before; the one
#: implementation lives in sizes.py.
_bytes_of = sizes.parse


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
            f"box {box_spec.name!r} already exists -- use `vmorch apply` to change it"
        )
    if virsh.domain_exists(box_spec.domain):
        raise BoxError(f"libvirt already has a domain named {box_spec.domain!r}")

    config.ensure_state_dirs()
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
    _ensure_net_filters(box_spec)

    disk = _create_overlay(box_spec, base)
    seed = cloudinit.build_seed(box_spec, box_dir(box_spec.name),
                                allocation.mac, allocation.wan_mac,
                                nets=net_attachments(box_spec),
                                gateway=gateway_for(box_spec)[0],
                                resolver=gateway_for(box_spec)[1],
                                router_subnets=[netlib.get(n).subnet
                                                for n in box_spec.routes_for])
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
        # Immediately before the start, every time: see arm_filters above.
        # ensure_base() near the top is too early -- the image copy, the
        # overlay and the seed build all happen in between.
        arm_filters(box_spec)
        virsh.run("start", box_spec.domain)

        # A router is the one box whose readiness other boxes depend on, so it
        # is the one box `create` waits for. Its cloud-init runcmd does set up
        # forwarding -- but runcmd finishes *after* ssh comes up, so a peer
        # created moments later can be running, correctly pointed at the router,
        # and getting nothing. Observed directly: one run of the same test
        # passed and the next failed on exactly that gap. Re-running the step
        # over ssh is idempotent, and makes the outcome deterministic rather
        # than a race against cloud-init.
        if box_spec.routes_for:
            _wait_reachable(box_spec.name)
            guest.configure_router(
                box_spec.name,
                [netlib.get(n).subnet for n in box_spec.routes_for])

        # The sudo password is set here rather than in the seed, so it never
        # exists in cleartext inside the box's own cloud-init data. Needs the
        # box up, hence after the start.
        if box_spec.sudo == "password":
            _wait_reachable(box_spec.name)
            cloudinit.set_password(box_spec)

    return load(box_spec.name)


def _ensure_net_filters(box_spec: BoxSpec) -> None:
    """Define this box's per-net filters before the domain references them.

    libvirt refuses a domain whose <filterref> names a filter that does not
    exist, so this has to run before define_domain -- and it has to run on every
    apply, because the pinned address moves if the box's allocation ever does.
    """
    for name in box_spec.nets:
        net = netlib.get(name)
        netlib.ensure(net)
        netlib.ensure_box_filter(net, box_spec.name,
                                 router=name in box_spec.routes_for)


def net_attachments(box_spec: BoxSpec) -> list[tuple[str, str, str]]:
    """(net, mac, address) for each local net, for the guest's netplan."""
    out = []
    for name in box_spec.nets:
        net = netlib.get(name)
        out.append((name, net.mac(box_spec.name), net.address(box_spec.name)))
    return out


def attach_net(name: str, net_name: str, router: bool = False) -> Box:
    """Put a box on a local network. Restarts it if it is running.

    A restart rather than a hot-plug: the guest needs its address written before
    the interface exists, and `apply` already does exactly that for every other
    spec change. One code path beats a second, live one that can disturb the
    network a working agent is on.
    """
    netlib.get(net_name)                    # raises if the net does not exist
    box = load(name)
    if net_name in box.spec.nets:
        raise BoxError(f"{name} is already attached to {net_name}")
    box.spec.nets.append(net_name)
    if router and net_name not in box.spec.routes_for:
        box.spec.routes_for.append(net_name)
    save_spec(box.spec)
    return apply(name)


def detach_net(name: str, net_name: str) -> Box:
    box = load(name)
    if net_name not in box.spec.nets:
        raise BoxError(f"{name} is not attached to {net_name}")
    box.spec.nets.remove(net_name)
    if net_name in box.spec.routes_for:
        box.spec.routes_for.remove(net_name)
    save_spec(box.spec)
    result = apply(name)
    netlib.delete_box_filter(net_name, name)
    return result


def boxes_on_net(net_name: str) -> list[str]:
    """Names of boxes whose spec attaches them to this local network."""
    found = []
    for name in list_names():
        try:
            if net_name in spec_mod.load(spec_path(name)).nets:
                found.append(name)
        except Exception:                             # noqa: BLE001
            continue
    return found


def router_on_net(net_name: str) -> str | None:
    """The box that forwards for this net, if any. First one wins.

    More than one router on a segment is not rejected -- it is a legitimate if
    unusual thing to want -- but only one can supply the default route, so this
    picks deterministically rather than by whichever spec was read first.
    """
    for name in sorted(boxes_on_net(net_name)):
        try:
            if net_name in spec_mod.load(spec_path(name)).routes_for:
                return name
        except Exception:                             # noqa: BLE001
            continue
    return None


def gateway_for(box_spec: BoxSpec) -> tuple[str | None, str | None]:
    """(gateway address, resolver) for a box that should route via a peer.

    None for a box with its own NAT NIC: `internet = true` already gives it a
    default route, and a second one would race. The box's own grant wins, always.

    The resolver is the NAT network's, because that is the only one a box behind
    a gateway can actually use -- the WAN filter drops DNS to anything else, so a
    query for 1.1.1.1 is masqueraded by the router and then dropped by the
    router's own filter.
    """
    if box_spec.internet:
        return None, None
    for name in box_spec.nets:
        if name in box_spec.routes_for:
            continue                       # a router does not route via itself
        router = router_on_net(name)
        if router:
            try:
                return netlib.get(name).address(router), network.nat_gateway()
            except Exception:                         # noqa: BLE001
                return netlib.get(name).address(router), None
    return None, None


def _disk_shortfall(name: str, box_spec: BoxSpec) -> int:
    """How many bytes the spec asks for beyond the real disk. 0 if in step.

    Raises rather than returning a negative number: a spec asking for *less*
    than the disk already is cannot be satisfied, and quietly ignoring it is
    what this function exists to stop.
    """
    disk = disk_path(name)
    if not disk.exists():
        return 0
    actual = _virtual_size(disk)
    wanted = _bytes_of(box_spec.disk)
    if wanted < actual:
        raise BoxError(
            f"{name}: box.toml asks for disk = {box_spec.disk} but the disk is "
            f"already {_format_size(actual)}. Shrinking discards the end of the "
            "device, where the filesystem keeps its data, so it is refused. Set "
            f'disk = "{_format_size(actual)}" (or larger) and apply again.'
        )
    return wanted - actual


def _needs_wan_config(name: str, box_spec: BoxSpec, running: bool) -> bool:
    """True if the guest has to be told about an internet NIC it does not know.

    Asks the guest what it is configured for rather than diffing box.toml
    against its previous contents, so this also repairs a box already left in
    the broken state -- desired vs actual, exactly like _disk_shortfall.

    A box that cannot be asked (stopped, or not answering ssh) is reported as
    not needing it; apply() warns separately in that case, because guessing
    would mean either a spurious warning or a silent miss.
    """
    if not box_spec.internet or not running:
        return False
    try:
        return not guest.has_wan_config(name)
    except guest.GuestError:
        return False


def apply(name: str) -> Box:
    """Regenerate the domain from the spec and reconcile.

    Deliberately does not re-run cloud-init: that ran once, at first boot, and
    a reseed rewrites host keys and every file cloud-init owns -- far too much
    for a one-line spec edit. Later changes go through domain XML and narrow
    in-guest commands instead.

    Two things are reconciled *inside* the guest, because the spec is the source
    of truth and a field in box.toml has to mean something:

      disk      grown to match, partition and filesystem included
      internet  the new NIC's netplan written, so it comes up on the restart

    Both were silent no-ops once. Editing the size, or the internet flag,
    reported success and changed nothing, leaving the spec describing a box that
    did not exist.
    """
    box = load(name)
    # Checked before the restart below, so a spec that cannot be satisfied
    # fails immediately instead of after bouncing a running box.
    shortfall = _disk_shortfall(name, box.spec)
    allocation = alloc.allocate(name)
    services.ensure_box_filter(box.spec)
    # Before domain.build: libvirt refuses a domain whose <filterref> names a
    # filter that does not exist yet.
    _ensure_net_filters(box.spec)

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

    # Before the restart, and only while the box is still up: this needs ssh,
    # and it needs the config in place by the time the guest boots with the new
    # NIC. Writing it costs nothing when it turns out not to be needed.
    wan_note = ""
    if box.spec.internet and not was_running:
        wan_note = (
            f"{name} is stopped, so its internet NIC could not be configured "
            "from here. If it comes up without an address, run "
            f"`vmorch reseed {name}`."
        )
    elif _needs_wan_config(name, box.spec, was_running):
        try:
            guest.configure_wan(name, allocation.wan_mac)
            wan_note = "configured the internet NIC inside the box"
        except guest.GuestError as exc:
            # Not fatal: the rest of the reconcile is still worth doing, and
            # the box keeps the management NIC either way. Say so rather than
            # reporting a clean success over a network that will not come up.
            wan_note = f"could not configure the internet NIC: {exc}"

    # Local nets, same rules: written before the restart, over ssh, and rewritten
    # whole so detaching removes the stanza rather than orphaning an interface
    # the guest keeps trying to raise.
    # Folder modes, before the restart and while ssh still works. The domain
    # XML carries <readonly/> either way, but a live box keeps the device it
    # was given, so without this a ro->rw change is a silent no-op until the
    # next boot -- and the guest's own mount options never change at all.
    if was_running:
        for folder in box.spec.folders:
            try:
                guest.mount_folder(name, folder)
            except guest.GuestError:
                pass          # apply() reports the network note; a share that
                              # cannot be remounted is recovered by `vmorch mount`

    net_note = ""
    if was_running:
        try:
            gw, resolver = gateway_for(box.spec)
            guest.configure_nets(name, net_attachments(box.spec), gw, resolver)
            # The router's own forwarding, after its addresses: masquerade rules
            # name the subnets it fronts, which it has to be on first.
            guest.configure_router(
                name, [netlib.get(n).subnet for n in box.spec.routes_for])
            bits = []
            if box.spec.nets:
                bits.append(f"attached to {', '.join(box.spec.nets)}")
            if box.spec.routes_for:
                bits.append(f"routing for {', '.join(box.spec.routes_for)}")
            if gw:
                bits.append(f"default route via {gw}")
            net_note = "; ".join(bits)
        except guest.GuestError as exc:
            net_note = f"could not configure local networks: {exc}"
    elif box.spec.nets:
        net_note = (f"{name} is stopped; its local network addresses will be "
                    "written the next time it is applied while running")

    if was_running:
        _graceful_restart_stop(name)

    virsh.define_domain(xml)
    _regenerate_ssh()

    if was_running:
        _ensure_console_log(name)
        _ensure_disk_perms(name)
        arm_filters(box.spec)
        virsh.run("start", box.spec.domain)

    # After the restart, so a box that was running grows its filesystem online
    # in the same step rather than needing a second command.
    if shortfall > 0:
        resize_disk(name, box.spec.disk)

    applied = load(name)
    applied.note = "; ".join(n for n in (wan_note, net_note) if n)
    return applied


def start(name: str) -> None:
    box = load(name)
    _ensure_console_log(name)
    _ensure_disk_perms(name)
    if box.state != "running":
        arm_filters(box.spec)
        virsh.run("start", box.spec.domain)


def stop(name: str, force: bool = False) -> None:
    box = load(name)
    if box.state == "running":
        virsh.run("destroy" if force else "shutdown", box.spec.domain)


def destroy(name: str, keep_disk: bool = False) -> None:
    """Remove a box, and everything the host was holding for it."""
    box = load(name)
    # `get`, not `allocate`: a cleanup path must read the record, never make
    # one. allocate() re-issues an address whose subnet no longer matches the
    # config, so calling it here handed destroy a *fresh* mac and ip and it
    # then tried to release a reservation that had never existed -- aborting
    # part-way and leaving both the stale reservation and a new, unreleased
    # ledger entry behind.
    allocation = alloc.get(name)

    if virsh.domain_exists(box.spec.domain):
        if virsh.domain_state(box.spec.domain) == "running":
            virsh.run("destroy", box.spec.domain)
        virsh.run("undefine", box.spec.domain, "--nvram")

    sshconf.forget_host(box.ip)

    if not keep_disk:
        shutil.rmtree(box_dir(name), ignore_errors=True)

    services.delete_box_filter(name)
    # Per-net filters too. A leftover blocks `vmorch net rm` on a network whose last
    # box is gone, and would be inherited by a box recreated under the same name
    # -- carrying a pinned address that may no longer be the one it is given.
    for net_name in box.spec.nets:
        netlib.delete_box_filter(net_name, name)
    # A box with no ledger entry is already half-gone; carry on cleaning up the
    # rest rather than refusing to finish.
    if allocation is not None:
        network.unreserve_address(name, allocation.mac, allocation.ip)
    network.unreserve_by_name(name)
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
            # show in `vmorch show` while doing nothing -- the same silent lie the
            # unimplemented service mechanisms used to tell.
            box.spec.folders = [f for f in box.spec.folders if f.tag != tag]
            save_spec(box.spec)
            if "PCI slot" in exc.stderr:
                raise BoxError(
                    f"{name} has no free PCI slots for another share. Boxes "
                    "created before this was fixed have no spares; restart it "
                    f"(`vmorch stop {name} && vmorch apply {name} && vmorch start {name}`) "
                    "to pick up the extra ports."
                ) from None
            raise
        try:
            guest.mount_folder(name, folder)
        except guest.GuestError as exc:
            raise BoxError(
                f"{name}: the share is attached and saved, but mounting it "
                f"inside the box failed ({exc}). Run `vmorch mount {name}` once the "
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
                         instance_id=f"{box.spec.domain}-{stamp}",
                         nets=net_attachments(box.spec),
                         gateway=gateway_for(box.spec)[0],
                         resolver=gateway_for(box.spec)[1],
                         router_subnets=[netlib.get(n).subnet
                                         for n in box.spec.routes_for])

    # Reseeding regenerates the box's ssh host keys, so the entry we already
    # trust is about to become wrong. Left in place it produces "Host key
    # verification failed" and the repair command leaves the box unreachable --
    # the opposite of its job.
    sshconf.forget_host(box.ip)

    if box.state == "running":
        _graceful_restart_stop(name)
    _ensure_console_log(name)
    _ensure_disk_perms(name)
    arm_filters(box.spec)
    virsh.run("start", box.spec.domain)

    # Reseeding relocks the account (the seed's runcmd does), so password mode
    # has to be re-established afterwards or `vmorch password` prints a secret the
    # box no longer accepts.
    if box.spec.sudo == "password":
        _wait_reachable(name)
        cloudinit.set_password(box.spec)
    return load(name)


def set_sudo(name: str, mode: str) -> Box:
    """Change what the agent user may do with sudo.

    Only written to the spec here. The rule lives in a sudoers file that
    cloud-init writes, and cloud-init runs once -- so this needs `vmorch reseed` to
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
    # it qemu-img refuses to report anything at all -- which would make `vmorch disk`
    # work only on stopped boxes. Read-only inspection, so sharing is safe.
    info = json.loads(subprocess.run(
        ["qemu-img", "info", "-U", "--output=json", str(disk)],
        capture_output=True, text=True, check=True,
    ).stdout)
    return int(info["virtual-size"])


_format_size = sizes.render


def resize_disk(name: str, size: str) -> dict:
    """Grow a box's disk. Accepts an absolute size ("60G") or an increment ("+20G").

    Three things have to change together, and stopping halfway is what makes a
    resize look broken:

    1. the qcow2 active layer, so the virtual device is bigger;
    2. the guest's partition table and filesystem, or `df` is unchanged;
    3. the spec, or the next `vmorch apply` reverts the box to its old size.

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
    except (sizes.SizeError, IndexError) as exc:
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
            f"stop {name} first: `vmorch stop {name}`. Snapshotting a running box "
            "means a crash-consistent image at best."
        )
    return snapshots.create(box_dir(name), disk_path(name), label)


def rollback(name: str, index: int):
    box = load(name)
    if box.state == "running":
        raise BoxError(f"stop {name} first: `vmorch stop {name}`")
    # Pass the spec size so rewinding past a `vmorch disk` does not quietly undo it.
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
    # others would record a grant in the spec, show it in `vmorch show`, and deliver
    # nothing -- a silent no-op is worse than a refusal.
    if via != "filter":
        raise BoxError(
            f"via={via!r} is designed but not implemented yet; use via=filter. "
            "See the module docstring in vmorch/services.py for what ssh and "
            "vsock would do."
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
    # Checked before it reaches a shell in the guest, even though it must
    # already be in the spec to be revoked -- a spec edited by hand is still an
    # input.
    spec_mod.validate_service_name(svc_name)
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
