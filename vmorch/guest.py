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


def run(name: str, script: str, check: bool = True,
        stdin_extra: str = "") -> str:
    """Execute a shell script inside the box, with privilege.

    Connects as **root over the tool's own key**, not as the agent user via
    sudo. That separation is the point: the agent's privileges can be reduced to
    nothing (`agent_sudo = "none"`) without breaking `vmorch share`, `vmorch service` or
    `vmorch golden`, because the tool never depended on the agent being root.

    Falls back to `agent` + sudo for boxes created before root access was
    provisioned -- cloud-init runs once, so an older box has no root key until
    it is reseeded.
    """
    # stdin_extra is appended after the script, so `read` inside it consumes a
    # secret that never appears in an argument vector -- and so is never visible
    # in the guest's process list.
    payload = script + stdin_extra
    proc = _ssh(f"root@{name}", ["bash", "-s"], payload)

    if proc.returncode == 255:          # ssh transport failure, not the script
        legacy = _ssh(name, ["sudo", "bash", "-s"], payload)
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


def grow_root(name: str) -> str:
    """Expand the root partition and filesystem to fill the virtual disk.

    Growing the qcow2 only makes the *device* bigger. The guest still has a
    partition table and a filesystem describing the old size, so without this
    step `df` is unchanged and the extra space is invisible -- the single most
    confusing way for a resize to appear to have done nothing.

    Everything here is idempotent: run it on a box that is already at full size
    and it reports the same figures back.

    `growpart` ships in the cloud images, but the fallback is `sfdisk` rather
    than an install, because the boxes most likely to fill up are isolated ones
    that cannot reach a package mirror.

    The root partition has to be the last one on the disk for this to work,
    which is how every cloud image lays it out (Ubuntu puts the small ESP and
    boot partitions first, numbered 14-16, with root at partition 1).
    """
    return run(name, r"""set -e
src=$(findmnt -no SOURCE /)
fstype=$(findmnt -no FSTYPE /)
parent=$(lsblk -ndo PKNAME "$src")
[ -n "$parent" ] || { echo "cannot resolve the disk behind $src" >&2; exit 1; }
disk=/dev/$parent
num=${src#/dev/$parent}
num=${num#p}

rc=0
if command -v growpart >/dev/null 2>&1; then
    out=$(growpart "$disk" "$num" 2>&1) || rc=$?
else
    out=$(echo ', +' | sfdisk -N "$num" --no-reread --force "$disk" 2>&1) || rc=$?
    partx -u "$disk" >/dev/null 2>&1 || true
fi
echo "$out"
# growpart's man page says it exits 2 when there is nothing to grow. It does
# not -- it prints NOCHANGE and exits 1, the same status as a real failure. So
# the output is the signal, not the exit code, or every already-full-size box
# would report a spurious error.
if [ "$rc" -ne 0 ]; then
    case "$out" in
        *NOCHANGE*|*"no space"*) : ;;
        *) echo "growing the partition failed ($rc)" >&2; exit 1 ;;
    esac
fi

case "$fstype" in
    ext2|ext3|ext4) resize2fs "$src" ;;
    xfs)            xfs_growfs / ;;
    btrfs)          btrfs filesystem resize max / ;;
    *) echo "unrecognised root filesystem '$fstype': the partition was grown "\
            "but the filesystem was not" >&2 ;;
esac
df -h / | tail -1
""")


#: Where the WAN interface's config goes. Not cloud-init's own
#: 50-cloud-init.yaml: that file belongs to cloud-init, which rewrites it
#: wholesale on a reseed. A separate, later-sorting file merges with it instead
#: of fighting it, and both describe the same interface identically, so a reseed
#: afterwards is a no-op rather than a conflict.
WAN_NETPLAN = "/etc/netplan/60-vmorch-wan.yaml"


