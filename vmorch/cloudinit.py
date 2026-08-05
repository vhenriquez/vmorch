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

import secrets
import subprocess
from pathlib import Path

from . import config
from .spec import BoxSpec

SSH_KEY = config.SSH_DIR / "vmorch_ed25519"


def password_path(name: str) -> Path:
    return config.BOXES_DIR / name / "sudo-password"


def box_password(name: str) -> str:
    """The sudo password for a box, generated once and kept on the host.

    Host-side on purpose: the guest gets only its hash, so an agent inside the
    box cannot read the secret it would need to escalate.
    """
    path = password_path(name)
    if path.exists():
        return path.read_text().strip()
    secret = secrets.token_urlsafe(18)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n")
    path.chmod(0o600)
    return secret


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


def _sudoers_line(spec: BoxSpec) -> str:
    """The one sudoers rule for the agent user.

    "password" is deliberately *not* the same as "the agent can escalate": the
    password is generated host-side and never placed in the guest in readable
    form, so an agent process that is compromised cannot use it. A human who
    needs to fix something can, with `vm password <box>`.
    """
    if spec.sudo == "nopasswd":
        return f"{spec.user} ALL=(ALL) NOPASSWD:ALL"
    if spec.sudo == "password":
        return f"{spec.user} ALL=(ALL) ALL"
    return f"# {spec.user}: no sudo (vmorch agent_sudo = none)"


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
    ]
    # NOT cloud-init's `sudo:` key. It adds an entry but does not remove one
    # that is already there, and a golden image carries whatever sudoers file
    # it was built with -- so `sudo: false` on an image built with NOPASSWD
    # leaves the agent fully privileged. Verified: it did exactly that.
    # The rule is written explicitly below instead, and the image's own file
    # deleted, so the outcome does not depend on what the image shipped.
    lines += [
        # kvm: /dev/kvm is root:kvm 0660, so a nested box exposes the device
        # but the agent cannot open it -- the emulator falls back to software
        # and is unusably slow, with nothing obviously wrong.
        # docker/lxd: harmless when absent, and saves a reboot when present.
        "    groups: [kvm, docker]",
        "    shell: /bin/bash",
        "    lock_passwd: true",
        "    ssh_authorized_keys:",
        f"      - {pub}",
        "",
        # The tool's own privileged path, independent of whatever the agent
        # user is allowed. Key-only, and the private half never leaves the
        # host, so the agent cannot use this entry even though it can read
        # nothing of it. Without this, taking sudo away from the agent would
        # also break `vm share`, `vm service` and `vm golden`.
        "  - name: root",
        "    ssh_authorized_keys:",
        f"      - {pub}",
        "",
        # The agent owns the box completely; root there is expected, not a
        # concern. The boundary is the VM, not in-guest privilege.
        "ssh_pwauth: false",
        "",
        "write_files:",
        "  - path: /etc/sudoers.d/90-vmorch-agent",
        "    permissions: '0440'",
        "    content: |",
        f"      {_sudoers_line(spec)}",
        "",
        # root login by key only, and only from the management network.
        "disable_root: false",
        "",
    ]

    if spec.packages:
        lines.append("packages:")
        lines += [f"  - {p}" for p in spec.packages]
        lines.append("")

    runcmds = [
        # Whatever the image baked in must go, or the explicit rule above is
        # merely additive and the old NOPASSWD entry still wins.
        "  - [rm, -f, /etc/sudoers.d/90-cloud-init-users]",
    ]
    if spec.sudo == "password":
        runcmds.append(f"  - [bash, -c, \"echo '{spec.user}:{box_password(spec.name)}' "
                       "| chpasswd\"]")
    else:
        runcmds.append(f"  - [passwd, -l, {spec.user}]")
    lines += ["runcmd:", *runcmds, ""]

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

        for f in spec.folders:
            lines.append(f"  - [mkdir, -p, /mnt/{f.tag}]")
        lines.append("  - [mount, -a]")
        lines.append("")

    return "\n".join(lines)


def meta_data(spec: BoxSpec, instance_id: str | None = None) -> str:
    # instance-id is what cloud-init uses to decide whether it has already run.
    # Keyed to the box name so a rebuilt box re-runs setup -- and changing it is
    # the supported way to make an EXISTING box run its first-boot config again,
    # which is what `vm reseed` does to repair a box whose ssh has broken.
    return (f"instance-id: {instance_id or spec.domain}\n"
            f"local-hostname: {spec.name}\n")


def network_config(spec: BoxSpec, mgmt_mac: str, wan_mac: str,
                   nets: list[tuple[str, str, str]] | None = None) -> str:
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

    # Local nets are static: the segment has no DHCP server, because it has no
    # host address at all -- that is what makes it members-only. Putting them in
    # the seed means a box created already attached comes up on its nets at
    # first boot, with no apply needed afterwards.
    for net_name, mac, address in (nets or []):
        lines += [
            f"  {net_name}:",
            "    match:",
            f'      macaddress: "{mac}"',
            f"    addresses: [{address}/24]",
            "    dhcp4: false",
            "    dhcp6: false",
        ]
    return "\n".join(lines) + "\n"


def build_seed(spec: BoxSpec, out_dir: Path, mgmt_mac: str, wan_mac: str,
               instance_id: str | None = None,
               nets: list[tuple[str, str, str]] | None = None) -> Path:
    """Write a NoCloud seed ISO. Returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ud = out_dir / "user-data"
    md = out_dir / "meta-data"
    nc = out_dir / "network-config"
    ud.write_text(user_data(spec))
    md.write_text(meta_data(spec, instance_id))
    nc.write_text(network_config(spec, mgmt_mac, wan_mac, nets))

    seed = out_dir / "seed.iso"
    subprocess.run(
        ["cloud-localds", f"--network-config={nc}", str(seed), str(ud), str(md)],
        check=True,
        capture_output=True,
    )
    return seed
