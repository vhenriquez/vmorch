"""The management network and the nwfilter rules that enforce isolation.

Everything here is enforced host-side, which is the property that makes the
grants meaningful: an agent with root inside a box cannot undo any of it.

Two networks are in play:

  nic0  management  -- an isolated libvirt network (no <forward>). Always
                       present. This is what `ssh <name>` uses, so it must not
                       depend on the internet grant.
  nic1  internet    -- the NAT network, attached only when internet = true.

Filters:

  vmorch-mgmt-filter  blocks guest->host services on the bridge, and guest->guest
  vmorch-wan-lan      permits everything outbound (lan = true)
  vmorch-wan-nolan    drops RFC1918 + link-local, so "internet" means the public
                      internet only (lan = false, the default)
"""

from __future__ import annotations

import re

from . import config, virsh

MGMT_NET_XML = f"""<network>
  <name>{config.MGMT_NET}</name>
  <bridge name='{config.MGMT_BRIDGE}' stp='on' delay='0'/>
  <!-- No <forward> element: this network is isolated. Host<->guest only,
       no routing outward. SSH lives here so it survives internet = false. -->
  <ip address='{config.MGMT_GATEWAY}' netmask='{config.MGMT_NETMASK}'>
    <dhcp>
      <range start='{config.MGMT_DHCP_START}' end='{config.MGMT_DHCP_END}'/>
    </dhcp>
  </ip>
</network>
"""


def nat_gateway() -> str:
    """The NAT network's gateway address, read from libvirt.

    Discovered rather than hardcoded: this is where an internet-enabled box's
    DNS server lives, and it must be carved out of the RFC1918 drop by its real
    address, not an assumed one.
    """
    xml = virsh.run("net-dumpxml", config.NAT_NET)
    match = re.search(r"<ip address='([^']+)'", xml)
    if not match:
        raise RuntimeError(f"no <ip> in network {config.NAT_NET}")
    return match.group(1)


def _wan_filter_xml(allow_lan: bool) -> str:
    """Filter for the internet NIC.

    With lan = false we drop RFC1918 and link-local destinations, so the box
    reaches the public internet but not the router, the NAS, or any other
    machine.

    Routing is unaffected by this: internet-bound packets carry *public*
    destination IPs and never match these rules. Only directly-addressed
    gateway traffic does -- which is why DHCP and DNS need the carve-out below,
    and why that carve-out is emitted from this same template rather than being
    left to anyone's memory.
    """
    name = "vmorch-wan-lan" if allow_lan else "vmorch-wan-nolan"

    if allow_lan:
        rules = "  <!-- lan = true: no egress restrictions beyond clean-traffic -->"
    else:
        # Priorities are explicit. The accepts must outrank the drops; relying
        # on document order here would be a latent ordering bug.
        # The DNS carve-out must name the NAT gateway, NOT the management
        # gateway. An internet-enabled box gets its resolver from the NAT
        # network's dnsmasq, and that address is itself RFC1918 -- so without
        # this exact exception the box has working internet and resolves
        # nothing. Verified: the isolated management network's dnsmasq does not
        # forward upstream, so pointing guests there instead would not work.
        gw = nat_gateway()
        carve_outs = [
            "  <!-- Carve-outs FIRST (priority 100): without these the box has",
            "       'internet' but cannot get an address or resolve a name. -->",
            "  <rule action='accept' direction='out' priority='100'>",
            "    <udp dstportstart='67' dstportend='68'/>",
            "  </rule>",
            "  <rule action='accept' direction='out' priority='100'>",
            f"    <udp dstipaddr='{gw}' dstportstart='53'/>",
            "  </rule>",
            "  <rule action='accept' direction='out' priority='100'>",
            f"    <tcp dstipaddr='{gw}' dstportstart='53'/>",
            "  </rule>",
        ]
        # Per-protocol, NOT <all>. Two findings forced this:
        #
        #   <ip> blocks TCP but lets ICMP through, so the box could still ping
        #   the router -- a rule that stops connections but not probes is not
        #   isolation.
        #
        #   <all> catches ICMP but is implemented in EBTABLES, which has no
        #   connection tracking. Mixing it with a stateful accept silently loses
        #   the state match, because the two live in different layers.
        #
        # tcp/udp/icmp all land in iptables, where `state` works and every
        # protocol is covered.
        drops = ["  <!-- Then drop private space (priority 200) -->"]
        for cidr in config.PRIVATE_RANGES:
            addr, prefix = cidr.split("/")
            for proto in ("tcp", "udp", "icmp"):
                drops += [
                    "  <rule action='drop' direction='out' priority='200'>",
                    f"    <{proto} dstipaddr='{addr}' dstipmask='{prefix}'/>",
                    "  </rule>",
                ]
        rules = "\n".join(carve_outs + drops)

    return f"""<filter name='{name}' chain='root'>
  <filterref filter='clean-traffic'/>
{rules}
</filter>
"""