def configure_wan(name: str, wan_mac: str) -> str:
    """Teach a running guest about its internet NIC.

    Granting internet to an existing box adds a second NIC at the hypervisor,
    and that is all it does. The guest's network config was written by
    cloud-init at first boot, when there was one NIC, and cloud-init does not
    run again -- so nothing in the guest knows the interface exists and it sits
    there DOWN with no address while `vmorch apply` reports success.

    Deliberately writes the file and stops. It does *not* run `netplan apply`,
    because the caller restarts the box moments later and boot brings the
    interface up with no risk of tearing down the management link this very
    command is running over. Matching by MAC means the file can be written
    before the NIC exists.

    Idempotent, and left in place when internet is revoked: netplan ignores a
    `match` that resolves to nothing, and keeping it means re-granting internet
    later needs no in-guest step at all.
    """
    if not run(name, "command -v netplan >/dev/null 2>&1 && echo yes || echo no",
               check=False).strip().endswith("yes"):
        raise GuestError(
            f"{name} does not use netplan, so vmorch cannot configure its "
            "internet NIC from here. Run `vmorch reseed " + name + "` instead: that "
            "regenerates the seed and lets cloud-init write whatever the "
            "distro uses."
        )

    # 0600 to match what cloud-init writes; netplan warns loudly about a
    # world-readable config and the warning is on every boot.
    return run(name, f"""set -e
cat > {WAN_NETPLAN} <<'YAML'
# Written by vmorch when internet was granted to an existing box.
# cloud-init only configures NICs at first boot, so this file is what makes the
# second interface work without reseeding. Matched by MAC because interface
# names follow PCI enumeration order.
network:
  version: 2
  ethernets:
    wan:
      match:
        macaddress: "{wan_mac}"
      dhcp4: true
      dhcp6: false
YAML
chmod 600 {WAN_NETPLAN}
netplan generate
echo "wrote {WAN_NETPLAN}"
""")


#: One file for every local net NIC, rewritten whole. Sorts after cloud-init's
#: 50- and after the wan file, and being a single file means detaching a net
#: removes its stanza rather than leaving an orphan behind.
NETS_NETPLAN = "/etc/netplan/61-vmorch-nets.yaml"


ROUTER_NFT = "/etc/vmorch-router.nft"
ROUTER_UNIT = "/etc/systemd/system/vmorch-router.service"


def router_script(subnets: list[str]) -> str:
    """Shell that makes a box a router for `subnets`. One source, two callers.

    Used over ssh by `apply` **and** embedded in the cloud-init seed, because a
    box created as a router has to come up routing -- wiring it into `apply`
    alone shipped once and left `ip_forward = 0` on a brand-new firewall while
    everything else looked configured.

    Forwarding plus masquerade, and nothing else: vmorch makes the box a
    *router*, the firewall policy on top is the owner's. That is the whole point
    of putting a box in that position rather than a rule in the host's nwfilter.

    Masquerade matches on the **source subnet**, not an output interface.
    Interface names follow PCI enumeration order and netplan does not rename
    them -- `wan` is a netplan id, the kernel still calls it `enp2s0`. Excluding
    the segment's own subnet as a destination leaves member-to-member traffic
    un-NATed, so peers keep seeing each other's real addresses.

    **Persisted through a unit, not just applied.** nftables rules live in the
    kernel and do not survive a reboot; `sysctl.d` does. Writing one and not the
    other gives a router that works until it is restarted and then silently does
    not -- the worst kind of failure to debug.

    Idempotent: the table is replaced wholesale, and it is vmorch's own, so the
    owner's rules in their own tables are never touched.
    """
    if not subnets:
        return ("set -e\n"
                "systemctl disable --now vmorch-router.service 2>/dev/null || true\n"
                f"rm -f {ROUTER_NFT} {ROUTER_UNIT} "
                "/etc/sysctl.d/99-vmorch-router.conf\n"
                "nft delete table ip vmorch_nat 2>/dev/null || true\n"
                "sysctl -qw net.ipv4.ip_forward=0 || true\n"
                "systemctl daemon-reload 2>/dev/null || true\n"
                "echo 'router role removed'\n")

    rules = "\n".join(
        f"    ip saddr {net} ip daddr != {net} masquerade" for net in subnets)
    return f"""set -e
printf 'net.ipv4.ip_forward=1\\n' > /etc/sysctl.d/99-vmorch-router.conf
sysctl -qw net.ipv4.ip_forward=1

cat > {ROUTER_NFT} <<'NFT'
#!/usr/sbin/nft -f
# Written by vmorch. Masquerade for the local networks this box fronts.
# Matched on source subnet because interface names follow PCI order.
# Your own firewall rules belong in your own tables; this one is replaced
# wholesale every time vmorch reconciles the box.
delete table ip vmorch_nat
table ip vmorch_nat {{
  chain postrouting {{
    type nat hook postrouting priority srcnat; policy accept;
{rules}
  }}
}}
NFT

cat > {ROUTER_UNIT} <<'UNIT'
[Unit]
Description=vmorch router rules
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
# `delete table` fails when it does not exist yet, which is the normal case on
# a cold boot, so the file is applied with -f twice: once to create, once to
# replace. `|| true` on the first covers the cold case without hiding the second.
ExecStart=/bin/sh -c 'nft add table ip vmorch_nat; nft -f {ROUTER_NFT}'

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable vmorch-router.service
# restart, not `enable --now`: the unit is Type=oneshot with RemainAfterExit,
# so once it is active `--now` does nothing at all. Changing which subnets a
# router fronts wrote a new ruleset and never loaded it -- the box kept
# masquerading for the old list until someone rebooted it.
systemctl restart vmorch-router.service
echo "routing for {' '.join(subnets)}"
"""


