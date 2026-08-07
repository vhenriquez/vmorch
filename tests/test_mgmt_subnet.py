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
            failures += check("...and the way to move deliberately",
                              "net-undefine" in msg, msg)
            # An error that names a command the tool does not have sends the
            # reader somewhere that does not exist. The first version pointed
            # at `vmorch net --migrate`, which was never wired up.
            import re as _re
            for cmd in _re.findall(r"\bvmorch ([a-z]+)", msg):
                failures += check(f"...and 'vmorch {cmd}' is a real command",
                                  f'"{cmd}"' in (ROOT / "vmorch" / "cli.py").read_text(),
                                  f"the message points at `vmorch {cmd}`")

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

    # --- the ledger, not just the network --------------------------------
    #
    # Guarding the network alone was not enough. release() tombstones rather
    # than deletes, on purpose, so a rebuilt box keeps its address -- which
    # means an address recorded under an older subnet comes BACK every time the
    # name is reused. Destroying and recreating did not escape it: `test` was
    # recreated onto the same dead 10.150.0.49 twice.
    import json
    import tempfile
    from vmorch import alloc

    saved_file = config.ALLOC_FILE
    tmp = Path(tempfile.mkdtemp()) / "allocations.json"
    config.ALLOC_FILE = tmp
    config.MGMT_SUBNET = "192.168.150.0/24"
    config.MGMT_GATEWAY = "192.168.150.1"
    try:
        tmp.write_text(json.dumps({"allocations": {
            "stale": {"name": "stale", "ip": "10.150.0.49",
                      "mac": "52:54:00:6d:01:31", "cid": 139,
                      "released": True, "released_at": "2026-08-07T22:55:51"},
            "good": {"name": "good", "ip": "192.168.150.33",
                     "mac": "52:54:00:6d:01:21", "cid": 123, "released": False},
        }}))

        failures += check("a stale allocation is spotted",
                          [a.name for a in alloc.stale_allocations()] == ["stale"])

        fresh = alloc.allocate("stale")
        failures += check("reusing the name does NOT return the dead address",
                          fresh.ip != "10.150.0.49", fresh.ip)
        failures += check("...it returns one on the current subnet",
                          fresh.ip.startswith("192.168.150."), fresh.ip)
        failures += check("...and the MAC follows the new address",
                          fresh.mac.endswith(f"{int(fresh.ip.rsplit('.',1)[1]):02x}"),
                          fresh.mac)
        failures += check("a good allocation is still returned unchanged",
                          alloc.allocate("good").ip == "192.168.150.33")
        failures += check("no stale entries remain afterwards",
                          alloc.stale_allocations() == [])

        # A cleanup path must never mint a record. destroy() called allocate()
        # to learn what to release; once allocate started re-issuing stale
        # entries it handed destroy a fresh mac and ip, which it then failed to
        # unreserve -- aborting part-way and leaving both the old reservation
        # and a new unreleased entry behind.
        src = (ROOT / "vmorch" / "boxes.py").read_text()
        body = src[src.index("def destroy("):src.index("def _wait_reachable")]
        failures += check("destroy() reads the allocation, never creates one",
                          "alloc.allocate(" not in body,
                          "destroy must use alloc.get()")
        failures += check("destroy() also clears reservations by name",
                          "unreserve_by_name" in body,
                          "a reservation written under an older subnet does not "
                          "match a delete built from the current allocation")
    finally:
        config.ALLOC_FILE = saved_file

    print("FAILED" if failures else "a changed mgmt_subnet is caught")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
