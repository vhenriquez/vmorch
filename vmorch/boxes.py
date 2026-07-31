"""Box lifecycle: create, start, stop, apply, destroy.

A box outlives its first session. Its spec is a durable artifact on disk, the
domain XML is regenerated from it, and `apply` reconciles a box that already
exists. That is what separates this from a one-shot `virt-install` command line.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import (alloc, cloudinit, config, domain, guest, hostaccess, images,
               network, services, spec as spec_mod, sshconf, virsh)
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


def _ensure_console_log(name: str) -> Path:
    """Pre-create the console log so its owner can actually read it.

    libvirtd creates this file itself as root:root 0600 -- and a 0600 creation
    mode zeroes the ACL mask, so a default ACL cannot rescue it either. If the
    file already exists libvirt appends instead of creating, so making it here
    with a sane mode is what keeps `vm logs` working at all.
    """
    log = console_log(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.touch(mode=0o644)
    return log


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
        virsh.run("destroy", box.spec.domain)

    virsh.define_domain(xml)
    _regenerate_ssh()

    if was_running:
        virsh.run("start", box.spec.domain)

    return load(name)


def start(name: str) -> None:
    box = load(name)
    _ensure_console_log(name)
    if box.state != "running":
        virsh.run("start", box.spec.domain)


def stop(name: str, force: bool = False) -> None:
    box = load(name)
    if box.state == "running":
        virsh.run("destroy" if force else "shutdown", box.spec.domain)


def destroy(name: str, keep_disk: bool = False) -> None:
    """Remove a box. Its identifiers stay burned -- see alloc.py."""
    box = load(name)

    if virsh.domain_exists(box.spec.domain):
        if virsh.domain_state(box.spec.domain) == "running":
            virsh.run("destroy", box.spec.domain)
        virsh.run("undefine", box.spec.domain, "--nvram")

    sshconf.forget_host(box.ip)

    if not keep_disk:
        shutil.rmtree(box_dir(name), ignore_errors=True)

    services.delete_box_filter(name)
    alloc.release(name)
    _regenerate_ssh()


def _wait_reachable(name: str, timeout: int = 120) -> None:
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
    box = apply(name)

    # cloud-init already ran on this box, so the mount will not appear by
    # itself. Do it over SSH -- this is the reconfigure path, not the create
    # path.
    if box.state == "running":
        _wait_reachable(name)
        guest.mount_folder(name, folder)
    return box


def grant_service(name: str, svc_name: str, host_port: int, guest_port: int,
                  via: str = "filter") -> Box:
    """Let a box reach a host service. Every grant is a deliberate hole."""
    box = load(name)
    if any(s.name == svc_name for s in box.spec.from_host):
        raise BoxError(f"box {name!r} already has service {svc_name!r}")

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
    if virsh.domain_state(box.spec.domain) == "running" and guest.reachable(name):
        guest.unmount_folder(name, tag)

    box.spec.folders = remaining
    save_spec(box.spec)
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
