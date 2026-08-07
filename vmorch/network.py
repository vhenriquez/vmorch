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

# xmlns:dnsmasq lets us pass options straight to the dnsmasq libvirt runs for
# this network. log-queries records every lookup with the client's address,
# which is the audit trail for what a box tried to reach -- by name, before it
# became an IP that means nothing six months later.
MGMT_NET_XML = f"""<network xmlns:dnsmasq='http://libvirt.org/schemas/network/dnsmasq/1.0'>
  <name>{config.MGMT_NET}</name>
  <bridge name='{config.MGMT_BRIDGE}' stp='on' delay='0'/>
  <!-- No <forward> element: this network is isolated. Host<->guest only,
       no routing outward. SSH lives here so it survives internet = false. -->
  <ip address='{config.MGMT_GATEWAY}' netmask='{config.MGMT_NETMASK}'>
    <dhcp>
      <range start='{config.MGMT_DHCP_START}' end='{config.MGMT_DHCP_END}'/>
    </dhcp>
  </ip>
  <dnsmasq:options>
    <dnsmasq:option value='log-queries'/>
  </dnsmasq:options>
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


def nat_bridge() -> str:
    """The NAT network's bridge interface, read from libvirt.

    virbr0 is only libvirt's usual name for it, not a guarantee: a host that
    already had a virbr0 when the default network was defined gets virbr1, and
    a rebuilt default network can land anywhere. The audit rules match on this
    name, and a wrong one does not error -- it just logs nothing, which is the
    worst way for an audit trail to fail.
    """
    xml = virsh.run("net-dumpxml", config.NAT_NET)
    match = re.search(r"<bridge name='([^']+)'", xml)
    if not match:
        raise RuntimeError(f"no <bridge> in network {config.NAT_NET}")
    return match.group(1)


def _wan_filter_xml(allow_lan: bool, gw: str | None = None) -> str:
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
    # Discovered by default, injectable for tests. Reading it from libvirt is
    # right at runtime and wrong in a unit test, which then needs a working
    # libvirt to check the shape of a string.
    gw = gw or nat_gateway()

    # DNS lockdown, on BOTH filters. Without it the query log is worth little:
    # a box can talk to 8.8.8.8:53 directly, or DNS-over-TLS on 853, and never
    # touch the resolver whose queries we record. Anything but our resolver is
    # dropped, so the log is complete for plain DNS and DoT.
    #
    # DoH is not covered -- it is HTTPS to 443 and indistinguishable from any
    # other web traffic without blocking provider addresses outright. The
    # connection log is what catches that.
    dns_lock = [
        "  <!-- DNS only via our resolver, so the query log has no holes -->",
        "  <rule action='accept' direction='out' priority='100'>",
        f"    <udp dstipaddr='{gw}' dstportstart='53' dstportend='53'/>",
        "  </rule>",
        "  <rule action='accept' direction='out' priority='100'>",
        f"    <tcp dstipaddr='{gw}' dstportstart='53' dstportend='53'/>",
        "  </rule>",
        "  <rule action='drop' direction='out' priority='300'>",
        "    <udp dstportstart='53' dstportend='53'/>",
        "  </rule>",
        "  <rule action='drop' direction='out' priority='300'>",
        "    <tcp dstportstart='53' dstportend='53'/>",
        "  </rule>",
        "  <!-- DNS-over-TLS -->",
        "  <rule action='drop' direction='out' priority='300'>",
        "    <tcp dstportstart='853' dstportend='853'/>",
        "  </rule>",
    ]

    if allow_lan:
        rules = "\n".join(
            ["  <!-- lan = true: no egress restrictions beyond DNS lockdown -->"]
            + dns_lock)
    else:
        # Priorities are explicit. The accepts must outrank the drops; relying
        # on document order here would be a latent ordering bug.
        # The DNS carve-out must name the NAT gateway, NOT the management
        # gateway. An internet-enabled box gets its resolver from the NAT
        # network's dnsmasq, and that address is itself RFC1918 -- so without
        # this exact exception the box has working internet and resolves
        # nothing. Verified: the isolated management network's dnsmasq does not
        # forward upstream, so pointing guests there instead would not work.
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
        rules = "\n".join(dns_lock + carve_outs + drops)

    return f"""<filter name='{name}' chain='root'>
  <filterref filter='clean-traffic'/>
{rules}
</filter>
"""


_mgmt_addr, _mgmt_prefix = config.MGMT_SUBNET.split("/")

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
       Per-box service grants are inserted above this priority.

       The mask comes from MGMT_SUBNET rather than a literal 24: that value is
       configurable, and a hardcoded mask on a /16 management network would
       leave three quarters of it reachable. -->
  <rule action='drop' direction='out' priority='500'>
    <tcp dstipaddr='{_mgmt_addr}' dstipmask='{_mgmt_prefix}'/>
  </rule>
  <rule action='drop' direction='out' priority='500'>
    <udp dstipaddr='{_mgmt_addr}' dstipmask='{_mgmt_prefix}'/>
  </rule>
  <rule action='drop' direction='out' priority='500'>
    <icmp dstipaddr='{_mgmt_addr}' dstipmask='{_mgmt_prefix}'/>
  </rule>
</filter>
"""


