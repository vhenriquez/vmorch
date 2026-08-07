"""Changing `mgmt_subnet` after the network exists must fail loudly.

The subnet is only read when the management network is *created*. Change it
afterwards and nothing reconciles: the live network keeps serving the old range.

Nothing downstream notices, which is what makes it dangerous. libvirt accepts a
`<host ip=...>` reservation **outside its own DHCP range without an error**,
dnsmasq then ignores it, and the guest takes a random in-range lease instead. So
`vmorch new` prints an address, writes it into the ssh config, and the box comes
up somewhere else entirely -- `ssh <box>` times out and nothing anywhere says
why.

Observed exactly that on 2026-08-07: a box reported at 10.150.0.50 was living on
192.168.150.134. Four boxes on the old subnet, two on the new, one tool with no
idea.

Run: python3 tests/test_mgmt_subnet.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vmorch import config, network, virsh  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def fake_dumpxml(subnet_gateway: str, netmask: str = "255.255.255.0"):
    def run(*args, **kw):
        if args and args[0] == "net-dumpxml":
            return (f"<network><name>{config.MGMT_NET}</name>"
                    f"<ip address='{subnet_gateway}' netmask='{netmask}'>"
                    "</ip></network>")
        raise virsh.VirshError(list(args), 1, "unexpected call")
    return run


def main() -> int:
    failures = 0
    real_run, real_subnet = virsh.run, config.MGMT_SUBNET

    try:
        # Matching: silent, as it should be.
        config.MGMT_SUBNET = "10.150.0.0/24"
        virsh.run = fake_dumpxml("10.150.0.1")
        try:
            network.check_mgmt_subnet()
            failures += check("a matching subnet passes quietly", True)
        except network.NetworkMismatch as exc:
            failures += check("a matching subnet passes quietly", False, str(exc))

        # libvirt reports the GATEWAY and a dotted netmask; the config wants a
        # CIDR network. Comparing them raw made every network look wrong, and
        # the advice printed back read `mgmt_subnet = "192.168.150.1/255.255.255.0"`.
        failures += check("the live subnet is normalised to CIDR",
                          network._live_mgmt() == ("10.150.0.0/24", "10.150.0.1"),
                          str(network._live_mgmt()))

        # Mismatched: must raise, and must be useful.
        virsh.run = fake_dumpxml("192.168.150.1")
        try:
            network.check_mgmt_subnet()
            failures += check("a changed subnet is refused", False,
                              "this is the silent-wrong-address bug")
        except network.NetworkMismatch as exc:
            msg = str(exc)
            failures += check("a changed subnet is refused", True)
            failures += check("...names what is live", "192.168.150.0/24" in msg)
            failures += check("...names what is configured", "10.150.0.0/24" in msg)
            failures += check("...explains the ssh symptom", "ssh" in msg, msg)
            failures += check("...offers the no-op way out",
                              'mgmt_subnet  = "192.168.150.0/24"' in msg, msg)
            failures += check("...offers a correct gateway with it",
                              'mgmt_gateway = "192.168.150.1"' in msg, msg)
            failures += check("...and the migrating way out",
                              "--migrate" in msg, msg)

        # No network yet: nothing to compare, and `vmorch new` on a clean host
        # must not trip over this.
        def missing(*a, **k):
            raise virsh.VirshError(list(a), 1, "not found")
        virsh.run = missing
        try:
            network.check_mgmt_subnet()
            failures += check("an absent network is not a mismatch", True)
        except network.NetworkMismatch as exc:
            failures += check("an absent network is not a mismatch", False, str(exc))

        # And the guard has to be wired into the path that allocates addresses,
        # or it protects nothing.
        src = (ROOT / "vmorch" / "network.py").read_text()
        body = src[src.index("def ensure_mgmt_network"):]
        failures += check("ensure_mgmt_network calls the guard",
                          "check_mgmt_subnet()" in body,
                          "the check exists but nothing runs it")
        cli = (ROOT / "vmorch" / "cli.py").read_text()
        failures += check("the CLI turns it into a sentence, not a traceback",
                          "network.NetworkMismatch" in cli)
    finally:
        virsh.run, config.MGMT_SUBNET = real_run, real_subnet

    print("FAILED" if failures else "a changed mgmt_subnet is caught")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
