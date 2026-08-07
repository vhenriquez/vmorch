"""Local networks: private segments boxes can talk to each other on.

Everything else in vmorch works to keep boxes apart. This is the one deliberate
exception, so it is opt-in, named, and narrow.

**A local net is members-only.** The libvirt network carries no `<forward>` and,
unlike the management network, **no `<ip>` either** -- so the host has no address
on the bridge, there is no dnsmasq, and there is nowhere for a packet to go
except another member. That is why there is no "block the gateway" rule here:
there is no gateway to block.

No DHCP follows from that, which is a feature rather than a gap. Addresses are
written straight into each guest's netplan, so a box knows its peers' addresses
before either has booted, and there is no lease to wait for or race with.

    Management NIC   isolated, port-isolated, always present -> ssh
    Internet NIC     NAT, port-isolated, only if internet = true
    Local net NIC    one per attached net, deliberately NOT port-isolated

`<port isolated='yes'/>` is precisely what stops box-to-box traffic elsewhere, so
a local net is the considered opt-out of it -- on its own NIC, leaving the other
two untouched. Joining a net can never weaken what a box already had.

**Addresses are deterministic.** A box's host octet on every local net is the
same as its management octet, so `dev` at 10.150.0.33 is .33 on every net it
joins. No second allocator, no collisions -- the management allocation is already
unique per box -- and an address you can work out rather than look up.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

from . import alloc, config, virsh

#: Bridge names are capped at IFNAMSIZ (15). "virbr-" leaves nine characters,
#: and a name that overflows produces a network libvirt accepts and an interface
#: it cannot create -- so the limit is enforced when the net is named.
NAME_MAX = 9
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

#: Middle MAC byte for a local-net NIC: 0x01 is management, 0x02 is internet.
NET_MAC_BASE = 0x10
#: Highest usable index, so NET_MAC_BASE + index still fits in one byte.
MAX_NET_INDEX = 0xFF - NET_MAC_BASE


def pool_capacity(pool: str = "") -> int:
    """How many /24s the configured pool actually holds.

    The prefix length used to be ignored entirely: a /20 holds 16 subnets, but
    the allocator counted to 255 and happily handed out addresses well outside
    the range it had been told to use -- potentially landing on the host's real
    LAN, which is the one thing choosing a pool is meant to prevent.
    """
    pool = pool or config.LOCALNET_POOL
    bits = int(pool.split("/")[1])
    return max(0, min(1 << max(0, 24 - bits), MAX_NET_INDEX + 1))


class NetError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalNet:
    name: str
    index: int          # position in the pool; fixes the subnet and the MACs
    subnet: str         # e.g. "10.150.16.0/24"

    @property
    def libvirt_name(self) -> str:
        return f"vmorch-net-{self.name}"

    @property
    def bridge(self) -> str:
        return f"virbr-{self.name}"

    @property
    def prefix(self) -> str:
        """The first three octets, e.g. "10.150.16"."""
        return self.subnet.split("/")[0].rsplit(".", 1)[0]

    def address(self, box: str) -> str:
        """This box's address on this net: its management octet, reused."""
        octet = alloc.allocate(box).ip.rsplit(".", 1)[1]
        return f"{self.prefix}.{octet}"

    def mac(self, box: str) -> str:
        """MAC for this box's NIC on this net.

        Same derivation as the management and internet MACs -- the box's octet
        in the last byte, the NIC's role in the one before. Nets start at 0x10
        so they can never collide with 0x01 (management) or 0x02 (internet),
        which caps the index at MAX_NET_INDEX: past it the middle byte needs
        three hex digits and the MAC is silently malformed rather than rejected.
        """
        octet = int(alloc.allocate(box).ip.rsplit(".", 1)[1])
        return f"52:54:00:6d:{NET_MAC_BASE + self.index:02x}:{octet:02x}"


NETS_FILE = config.STATE_DIR / "nets.toml"


