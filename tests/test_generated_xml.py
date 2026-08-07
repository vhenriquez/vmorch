"""Every document we hand libvirt must be well-formed XML.

This exists because it was not, once: XML forbids `--` inside a comment, and the
prose comments in these templates used it as an em-dash. libvirt rejected the
domain at define time with a parse error pointing at a comment, which is a
confusing way to discover a typo.

Run: python3 -m tests.test_generated_xml
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.dom.minidom import parseString

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmorch import domain, network, services, spec as spec_mod  # noqa: E402


def _spec(**kw):
    data = {"name": "sample", **kw}
    return spec_mod.parse(data)


def documents():
    plain = _spec()
    loaded = _spec(
        network={"internet": True, "lan": False},
        folders=[
            {"host": "/tmp", "tag": "tmpdir"},
            {"host": "/var/tmp", "tag": "vartmp", "mode": "rw"},
        ],
        services={"from_host": [
            {"name": "ollama", "host": 11434, "guest": 11434, "via": "filter"},
        ]},
    )

    for s in (plain, loaded):
        yield f"domain[{s.name}]", domain.build(
            s, disk_path="/d.qcow2", mac="52:54:00:00:00:01",
            wan_mac="52:54:00:00:00:02", cid=100, console_log="/c.log",
            seed_iso="/seed.iso", uuid="1234",
        )
        yield f"box-filter[{s.name}]", services.build_box_filter(s)

    yield "mgmt-network", network.MGMT_NET_XML
    yield "mgmt-filter", network.MGMT_FILTER_XML
    # Gateway injected, not discovered: this test checks that the documents we
    # hand libvirt are well-formed, which must not require a running libvirt.
    yield "wan-lan", network._wan_filter_xml(allow_lan=True, gw="192.168.122.1")
    yield "wan-nolan", network._wan_filter_xml(allow_lan=False,
                                               gw="192.168.122.1")


def main() -> int:
    failures = 0
    for label, xml in documents():
        try:
            parseString(xml)
            print(f"  ok    {label}")
        except Exception as exc:                       # noqa: BLE001
            print(f"  FAIL  {label}: {exc}")
            failures += 1

    # The read-only default is the security-critical invariant here, so assert
    # it rather than trusting the XML to look right.
    s = _spec(folders=[{"host": "/tmp", "tag": "t"}])
    if "<readonly/>" not in domain.build(
        s, "/d.qcow2", "52:54:00:00:00:01", "52:54:00:00:00:02", 100, "/c.log"
    ):
        print("  FAIL  a folder with no mode did not emit <readonly/>")
        failures += 1
    else:
        print("  ok    default folder emits <readonly/>")

    print("FAILED" if failures else "all generated XML is well-formed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
