"""Generate the ssh config fragment so `ssh <name>` just works.

Four things have to line up, and missing any one of them makes the experience
worse than plain `ssh user@ip`:

1. A stable address -- handled by the MAC-pinned DHCP reservation in alloc.py.
2. **This tool owns exactly one file.** ~/.ssh/config gets a single `Include`
   line added once; everything else lives in ~/.ssh/config.d/vmorch, which is
   rewritten wholesale. The owner's hand-maintained config is never rewritten.
3. **A separate known_hosts.** Rebuilding a box under the same name changes its
   host key; without this, ssh refuses to connect and shouts about a possible
   MITM every single time.
4. Key injection -- handled by cloud-init.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime

from . import alloc, config
from .cloudinit import SSH_KEY

HEADER = """# Managed by vmorch -- this file is rewritten in full on every change.
# Do not edit by hand; edit the box spec and run `vm apply <name>`.
"""


def ensure_include() -> bool:
    """Add the Include line to ~/.ssh/config. Returns True if added.

    Backs the file up first: it is the owner's, not ours.
    """
    config.SSH_CONFIG_D.mkdir(mode=0o700, parents=True, exist_ok=True)

    if config.SSH_CONFIG.exists():
        existing = config.SSH_CONFIG.read_text()
        if config.SSH_INCLUDE_LINE in existing:
            return False
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(
            config.SSH_CONFIG,
            config.SSH_CONFIG.with_suffix(f".vmorch-backup-{stamp}"),
        )
        # Include must precede host-specific blocks: ssh takes the first value
        # it sees for any given keyword, so a trailing Include can be shadowed.
        config.SSH_CONFIG.write_text(
            f"{config.SSH_INCLUDE_LINE}\n\n{existing}"
        )
    else:
        config.SSH_CONFIG.write_text(f"{config.SSH_INCLUDE_LINE}\n")
        config.SSH_CONFIG.chmod(0o600)
    return True


def _block(name: str, ip: str, user: str, forwards: list[str]) -> str:
    lines = [
        f"Host {name}",
        f"    HostName {ip}",
        f"    User {user}",
        f"    IdentityFile {SSH_KEY}",
        "    IdentitiesOnly yes",
        # A rebuilt box legitimately has a new host key. Keeping vmorch hosts
        # in their own file means that never touches the owner's known_hosts.
        f"    UserKnownHostsFile {config.SSH_KNOWN_HOSTS}",
        "    StrictHostKeyChecking accept-new",
    ]
    lines += [f"    {f}" for f in forwards]
    return "\n".join(lines)


def regenerate(boxes: list[tuple[str, str, list[str]]]) -> None:
    """Rewrite the fragment from the full set of boxes.

    Takes (name, user, forwards) and looks the address up from the ledger, so
    the generated config can never disagree with the DHCP reservation.
    """
    ensure_include()

    parts = [HEADER]
    for name, user, forwards in sorted(boxes):
        allocation = alloc.get(name)
        if allocation is None:
            continue
        parts.append(_block(name, allocation.ip, user, forwards))
        parts.append("")

    config.SSH_FRAGMENT.write_text("\n".join(parts))
    config.SSH_FRAGMENT.chmod(0o600)

    if not config.SSH_KNOWN_HOSTS.exists():
        config.SSH_KNOWN_HOSTS.touch(mode=0o600)


def forget_host(ip: str) -> None:
    """Drop a box's host key when it is destroyed.

    Without this, recreating a box reuses its address, ssh sees a *changed* key
    for a known host, and refuses to connect -- `accept-new` does not help,
    because the host is no longer new.

    Uses ssh-keygen -R rather than filtering the file directly: ssh writes
    hashed entries by default (`|1|...`), so matching plaintext addresses
    against those lines silently never fires. That exact bug shipped here once
    and was caught only by recreating a box.
    """
    if not config.SSH_KNOWN_HOSTS.exists():
        return
    subprocess.run(
        ["ssh-keygen", "-q", "-R", ip, "-f", str(config.SSH_KNOWN_HOSTS)],
        capture_output=True,
    )
    # ssh-keygen leaves a .old backup containing the very key we just removed.
    backup = config.SSH_KNOWN_HOSTS.with_suffix(config.SSH_KNOWN_HOSTS.suffix + ".old")
    backup.unlink(missing_ok=True)