def configure_router(name: str, subnets: list[str]) -> str:
    """Apply the router role to a running box over ssh."""
    return run(name, router_script(subnets), check=bool(subnets))


def configure_nets(name: str, attachments: list[tuple[str, str, str]],
                   gateway: str | None = None,
                   resolver: str | None = None) -> str:
    """Give the guest a static address on each local net it is attached to.

    `attachments` is (net name, mac, address) per net. `gateway` is a router's
    address on one of them, if this box is meant to reach the world through
    another box rather than through its own NAT NIC; `resolver` goes with it,
    because a box behind a gateway has no DNS otherwise.

    Static rather than DHCP because a local net has no dnsmasq -- it has no host
    address at all, which is what makes it members-only. The addresses are known
    before either box boots, so writing them straight in is both simpler and
    better: a box can reach a peer the instant both are up, with no lease to
    wait for.

    /24 is assumed, matching what `vmorch net create` hands out.

    Written whole every time and deleted when nothing is attached, so detaching
    a net actually removes its configuration instead of leaving an interface
    the guest still tries to bring up.
    """
    if not attachments:
        return run(name, f"rm -f {NETS_NETPLAN}; netplan generate || true",
                   check=False)

    stanzas = []
    for net_name, mac, address in attachments:
        stanzas += [
            f"    {net_name}:",
            "      match:",
            f'        macaddress: "{mac}"',
            f"      addresses: [{address}/24]",
            "      dhcp4: false",
            "      dhcp6: false",
        ]
        # The default route goes on the first net that has one, and only for a
        # box with no NAT NIC of its own -- two default routes would race, and
        # the box's own internet grant must always win.
        if gateway and gateway.rsplit(".", 1)[0] == address.rsplit(".", 1)[0]:
            stanzas += [
                "      routes:",
                "        - to: default",
                f"          via: {gateway}",
            ]
            if resolver:
                stanzas += ["      nameservers:",
                            f"        addresses: [{resolver}]"]
            gateway = None
    body = "\n".join(stanzas)

    return run(name, f"""set -e
cat > {NETS_NETPLAN} <<'YAML'
# Written by vmorch. One stanza per local network this box is attached to.
# Static because a local net has no DHCP server by design -- it has no host
# address at all. Matched by MAC, since interface names follow PCI order.
network:
  version: 2
  ethernets:
{body}
YAML
chmod 600 {NETS_NETPLAN}
netplan generate
echo "wrote {NETS_NETPLAN}"
""")


def has_wan_config(name: str) -> bool:
    """True if the guest already has config for a second NIC.

    Checks cloud-init's file as well as ours: a box created with internet from
    the start was configured at first boot and needs nothing.
    """
    out = run(name, "cat /etc/netplan/*.yaml 2>/dev/null || true", check=False)
    return "wan:" in out


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
    # The fstab line is REWRITTEN, not merely added when absent. Skipping an
    # existing line meant a mode change never reached the guest: flipping a
    # folder from ro to rw in the spec and running `vmorch apply` reported success
    # and left the old options in place, both in fstab and on the live mount.
    # A remount follows for the same reason.
    run(name, f"""set -e
mkdir -p {mountpoint}
sed -i '\\| {mountpoint} virtiofs |d' /etc/fstab
echo '{folder.tag} {mountpoint} virtiofs defaults,{opts},nofail 0 0' >> /etc/fstab
if mountpoint -q {mountpoint}; then
    mount -o remount,{opts} {mountpoint} 2>/dev/null || {{
        umount {mountpoint} && mount {mountpoint}
    }}
else
    mount {mountpoint}
fi
""")


def unmount_folder(name: str, tag: str) -> None:
    mountpoint = f"/mnt/{tag}"
    run(name, f"""set -e
mountpoint -q {mountpoint} && umount {mountpoint} || true
sed -i '\\| {mountpoint} virtiofs |d' /etc/fstab
rmdir {mountpoint} 2>/dev/null || true
""")
