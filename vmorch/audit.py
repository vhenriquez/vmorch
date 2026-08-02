"""Read the host-side audit trail: what each box looked up, and what it reached.

Two independent streams, both written by the host, neither reachable from inside
a box. An agent with root in a guest cannot edit or suppress either -- which is
the only reason they are worth keeping.

  DNS queries      dnsmasq `log-queries` on the libvirt networks, to the journal
  Connections      an nftables chain logging NEW flows and drops, to the journal

The value added here is attribution and correlation. A raw line says
192.168.122.64 reached 140.82.121.4; what an audit needs is "box `android`
looked up github.com, then connected to it". So:

  * addresses are resolved to box names, via the allocation ledger for the
    management address and the NAT lease table for the internet address -- a
    box has two, and DNS from an internet-enabled box comes from the second;

  * connections are joined back to the most recent lookup that returned that
    address, because auditing bare IPs ages badly. CDN addresses are recycled
    and mean nothing months later.

**Blocked events matter more than allowed ones here.** The design is
default-deny, so successful internet traffic is expected. A box probing the LAN,
the host, or another box is the finding.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from . import alloc, config, virsh

#: nftables log prefix. Distinct so the reader never confuses these with any
#: other kernel logging on the host.
LOG_PREFIX = "vmorch-audit"

_DNS_QUERY = re.compile(
    r"dnsmasq\[\d+\]:\s+query\[(?P<type>[A-Z0-9]+)\]\s+(?P<name>\S+)\s+from\s+(?P<client>\S+)"
)
_DNS_REPLY = re.compile(
    r"dnsmasq\[\d+\]:\s+reply\s+(?P<name>\S+)\s+is\s+(?P<addr>\S+)"
)
_NFT = re.compile(
    # [\w-]+ not \w+: verdicts are hyphenated ("blocked-private"), and \w
    # stops at the hyphen, leaving the rest of the line unmatched.
    rf"{LOG_PREFIX}-(?P<verdict>[\w-]+?)\s+.*?"
    r"SRC=(?P<src>\S+)\s+DST=(?P<dst>\S+).*?"
    r"PROTO=(?P<proto>\S+)(?:\s+SPT=(?P<spt>\d+)\s+DPT=(?P<dpt>\d+))?"
)


@dataclass
class Event:
    when: str
    box: str
    kind: str           # dns | conn
    detail: str
    verdict: str = ""
    src: str = ""
    dst: str = ""
    proto: str = ""
    hostname: str = ""


@dataclass
class BoxMap:
    """Both of a box's addresses, so either can be attributed."""
    by_ip: dict[str, str] = field(default_factory=dict)

    def name(self, ip: str) -> str:
        return self.by_ip.get(ip, ip)


def box_addresses() -> BoxMap:
    """Map every address a box may appear as back to its name.

    The management address comes from our own ledger. The internet address is a
    DHCP lease on the NAT network, matched by the MAC we derive for that NIC --
    without this, every DNS query from an internet-enabled box is unattributed,
    because the query reaches the resolver over that interface.
    """
    m = BoxMap()
    wan_macs: dict[str, str] = {}
    for a in alloc.all_allocations():
        m.by_ip[a.ip] = a.name
        wan_macs[a.wan_mac.lower()] = a.name

    try:
        leases = virsh.run("net-dhcp-leases", config.NAT_NET)
    except virsh.VirshError:
        return m
    for line in leases.splitlines():
        parts = line.split()
        if len(parts) >= 5 and ":" in parts[2]:
            mac, ip = parts[2].lower(), parts[4].split("/")[0]
            if mac in wan_macs:
                m.by_ip[ip] = wan_macs[mac]
    return m


def _journal(since: str, extra: list[str]) -> list[dict]:
    """Pull structured journal records. Empty on any failure -- a missing log
    is a reason to say so, not to crash the reader."""
    cmd = ["journalctl", "--since", since, "-o", "json", "--no-pager", *extra]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _stamp(rec: dict) -> str:
    usec = rec.get("__REALTIME_TIMESTAMP")
    if not usec:
        return "?"
    return datetime.fromtimestamp(int(usec) / 1_000_000).strftime("%Y-%m-%d %H:%M:%S")