def _load() -> dict[str, LocalNet]:
    if not NETS_FILE.exists():
        return {}
    with open(NETS_FILE, "rb") as fh:
        raw = tomllib.load(fh)
    out = {}
    for name, body in raw.items():
        if isinstance(body, dict) and "index" in body:
            out[name] = LocalNet(name=name, index=int(body["index"]),
                                 subnet=str(body["subnet"]))
    return out


def _save(nets: dict[str, LocalNet]) -> None:
    NETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# vmorch local networks. Created by `vm net create`.",
        "#",
        "# A local net is a members-only segment: boxes attached to the same one",
        "# can reach each other, and nothing else. Editing this file by hand does",
        "# not move a running box -- use `vm net attach` / `vm net detach`.",
        "",
    ]
    for net in sorted(nets.values(), key=lambda n: n.index):
        lines += [f"[{net.name}]",
                  f"index = {net.index}",
                  f'subnet = "{net.subnet}"',
                  ""]
    NETS_FILE.write_text("\n".join(lines))


def list_nets() -> list[LocalNet]:
    return sorted(_load().values(), key=lambda n: n.index)


def get(name: str) -> LocalNet:
    nets = _load()
    if name not in nets:
        known = ", ".join(sorted(nets)) or "none defined"
        raise NetError(f"no local network {name!r}. Known: {known}\n"
                       f"  Create one with `vm net create {name}`")
    return nets[name]


def exists(name: str) -> bool:
    return name in _load()


def _validate(name: str) -> None:
    if not _NAME_RE.match(name):
        raise NetError(
            f"invalid network name {name!r}: lower-case letters, digits and "
            "hyphens, starting with a letter.")
    if len(name) > NAME_MAX:
        raise NetError(
            f"network name {name!r} is {len(name)} characters; the limit is "
            f"{NAME_MAX}. The bridge is named virbr-{name}, and Linux caps an "
            "interface name at 15 characters -- libvirt accepts the network and "
            "then cannot create the interface.")


def _network_xml(net: LocalNet) -> str:
    """An isolated layer-2 segment. No forward, and deliberately no <ip>.

    Omitting <ip> is what makes this members-only: libvirt gives the host no
    address on the bridge and starts no dnsmasq for it, so there is no host
    service to reach and no resolver to leak queries to. The subnet exists only
    as an addressing convention that vmorch writes into each guest.
    """
    return f"""<network>
  <name>{net.libvirt_name}</name>
  <bridge name='{net.bridge}' stp='on' delay='0'/>
</network>
"""


def create(name: str, subnet: str | None = None) -> LocalNet:
    _validate(name)
    nets = _load()
    if name in nets:
        raise NetError(f"local network {name!r} already exists")

    capacity = pool_capacity()
    taken = {n.index for n in nets.values()}
    index = next((i for i in range(capacity) if i not in taken), None)
    if index is None:
        raise NetError(
            f"no free subnet left in {config.LOCALNET_POOL}: it holds "
            f"{capacity} network(s) and all are in use. Widen localnet_pool in "
            f"{config.CONFIG_FILE}, or remove a network with `vm net rm`.")
    if subnet is None:
        base = config.LOCALNET_POOL.split("/")[0].rsplit(".", 2)[0]
        third = int(config.LOCALNET_POOL.split("/")[0].rsplit(".", 2)[1]) + index
        subnet = f"{base}.{third}.0/24"

    net = LocalNet(name=name, index=index, subnet=subnet)
    ensure(net)
    nets[name] = net
    _save(nets)
    return net


def ensure(net: LocalNet) -> None:
    """Define and start the libvirt network, idempotently."""
    xml = _network_xml(net)
    try:
        existing = virsh.run("net-dumpxml", net.libvirt_name)
        uuid = re.search(r"<uuid>([^<]+)</uuid>", existing)
        if uuid:
            xml = xml.replace(">", f">\n  <uuid>{uuid.group(1)}</uuid>", 1)
    except virsh.VirshError:
        pass
    virsh.define_network(xml)
    virsh.run("net-autostart", net.libvirt_name, check=False)
    try:
        if "Active:         yes" not in virsh.run("net-info", net.libvirt_name):
            virsh.run("net-start", net.libvirt_name)
    except virsh.VirshError:
        virsh.run("net-start", net.libvirt_name, check=False)