# Guest->host is blocked by default; a shared service is the controlled
# exception, punched in per box by services.py. Guest->guest is blocked
# outright: boxes are single-agent and have no reason to talk to each other.
MGMT_FILTER_XML = f"""<filter name='vmorch-mgmt-filter' chain='root'>
  <filterref filter='clean-traffic'/>

  <!-- Allow DHCP, or the box never gets an address. -->
  <rule action='accept' direction='out' priority='100'>
    <udp dstportstart='67' dstportend='68'/>
  </rule>

  <!-- Replies to connections the HOST opened, ssh above all.
       Without this the guest->host drop below also severs the management
       session: the guest's SSH replies are addressed to the host and match the
       drop. Locked us out of a running box twice.

       These MUST stay in the same layer as the drops below. `state` is an
       iptables feature; an <all> drop is an ebtables rule and never consults
       conntrack, so pairing the two silently drops established traffic. That
       is why everything here is tcp/udp/icmp rather than <all>. -->
  <rule action='accept' direction='out' priority='150'>
    <tcp state='ESTABLISHED'/>
  </rule>
  <rule action='accept' direction='out' priority='150'>
    <udp state='ESTABLISHED'/>
  </rule>
  <rule action='accept' direction='out' priority='150'>
    <icmp state='ESTABLISHED,RELATED'/>
  </rule>

  <!-- Block guest-INITIATED traffic to the host, and to any other box.
       Per-box service grants are inserted above this priority. -->
  <rule action='drop' direction='out' priority='500'>
    <tcp dstipaddr='{config.MGMT_SUBNET.split('/')[0]}' dstipmask='24'/>
  </rule>
  <rule action='drop' direction='out' priority='500'>
    <udp dstipaddr='{config.MGMT_SUBNET.split('/')[0]}' dstipmask='24'/>
  </rule>
  <rule action='drop' direction='out' priority='500'>
    <icmp dstipaddr='{config.MGMT_SUBNET.split('/')[0]}' dstipmask='24'/>
  </rule>
</filter>
"""


def ensure_mgmt_network() -> bool:
    """Define and start the management network. Returns True if created."""
    created = False
    if not virsh.network_exists(config.MGMT_NET):
        virsh.define_network(MGMT_NET_XML)
        created = True

    state = virsh.run("net-info", config.MGMT_NET)
    if "Active:         no" in state:
        virsh.run("net-start", config.MGMT_NET)
    if "Autostart:      no" in state:
        virsh.run("net-autostart", config.MGMT_NET)
    return created


def _filter_uuid(name: str) -> str | None:
    """The UUID libvirt already has for this filter, if any."""
    try:
        xml = virsh.run("nwfilter-dumpxml", name)
    except virsh.VirshError:
        return None
    match = re.search(r"<uuid>([^<]+)</uuid>", xml)
    return match.group(1) if match else None


def _define_filter(xml: str) -> None:
    """Define or update a filter.

    libvirt refuses to redefine an existing filter unless the XML carries its
    current UUID, so we splice it in. Without this, ensure_filters() works
    exactly once and fails on every subsequent run -- and rule changes could
    never be rolled out to an existing host.
    """
    name = re.search(r"<filter name='([^']+)'", xml).group(1)
    uuid = _filter_uuid(name)
    if uuid and "<uuid>" not in xml:
        xml = xml.replace(">", f">\n  <uuid>{uuid}</uuid>", 1)
    virsh.define_nwfilter(xml)


def ensure_filters() -> None:
    _define_filter(MGMT_FILTER_XML)
    _define_filter(_wan_filter_xml(allow_lan=True))
    _define_filter(_wan_filter_xml(allow_lan=False))


def reserve_address(name: str, mac: str, ip: str) -> None:
    """Pin a box's IP by MAC, so the name->IP mapping survives rebuilds."""
    host_xml = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
    try:
        virsh.run(
            "net-update", config.MGMT_NET, "add", "ip-dhcp-host",
            host_xml, "--live", "--config", "--parent-index", "0",
        )
    except virsh.VirshError as exc:
        # Already reserved is success, not failure: reservations outlive the
        # boxes that use them (allocations are never recycled), so re-creating
        # a box under an old name must not trip over its own reservation.
        benign = ("already exists", "existing dhcp host entry")
        if not any(msg in exc.stderr for msg in benign):
            raise


def ensure_base() -> bool:
    """Bring up everything a box needs before it can be defined."""
    created = ensure_mgmt_network()
    ensure_filters()
    return created
