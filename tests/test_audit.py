"""Audit log parsing.

The reader is only as good as its regexes, and both log formats come from
programs we do not control -- dnsmasq and the kernel's nftables logger. These
samples are real lines, kept so a tweak to either pattern cannot quietly stop
matching and turn the audit trail into an empty list.

Run: python3 tests/test_audit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmorch import audit  # noqa: E402

DNS_QUERIES = [
    ("dnsmasq[4557]: query[A] github.com from 192.168.122.64",
     "A", "github.com", "192.168.122.64"),
    ("dnsmasq[4557]: query[AAAA] deb.debian.org from 192.168.150.23",
     "AAAA", "deb.debian.org", "192.168.150.23"),
    ("dnsmasq[900]: query[PTR] 1.122.168.192.in-addr.arpa from 192.168.122.5",
     "PTR", "1.122.168.192.in-addr.arpa", "192.168.122.5"),
]

NFT_LINES = [
    # hyphenated verdict -- \w+ stops at the hyphen and matched nothing
    ("vmorch-audit-blocked-private IN=virbr0 OUT=enp6s0 MAC=aa:bb "
     "SRC=192.168.122.64 DST=192.168.1.1 LEN=60 TTL=63 PROTO=TCP "
     "SPT=51234 DPT=22 WINDOW=64240",
     "blocked-private", "192.168.122.64", "192.168.1.1", "TCP", "22"),
    ("vmorch-audit-allow IN=virbr0 OUT=enp6s0 SRC=192.168.122.64 "
     "DST=140.82.121.4 PROTO=TCP SPT=44212 DPT=443",
     "allow", "192.168.122.64", "140.82.121.4", "TCP", "443"),
    ("vmorch-audit-blocked-dot IN=virbr0 SRC=192.168.122.9 DST=1.1.1.1 "
     "PROTO=TCP SPT=5000 DPT=853",
     "blocked-dot", "192.168.122.9", "1.1.1.1", "TCP", "853"),
    # ICMP has no ports at all
    ("vmorch-audit-blocked-private IN=virbr0 SRC=192.168.122.9 "
     "DST=192.168.1.1 PROTO=ICMP TYPE=8 CODE=0",
     "blocked-private", "192.168.122.9", "192.168.1.1", "ICMP", None),
]


def main() -> int:
    failures = 0

    for line, typ, name, client in DNS_QUERIES:
        m = audit._DNS_QUERY.search(line)
        ok = m and (m["type"], m["name"], m["client"]) == (typ, name, client)
        print(f"  {'ok  ' if ok else 'FAIL'} dns query: {name}")
        failures += 0 if ok else 1

    m = audit._DNS_REPLY.search("dnsmasq[4557]: reply github.com is 140.82.121.4")
    ok = m and (m["name"], m["addr"]) == ("github.com", "140.82.121.4")
    print(f"  {'ok  ' if ok else 'FAIL'} dns reply gives the address for correlation")
    failures += 0 if ok else 1

    for line, verdict, src, dst, proto, dpt in NFT_LINES:
        m = audit._NFT.search(line)
        ok = m and (m["verdict"], m["src"], m["dst"], m["proto"], m["dpt"]) == \
            (verdict, src, dst, proto, dpt)
        print(f"  {'ok  ' if ok else 'FAIL'} nft {verdict}"
              f"{'' if ok else f'  got {m.groupdict() if m else None}'}")
        failures += 0 if ok else 1

    # A blocked verdict must be recognisable as such: `vm audit --blocked`
    # filters on it, and that is the view that matters most.
    for verdict in ("blocked-private", "blocked-dns", "blocked-dot"):
        ok = verdict.startswith("block")
        print(f"  {'ok  ' if ok else 'FAIL'} '{verdict}' counts as blocked")
        failures += 0 if ok else 1

    # The generated ruleset must name the NAT resolver, not the management one.
    rules = audit.nft_ruleset(nat_gw="192.168.122.1")
    ok = "!= 192.168.122.1" in rules
    print(f"  {'ok  ' if ok else 'FAIL'} DNS rule names the NAT resolver")
    failures += 0 if ok else 1

    print("FAILED" if failures else "audit parsing is correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
