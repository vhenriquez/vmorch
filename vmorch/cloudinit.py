"""cloud-init seed generation.

This is what removes the OS installer from the loop: a distro cloud image plus a
seed ISO boots straight to a configured, SSH-reachable system.

**cloud-init runs once, on first boot.** Nothing in the reconfigure path may
depend on it -- `vmorch apply` on an existing box works through domain XML and
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

    "password" is not the same as "the agent can escalate": the secret is
    generated host-side and set over SSH after first boot, so it never enters
    the seed ISO or cloud-init's instance data. A human who needs to fix
    something reads it with `vmorch password <box>`.

    It used to be written into a cloud-init runcmd, which put it in cleartext on
    a device attached to the box and in /var/lib/cloud -- protected by nothing
    but in-guest file permissions, which is the exact layer this mode exists to
    defend against. See set_password() below.
    """
    if spec.sudo == "nopasswd":
        return f"{spec.user} ALL=(ALL) NOPASSWD:ALL"
    if spec.sudo == "password":
        return f"{spec.user} ALL=(ALL) ALL"
    return f"# {spec.user}: no sudo (vmorch agent_sudo = none)"


def _router_file(subnets: list[str] | None) -> list[str]:
    """The router setup script, as an entry in the ONE write_files block.

    Emitted here rather than as its own `write_files:` further down, because a
    second key of that name is not a second section -- YAML keeps one mapping
    key and drops the other, so either the sudoers rule or this script would
    silently disappear.
    """
    if not subnets:
        return []
    from .guest import router_script
    out = ["  - path: /var/lib/vmorch/make-router.sh",
           "    permissions: '0700'",
           "    content: |"]
    out += [f"      {line}" for line in router_script(subnets).splitlines()]
    return out