def dns_events(since: str, boxes: BoxMap) -> tuple[list[Event], dict[str, str]]:
    """DNS queries, plus a map of answer address -> hostname for correlation."""
    events: list[Event] = []
    answers: dict[str, str] = {}
    for rec in _journal(since, ["-t", "dnsmasq"]):
        msg = rec.get("MESSAGE", "")
        q = _DNS_QUERY.search(msg)
        if q:
            events.append(Event(
                when=_stamp(rec),
                box=boxes.name(q["client"]),
                kind="dns",
                detail=f"{q['type']:<5} {q['name']}",
                hostname=q["name"],
                src=q["client"],
            ))
            continue
        r = _DNS_REPLY.search(msg)
        if r and r["addr"] not in ("<CNAME>", "NXDOMAIN"):
            answers[r["addr"]] = r["name"]
    return events, answers


def conn_events(since: str, boxes: BoxMap, answers: dict[str, str]) -> list[Event]:
    events: list[Event] = []
    for rec in _journal(since, ["-k"]):
        msg = rec.get("MESSAGE", "")
        if LOG_PREFIX not in msg:
            continue
        m = _NFT.search(msg)
        if not m:
            continue
        dst = m["dst"]
        port = f":{m['dpt']}" if m["dpt"] else ""
        events.append(Event(
            when=_stamp(rec),
            box=boxes.name(m["src"]),
            kind="conn",
            verdict=m["verdict"],
            src=f"{m['src']}:{m['spt']}" if m["spt"] else m["src"],
            dst=f"{dst}{port}",
            proto=m["proto"],
            hostname=answers.get(dst, ""),
            detail="",
        ))
    return events


def collect(since: str = "-24h", box: str | None = None,
            blocked_only: bool = False) -> list[Event]:
    boxes = box_addresses()
    dns, answers = dns_events(since, boxes)
    conns = conn_events(since, boxes, answers)

    events = sorted(dns + conns, key=lambda e: e.when)
    if box:
        events = [e for e in events if e.box == box]
    if blocked_only:
        events = [e for e in events if e.verdict.startswith("block")]
    return events


def available() -> dict[str, bool]:
    """Which streams are actually running, so the reader can say what is missing."""
    dns_on = False
    try:
        xml = virsh.run("net-dumpxml", "--inactive", config.MGMT_NET)
        dns_on = "log-queries" in xml
    except virsh.VirshError:
        pass
    conn_on = bool(_journal("-7d", ["-k", "-g", LOG_PREFIX, "-n", "1"]))
    return {"dns": dns_on, "connections": conn_on}


# --------------------------------------------------------------------------
# Tier 2: the nftables ruleset. Needs root once; everything above does not.
# --------------------------------------------------------------------------

def nft_ruleset(nat_gw: str | None = None) -> str:
    """Logging rules for the box bridges.

    A separate table at a priority ahead of libvirt's own, doing nothing but
    logging and returning, so it observes without changing what is allowed --
    libvirt's nwfilter rules still make every decision.

    Only NEW connections are logged, not every packet: one line per flow keeps
    the log an audit trail rather than a packet dump.
    """
    # DNS for an internet-enabled box goes to the NAT network's resolver, not
    # the management one. Naming the wrong gateway here would log every
    # legitimate lookup as blocked -- noise that trains you to ignore the log.
    if nat_gw is None:
        from . import network
        nat_gw = network.nat_gateway()

    return f"""#!/usr/sbin/nft -f
# vmorch connection audit. Observes only: every rule ends in `return`, so
# nothing here changes whether traffic is allowed.
#
# Reload:  sudo nft -f /etc/nftables.d/vmorch-audit.nft
# Remove:  sudo nft delete table inet vmorch_audit

table inet vmorch_audit {{
    chain observe {{
        type filter hook forward priority -150; policy accept;

        # New flows leaving a box, on either bridge.
        iifname "{config.MGMT_BRIDGE}" ct state new \\
            log prefix "{LOG_PREFIX}-allow " level info
        iifname "virbr0" ct state new \\
            log prefix "{LOG_PREFIX}-allow " level info

        # What the guest is forbidden to reach. These duplicate the nwfilter
        # decisions purely so the attempt is recorded -- in a default-deny
        # design the refused attempts are the interesting ones.
        iifname "virbr0" ct state new \\
            ip daddr {{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} \\
            log prefix "{LOG_PREFIX}-blocked-private " level info
        iifname "virbr0" udp dport 53 ip daddr != {nat_gw} \\
            log prefix "{LOG_PREFIX}-blocked-dns " level info
        iifname "virbr0" tcp dport 853 \\
            log prefix "{LOG_PREFIX}-blocked-dot " level info
    }}

    chain observe_out {{
        type filter hook input priority -150; policy accept;
        # Guest -> host itself: blocked unless a service grant allows it.
        iifname "{config.MGMT_BRIDGE}" ct state new \\
            log prefix "{LOG_PREFIX}-tohost " level info
    }}
}}
"""
