"""Grant the qemu account access to box disks, minimally.

QEMU runs as `libvirt-qemu` under qemu:///system and cannot read the state
directory by default. Two separate mechanisms have to be satisfied, and they
fail in ways that look identical:

**AppArmor** (handled in config.py, not here). Ubuntu's virt-aa-helper profile
denies any hidden path under $HOME outright, so the state directory must not
live in ~/.local or any other dot-directory. See the comment on STATE_DIR.

**DAC** (handled here). The state directory is kept private to the owner, so
qemu is granted access by ACL rather than by opening it to every local account:

    chmod o+rx ~/vmorch     would let any local user read every box disk
    setfacl -m u:libvirt-qemu:rwx  grants exactly one system account

A default ACL is set alongside so per-box directories and disks created later
inherit access instead of needing a fix-up pass.

All of this is done as the owner of the directories, so no sudo is involved,
and it reverses with `setfacl -bn ~/vmorch`.
"""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

from . import config

QEMU_USER = "libvirt-qemu"


def _setfacl(path: Path, perms: str, default: bool = False,
             user: str = QEMU_USER) -> None:
    entry = f"u:{user}:{perms}"
    args = ["setfacl", "-m", f"d:{entry}" if default else entry, str(path)]
    subprocess.run(args, check=True, capture_output=True)


def _needs_setup(path: Path, users: list[str]) -> bool:
    """True unless every required entry is already present, access and default.

    Checking all of them matters: guarding on just one entry means a later
    addition to the required set silently never gets applied to hosts that were
    already set up.
    """
    out = subprocess.run(
        ["getfacl", "-cE", str(path)], capture_output=True, text=True
    ).stdout
    return not all(
        f"{prefix}user:{user}:" in out
        for user in users
        for prefix in ("", "default:")
    )


def ensure() -> list[Path]:
    """Make the state tree reachable by qemu. Returns the paths changed."""
    changed: list[Path] = []
    me = getpass.getuser()

    for own in (config.STATE_DIR, config.BOXES_DIR, config.BASES_DIR):
        own.mkdir(mode=0o750, parents=True, exist_ok=True)
        if _needs_setup(own, [QEMU_USER, me]):
            for user in (QEMU_USER, me):
                _setfacl(own, "rwx", user=user)
                # libvirtd creates files in here as root:root 0600 -- console
                # logs especially. Without a *default* entry for the invoking
                # user, the owner cannot read the logs of their own boxes.
                _setfacl(own, "rwx", user=user, default=True)
            changed.append(own)

    return changed