def remove(name: str, attached: list[str]) -> None:
    """Delete a local network. Refuses while boxes are still attached.

    The caller passes the attached boxes rather than this module importing
    boxes.py, which imports this one.
    """
    net = get(name)
    if attached:
        raise NetError(
            f"{name} still has {len(attached)} box(es) attached: "
            f"{', '.join(attached)}\n"
            f"  Detach them first: vm net detach {attached[0]} {name}")
    virsh.run("net-destroy", net.libvirt_name, check=False)
    virsh.run("net-undefine", net.libvirt_name, check=False)
    nets = _load()
    nets.pop(name, None)
    _save(nets)


def box_filter_name(net: str, box: str) -> str:
    return f"vmorch-net-{net}-{box}"


def box_filter_xml(net: LocalNet, box: str, router: bool = False) -> str:
    """Per-box filter for one local net NIC.

    Members of a net can reach each other -- that is the point -- so the thing
    worth preventing is one member *pretending to be another*. `clean-traffic`
    gives MAC, IP and ARP anti-spoofing, but by default it *learns* the guest's
    address, and a learned address is one the guest chose. Here the address is
    known before the box boots, so it is pinned and learning is turned off.

    There are no drop rules and none are needed: the segment has no gateway and
    no host address, so a packet cannot leave it whatever the guest addresses it
    to. Rules that cannot fire are rules that mislead the next reader.

    **A router is the one exception, and it has to be.** Forwarding means
    emitting packets whose source address belongs to somebody else -- a reply
    from the internet, relayed onto the segment, carries the *remote* address,
    not the router's. That is indistinguishable from spoofing, so the pin drops
    it and the return path dies while the outbound half works perfectly. Measured
    2026-08-05: with the pin, ping through a gateway box was 100% loss; with the
    pin removed from that one box's filter, 0% loss and HTTP 301, and every other
    member stayed pinned and still could not spoof.

    Only the IP pin goes. MAC and ARP anti-spoofing stay on even for a router,
    so it still cannot claim another box's identity on the wire.
    """
    if router:
        pin = ("  <!-- No IP pin: this box forwards for others, and a forwarded\n"
               "       packet carries somebody else's source address. MAC and\n"
               "       ARP anti-spoofing still apply. -->")
        params = "    <parameter name='CTRL_IP_LEARNING' value='none'/>"
    else:
        pin = ("  <!-- Pinned, not learned: the guest never asserts its own\n"
               "       address. -->")
        params = (f"    <parameter name='IP' value='{net.address(box)}'/>\n"
                  "    <parameter name='CTRL_IP_LEARNING' value='none'/>")

    return f"""<filter name='{box_filter_name(net.name, box)}' chain='root'>
{pin}
  <filterref filter='clean-traffic'>
{params}
  </filterref>
</filter>
"""


def ensure_box_filter(net: LocalNet, box: str, router: bool = False) -> str:
    xml = box_filter_xml(net, box, router=router)
    name = box_filter_name(net.name, box)
    try:
        existing = virsh.run("nwfilter-dumpxml", name)
        uuid = re.search(r"<uuid>([^<]+)</uuid>", existing)
        if uuid:
            xml = xml.replace(">", f">\n  <uuid>{uuid.group(1)}</uuid>", 1)
    except virsh.VirshError:
        pass
    virsh.define_nwfilter(xml)
    return name


def delete_box_filter(net_name: str, box: str) -> None:
    try:
        virsh.run("nwfilter-undefine", box_filter_name(net_name, box))
    except virsh.VirshError:
        pass
