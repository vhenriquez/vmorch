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
        carve_outs = [
            "  <!-- Carve-outs FIRST (priority 100): without these the box has",
            "       'internet' but cannot get an address or resolve a name. -->",
            "  <rule action='accept' direction='out' priority='100'>",
            "    <udp dstportstart='67' dstportend='68'/>",
            "  </rule>",
            "  <rule action='accept' direction='out' priority='100'>",
            f"    <udp dstipaddr='{config.MGMT_GATEWAY}' dstportstart='53'/>",
            "  </rule>",
            "  <rule action='accept' direction='out' priority='100'>",
            f"    <tcp dstipaddr='{config.MGMT_GATEWAY}' dstportstart='53'/>",
            "  </rule>",
        ]
        drops = ["  <!-- Then drop private space (priority 200) -->"]
        for cidr in config.PRIVATE_RANGES:
            addr, prefix = cidr.split("/")
            drops += [
                "  <rule action='drop' direction='out' priority='200'>",
                f"    <ip dstipaddr='{addr}' dstipmask='{prefix}'/>",
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

  <!-- Allow DHCP and DNS to the bridge, or the box never gets an address. -->
  <rule action='accept' direction='out' priority='100'>
    <udp dstportstart='67' dstportend='68'/>
  </rule>
  <rule action='accept' direction='out' priority='100'>
    <udp dstipaddr='{config.MGMT_GATEWAY}' dstportstart='53'/>
  </rule>

  <!-- Block everything else aimed at the host itself. Service grants are
       added per box at higher priority than this. -->
  <rule action='drop' direction='out' priority='500'>
    <ip dstipaddr='{config.MGMT_GATEWAY}' dstipmask='32'/>
  </rule>

  <!-- Boxes cannot reach each other. Belt and braces alongside the
       <port isolated='yes'/> on the interface itself. -->
  <rule action='drop' direction='out' priority='500'>
    <ip dstipaddr='{config.MGMT_SUBNET.split('/')[0]}' dstipmask='24'/>
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


def ensure_filters() -> None:
    virsh.define_nwfilter(MGMT_FILTER_XML)
    virsh.define_nwfilter(_wan_filter_xml(allow_lan=True))
    virsh.define_nwfilter(_wan_filter_xml(allow_lan=False))


def reserve_address(name: str, mac: str, ip: str) -> None:
    """Pin a box's IP by MAC, so the name->IP mapping survives rebuilds."""
    host_xml = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
    try:
        virsh.run(
            "net-update", config.MGMT_NET, "add", "ip-dhcp-host",
            host_xml, "--live", "--config", "--parent-index", "0",
        )
    except virsh.VirshError as exc:
        # Already reserved is success, not failure: ensure_* is idempotent.
        if "already exists" not in exc.stderr:
            raise


def ensure_base() -> bool:
    """Bring up everything a box needs before it can be defined."""
    created = ensure_mgmt_network()
    ensure_filters()
    return created
