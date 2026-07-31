"""cloud-init seed generation.

This is what removes the OS installer from the loop: a distro cloud image plus a
seed ISO boots straight to a configured, SSH-reachable system.

**cloud-init runs once, on first boot.** Nothing in the reconfigure path may
depend on it -- `vm apply` on an existing box works through domain XML and
in-guest commands over SSH instead. The create path and the reconfigure path
look deceptively similar and are not the same thing.

SSH identity: a dedicated key at ~/.ssh/vmorch_ed25519 is generated on first
use. The tool deliberately does not go looking through the owner's existing
keys -- a purpose-built key for boxes is better practice anyway, and it keeps
this tool out of files it has no business reading.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
from .spec import BoxSpec

SSH_KEY = config.SSH_DIR / "vmorch_ed25519"


def ensure_keypair() -> Path:
    """Return the public key path, generating the pair on first use."""
    pub = SSH_KEY.with_suffix(".pub")
    if not pub.exists():
        config.SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(SSH_KEY),
             "-C", "vmorch"],
            check=True,
            capture_output=True,
        )
    return pub


def user_data(spec: BoxSpec) -> str:
    pub = ensure_keypair().read_text().strip()

    lines = [
        "#cloud-config",
        f"hostname: {spec.name}",
        f"fqdn: {spec.name}.vmorch.local",
        "preserve_hostname: false",
        "",
        "users:",
        f"  - name: {spec.user}",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    shell: /bin/bash",
        "    lock_passwd: true",
        "    ssh_authorized_keys:",
        f"      - {pub}",
        "",
        # The agent owns the box completely; root there is expected, not a
        # concern. The boundary is the VM, not in-guest privilege.
        "ssh_pwauth: false",
        "disable_root: true",
        "",
    ]

    if spec.packages:
        lines.append("packages:")
        lines += [f"  - {p}" for p in spec.packages]
        lines.append("")

    if spec.folders:
        # Mount ro for read-only shares as well as marking <readonly/> in the
        # domain XML. Two layers must fail before an agent can write to a host
        # folder it was not granted.
        lines.append("mounts:")
        for f in spec.folders:
            opts = "ro" if f.readonly else "rw"
            lines.append(
                f"  - [{f.tag}, /mnt/{f.tag}, virtiofs, "
                f'"defaults,{opts},nofail", "0", "0"]'
            )
        lines.append("")

        lines.append("runcmd:")
        for f in spec.folders:
            lines.append(f"  - [mkdir, -p, /mnt/{f.tag}]")
        lines.append("  - [mount, -a]")
        lines.append("")

    return "\n".join(lines)


def meta_data(spec: BoxSpec) -> str:
    # instance-id is what cloud-init uses to decide whether it has already run
    # for this instance. Keyed to the box name so a rebuilt box re-runs setup.
    return f"instance-id: {spec.domain}\nlocal-hostname: {spec.name}\n"


def network_config(spec: BoxSpec, mgmt_mac: str, wan_mac: str) -> str:
    """Netplan config for the seed. Required, not optional.

    cloud-init's fallback config only ever brings up **one** interface. With
    two NICs that means the internet NIC is created by libvirt, never
    configured by the guest, and the box has "internet" with no address and no
    default route. Verified on 2026-07-31.

    Interfaces are matched by MAC because interface *names* depend on PCI
    enumeration order, which is not something to bet the management path on.

    DNS deliberately follows DHCP per NIC rather than being pinned:

      internet = false  nothing to resolve with, because the isolated network's
                        dnsmasq does not forward upstream. Name resolution just
                        fails -- which also closes DNS as a covert channel out
                        of an isolated box.
      internet = true   the NAT network's dnsmasq serves DNS over the internet
                        NIC. Its address is itself RFC1918, so the egress drop
                        carves it out explicitly; see network.nat_gateway().
    """
    lines = [
        "version: 2",
        "ethernets:",
        "  mgmt:",
        "    match:",
        f'      macaddress: "{mgmt_mac}"',
        "    dhcp4: true",
        "    dhcp6: false",
        # The management network is isolated and has no route out. Refusing its
        # routes and DNS outright removes any chance of a DHCP race leaving the
        # box with a default route that blackholes.
        "    dhcp4-overrides:",
        "      use-routes: false",
        "      use-dns: false",
    ]
    if spec.internet:
        lines += [
            "  wan:",
            "    match:",
            f'      macaddress: "{wan_mac}"',
            "    dhcp4: true",
            "    dhcp6: false",
        ]
    return "\n".join(lines) + "\n"


def build_seed(spec: BoxSpec, out_dir: Path, mgmt_mac: str, wan_mac: str) -> Path:
    """Write a NoCloud seed ISO. Returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ud = out_dir / "user-data"
    md = out_dir / "meta-data"
    nc = out_dir / "network-config"
    ud.write_text(user_data(spec))
    md.write_text(meta_data(spec))
    nc.write_text(network_config(spec, mgmt_mac, wan_mac))

    seed = out_dir / "seed.iso"
    subprocess.run(
        ["cloud-localds", f"--network-config={nc}", str(seed), str(ud), str(md)],
        check=True,
        capture_output=True,
    )
    return seed