class NetworkMismatch(RuntimeError):
    """The live management network does not match the configured subnet."""


def _live_mgmt() -> tuple[str, str] | None:
    """(subnet in CIDR, gateway) the management network actually serves.

    libvirt stores the *gateway* address and a dotted netmask; the config wants
    a CIDR network. Reporting the raw pair back to the user produced advice like
    `mgmt_subnet = "192.168.150.1/255.255.255.0"`, which is neither.
    """
    import ipaddress
    try:
        xml = virsh.run("net-dumpxml", config.MGMT_NET)
    except virsh.VirshError:
        return None
    ip = re.search(r"<ip address='([^']+)' netmask='([^']+)'", xml)
    if not ip:
        return None
    gateway, netmask = ip.group(1), ip.group(2)
    try:
        net = ipaddress.IPv4Network(f"{gateway}/{netmask}", strict=False)
    except ValueError:
        return None
    return str(net), gateway


def _live_mgmt_subnet() -> str | None:
    live = _live_mgmt()
    return live[0] if live else None


def check_mgmt_subnet() -> None:
    """Refuse to work against a network serving a different subnet.

    `mgmt_subnet` is only read when the network is *created*. Change it
    afterwards and this function is the only thing standing between you and a
    box that is allocated an address nothing on the wire will ever hand out.

    Nothing else notices. libvirt accepts a `<host ip=...>` reservation outside
    its own DHCP range **without an error**, dnsmasq then ignores it, and the
    guest takes a random in-range lease instead. So `vmorch new` reports an
    address, writes it into the ssh config, and the box is up and reachable at a
    completely different one -- `ssh <box>` times out with nothing anywhere
    saying why. Observed exactly that: a box reported at 10.150.0.50 was actually
    living on 192.168.150.134.

    Deliberately refuses rather than redefining. Every existing box holds an
    address on the old subnet, in the allocation ledger and in the ssh config;
    silently moving the network would strand all of them at once.
    """
    live = _live_mgmt()
    if live is None or live[0] == config.MGMT_SUBNET:
        return
    subnet, gateway = live

    raise NetworkMismatch(
        f"the {config.MGMT_NET} network is serving {subnet}, but mgmt_subnet is "
        f"{config.MGMT_SUBNET}.\n"
        "\n"
        "  A subnet is only read when the network is first created, so changing\n"
        "  it later leaves the live network where it was. Boxes would be given\n"
        "  addresses on the new subnet that dnsmasq never hands out -- they come\n"
        "  up on a random old-subnet lease instead and `ssh <box>` times out.\n"
        "\n"
        "  Keep what you have (nothing is disturbed):\n"
        f"    mgmt_subnet  = \"{subnet}\"\n"
        f"    mgmt_gateway = \"{gateway}\"\n"
        f"  in {config.CONFIG_FILE}\n"
        "\n"
        f"  Or move to {config.MGMT_SUBNET}, which means starting over on the\n"
        "  address plan -- destroy every box first, then:\n"
        f"    virsh -c {config.LIBVIRT_URI} net-destroy {config.MGMT_NET}\n"
        f"    virsh -c {config.LIBVIRT_URI} net-undefine {config.MGMT_NET}\n"
        "    vmorch net"
    )