def user_data(spec: BoxSpec, router_subnets: list[str] | None = None) -> str:
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
    # Group membership is not free, and it is not the same question as sudo.
    #
    #   kvm     /dev/kvm is root:kvm 0660, so a nested box exposes the device
    #           but the agent cannot open it without this -- the emulator falls
    #           back to software and is unusably slow with nothing obviously
    #           wrong. Only granted when the box actually asked for nested.
    #   docker  membership is root-equivalent on any image where docker is
    #           installed: the socket will run a privileged container for you.
    #           Adding it unconditionally meant sudo = "none" -- the one mode
    #           whose entire purpose is to stop in-guest escalation -- did not,
    #           on any golden image with docker baked in.
    #
    # So both are granted only where they cost nothing that has not already
    # been granted deliberately.
    groups = []
    if spec.nested:
        groups.append("kvm")
    if spec.sudo == "nopasswd":
        groups.append("docker")
    lines += [
        f"    groups: [{', '.join(groups)}]" if groups else "    groups: []",
        "    shell: /bin/bash",
        "    lock_passwd: true",
        "    ssh_authorized_keys:",
        f"      - {pub}",
        "",
        # The tool's own privileged path, independent of whatever the agent
        # user is allowed. Key-only, and the private half never leaves the
        # host, so the agent cannot use this entry even though it can read
        # nothing of it. Without this, taking sudo away from the agent would
        # also break `vmorch share`, `vmorch service` and `vmorch golden`.
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
        *_router_file(router_subnets),
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
    # Mountpoints, created before the mounts module runs. These belong in
    # runcmd and used to be appended after the `mounts:` block instead -- where
    # a blank line does not end a YAML sequence, so they became three more
    # entries in `mounts:` and cloud-init read ["mkdir", "-p", "/mnt/tag"] as an
    # fstab line whose device was "mkdir".
    for f in spec.folders:
        runcmds.append(f"  - [mkdir, -p, /mnt/{f.tag}]")
    if router_subnets:
        # A box created as a router has to come up routing. Wiring this into
        # `apply` alone shipped once and left ip_forward = 0 on a brand-new
        # firewall while its peers had correct default routes pointing at it --
        # everything looked configured and nothing forwarded.
        #
        # The script is generated by guest.py, so the ssh path and the first-boot
        # path cannot drift apart.
        runcmds.append("  - [bash, /var/lib/vmorch/make-router.sh]")
    # The account starts locked in every mode, password included. For that mode
    # the real password is set over SSH once the box is up (see set_password);
    # locking it here means there is no window in which the account exists with
    # no password at all, and nothing secret is ever written into the seed.
    runcmds.append(f"  - [passwd, -l, {spec.user}]")
    lines += ["runcmd:", *runcmds, ""]

    if spec.folders:
        # Mount ro for read-only shares as well as marking <readonly/> in the
        # domain XML. Two layers must fail before an agent can write to a host
        # folder it was not granted.
        #
        # Nothing may be appended after this block: it is a YAML sequence, and a
        # blank line does not close one. Anything that follows becomes another
        # mount entry.
        lines.append("mounts:")
        for f in spec.folders:
            opts = "ro" if f.readonly else "rw"
            lines.append(
                f"  - [{f.tag}, /mnt/{f.tag}, virtiofs, "
                f'"defaults,{opts},nofail", "0", "0"]'
            )
        lines.append("")

    return "\n".join(lines)


def set_password(spec: BoxSpec) -> None:
    """Set the agent's sudo password inside a running box, over SSH.

    Deliberately not part of the seed. cloud-init's user-data is readable inside
    the guest -- it rides in on a virtual CD-ROM and is kept under /var/lib/cloud
    -- so a password placed there is guarded only by in-guest permissions. That
    is precisely the boundary sudo = "password" is meant to hold, so the secret
    goes in over the tool's own root SSH channel instead, after boot, and the
    host keeps the only durable copy.

    No-op unless the box actually asked for password mode.
    """
    if spec.sudo != "password":
        return
    from . import guest
    secret = box_password(spec.name)
    # Passed on stdin, never as an argument: an argument is visible in the
    # guest's own process list for as long as the command runs.
    guest.run(spec.name,
              f"set -e\nread -r _pw\nprintf '%s:%s' {spec.user} \"$_pw\" "
              "| chpasswd\n",
              stdin_extra=secret + "\n")


def meta_data(spec: BoxSpec, instance_id: str | None = None) -> str:
    # instance-id is what cloud-init uses to decide whether it has already run.
    # Keyed to the box name so a rebuilt box re-runs setup -- and changing it is
    # the supported way to make an EXISTING box run its first-boot config again,
    # which is what `vmorch reseed` does to repair a box whose ssh has broken.
    return (f"instance-id: {instance_id or spec.domain}\n"
            f"local-hostname: {spec.name}\n")


def network_config(spec: BoxSpec, mgmt_mac: str, wan_mac: str,
                   nets: list[tuple[str, str, str]] | None = None,
                   gateway: str | None = None,
                   resolver: str | None = None) -> str:
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
        # A box with no NAT NIC of its own can still reach the world through a
        # router on one of its nets. Never for a box that has internet: its own
        # grant supplies the default route and two would race.
        if gateway and gateway.rsplit(".", 1)[0] == address.rsplit(".", 1)[0]:
            lines += ["    routes:",
                      "      - to: default",
                      f"        via: {gateway}"]
            if resolver:
                lines += ["    nameservers:",
                          f"      addresses: [{resolver}]"]
            gateway = None
    return "\n".join(lines) + "\n"


def build_seed(spec: BoxSpec, out_dir: Path, mgmt_mac: str, wan_mac: str,
               instance_id: str | None = None,
               nets: list[tuple[str, str, str]] | None = None,
               gateway: str | None = None,
               resolver: str | None = None,
               router_subnets: list[str] | None = None) -> Path:
    """Write a NoCloud seed ISO. Returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ud = out_dir / "user-data"
    md = out_dir / "meta-data"
    nc = out_dir / "network-config"
    ud.write_text(user_data(spec, router_subnets))
    md.write_text(meta_data(spec, instance_id))
    nc.write_text(network_config(spec, mgmt_mac, wan_mac, nets,
                                 gateway, resolver))

    seed = out_dir / "seed.iso"
    subprocess.run(
        ["cloud-localds", f"--network-config={nc}", str(seed), str(ud), str(md)],
        check=True,
        capture_output=True,
    )
    return seed
