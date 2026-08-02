"""Run commands inside a box over SSH.

This is the other half of reconfiguration. cloud-init runs **once**, at first
boot, so anything that changes afterwards -- mounting a folder shared onto a box
that is already running, installing a package, restarting a service -- has to
happen here instead.

The create path and the reconfigure path look similar and are not the same
thing. Anything that only ever works at creation belongs in cloudinit.py;
anything that must work on an existing box belongs here.
"""

from __future__ import annotations

import subprocess

from .spec import Folder


class GuestError(RuntimeError):
    pass


def _ssh(target: str, argv: list[str], script: str):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, *argv],
        input=script, capture_output=True, text=True,
    )


def run(name: str, script: str, check: bool = True) -> str:
    """Execute a shell script inside the box, with privilege.

    Connects as **root over the tool's own key**, not as the agent user via
    sudo. That separation is the point: the agent's privileges can be reduced to
    nothing (`agent_sudo = "none"`) without breaking `vm share`, `vm service` or
    `vm golden`, because the tool never depended on the agent being root.

    Falls back to `agent` + sudo for boxes created before root access was
    provisioned -- cloud-init runs once, so an older box has no root key until
    it is reseeded.
    """
    proc = _ssh(f"root@{name}", ["bash", "-s"], script)

    if proc.returncode == 255:          # ssh transport failure, not the script
        legacy = _ssh(name, ["sudo", "bash", "-s"], script)
        if legacy.returncode != 255:
            proc = legacy

    if check and proc.returncode != 0:
        raise GuestError(
            f"in-guest command failed on {name} ({proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def reachable(name: str) -> bool:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", name, "true"],
        capture_output=True,
    ).returncode == 0


def mount_folder(name: str, folder: Folder) -> None:
    """Mount a shared folder now, and persist it across reboots.

    The guest-side mount is `ro` for a read-only share as well as the domain XML
    carrying <readonly/>. Two independent layers have to fail before an agent
    can write to a host folder it was not granted.

    `nofail` matters: without it a box whose share was later revoked refuses to
    finish booting, which turns a routine reconfiguration into a wedged box.
    """
    opts = "ro" if folder.readonly else "rw"
    mountpoint = f"/mnt/{folder.tag}"
    run(name, f"""set -e
mkdir -p {mountpoint}
grep -q ' {mountpoint} virtiofs ' /etc/fstab || \
  echo '{folder.tag} {mountpoint} virtiofs defaults,{opts},nofail 0 0' >> /etc/fstab
mountpoint -q {mountpoint} || mount {mountpoint}
""")


def unmount_folder(name: str, tag: str) -> None:
    mountpoint = f"/mnt/{tag}"
    run(name, f"""set -e
mountpoint -q {mountpoint} && umount {mountpoint} || true
sed -i '\\| {mountpoint} virtiofs |d' /etc/fstab
rmdir {mountpoint} 2>/dev/null || true
""")