def ensure_mgmt_network() -> bool:
    """Define and start the management network. Returns True if created."""
    created = False
    if not virsh.network_exists(config.MGMT_NET):
        virsh.define_network(MGMT_NET_XML)
        created = True
    else:
        # Before anything is allocated or started against it.
        check_mgmt_subnet()

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


def arm_filters() -> None:
    """Define the filters immediately before a domain starts.

    **Load-bearing. Without this a box is not filtered when it boots.**

    Measured 2026-08-05: a box started without a filter definition just before
    it reached the LAN router and the host's LAN address with 0% loss, despite
    `lan = false` -- for as long as it was left alone, over two minutes. The
    filter was defined, correct, and bound to the port the whole time
    (`nwfilter-binding-list` showed it 1.4s after create returned); libvirt had
    simply not put the rules in place. Redefining the filter *before* the start
    fixes it: sampled from t+8s onwards, every start is filtered.

    Four variants were tested to find the cause -- as shipped, without
    `clean-traffic`, and with `CTRL_IP_LEARNING` pinned to `none` and to `dhcp`.
    All four behaved identically, which rules out the IP-learning theory: what
    matters is only that a define happens between the previous state and the
    start.

    So this is called from every path that starts a domain, and called *before*
    `virsh start`, not after. Calling it after was tried first and does nothing
    -- by then the unfiltered port already exists.

    `create()` alone was not enough even though it calls ensure_filters(): that
    happens near the top, before the image copy, the overlay and the seed build,
    and whatever libvirt needs is evidently not still in place by the time the
    domain actually starts.
    """
    ensure_filters()


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


def enable_dns_logging() -> list[str]:
    """Turn on dnsmasq query logging for both networks.

    Returns the networks that had to be restarted. libvirt regenerates each
    dnsmasq config from the network XML at start, so the option only takes
    effect on restart -- and restarting a network detaches every guest attached
    to it. Callers must warn before doing this.
    """
    restarted = []
    for net in (config.MGMT_NET, config.NAT_NET):
        xml = virsh.run("net-dumpxml", "--inactive", net)
        if "log-queries" in xml:
            continue
        xml = re.sub(r"<network(\s[^>]*)?>",
                     "<network xmlns:dnsmasq="
                     "'http://libvirt.org/schemas/network/dnsmasq/1.0'>",
                     xml, count=1)
        xml = xml.replace("</network>", """  <dnsmasq:options>
    <dnsmasq:option value='log-queries'/>
  </dnsmasq:options>
</network>""")
        virsh.define_network(xml)
        virsh.run("net-destroy", net, check=False)
        virsh.run("net-start", net)
        restarted.append(net)
    return restarted


def dns_logging_enabled() -> bool:
    try:
        return all("log-queries" in virsh.run("net-dumpxml", "--inactive", n)
                   for n in (config.MGMT_NET, config.NAT_NET))
    except virsh.VirshError:
        return False


def unreserve_address(name: str, mac: str, ip: str) -> None:
    """Drop a box's DHCP reservation.

    Without this the reservation outlives the box, so the network accumulates
    entries for boxes that no longer exist and the address cannot be handed to
    anything else -- dnsmasq would offer it to a MAC that will never ask again.
    """
    host_xml = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
    try:
        virsh.run("net-update", config.MGMT_NET, "delete", "ip-dhcp-host",
                  host_xml, "--live", "--config", "--parent-index", "0")
    except virsh.VirshError as exc:
        if "no matching" not in exc.stderr.lower():
            raise


def prune_reservations(live_names: set[str]) -> list[str]:
    """Remove reservations for boxes that no longer exist. Returns what went."""
    import re as _re
    xml = virsh.run("net-dumpxml", "--inactive", config.MGMT_NET)
    removed = []
    for mac, name, ip in _re.findall(
            r"<host mac='([^']+)' name='([^']+)' ip='([^']+)'/>", xml):
        if name not in live_names:
            unreserve_address(name, mac, ip)
            removed.append(name)
    return removed


def ensure_base() -> bool:
    """Bring up everything a box needs before it can be defined."""
    created = ensure_mgmt_network()
    ensure_filters()
    return created
