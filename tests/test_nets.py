"""Local networks: the one place box-to-box traffic is allowed.

Everything else in vmorch works to keep boxes apart, so this feature is the
exception and its edges are what matter:

  * a net must be members-only -- no <forward>, and no <ip> either, because an
    <ip> is what gives the host an address on the bridge and starts a dnsmasq
    on it;
  * the NIC must NOT be port-isolated, since that flag is exactly what stops
    box-to-box traffic on the other two bridges;
  * and the per-box filter must PIN the address rather than learn it, because
    on a shared segment the interesting attack is one member impersonating
    another.

The behaviour was verified against real boxes (peers reachable; LAN, host,
management gateway, the other box's management address and the internet all
still blocked; detaching removes the interface). These are the cheap checks that
keep it that way.

Run: python3 tests/test_nets.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vmorch import config, domain, nets, spec as spec_mod  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


LAB = nets.LocalNet(name="lab", index=0, subnet="192.168.160.0/24")


def main() -> int:
    failures = 0
    saved_file, saved_boxes = nets.NETS_FILE, config.BOXES_DIR
    saved_alloc = config.ALLOC_FILE
    tmp = Path(tempfile.mkdtemp())
    nets.NETS_FILE = tmp / "nets.toml"
    config.BOXES_DIR = tmp / "boxes"
    config.ALLOC_FILE = tmp / "allocations.json"

    try:
        # --- naming ------------------------------------------------------
        for bad, why in (("", "empty"), ("Lab", "upper case"),
                         ("9lab", "leading digit"), ("my_lab", "underscore"),
                         ("has space", "space")):
            try:
                nets._validate(bad)
                failures += check(f"rejects {why} name", False)
            except nets.NetError:
                failures += check(f"rejects {why} name", True)

        # The bridge is virbr-<name> and Linux caps an interface at 15 chars.
        # Over the limit libvirt accepts the network and then cannot create the
        # interface, which is a confusing way to fail.
        try:
            nets._validate("a" * (nets.NAME_MAX + 1))
            failures += check("rejects a name too long for the bridge", False)
        except nets.NetError as exc:
            failures += check("rejects a name too long for the bridge", True)
            failures += check("...and says why", "15" in str(exc), str(exc))

        nets._validate("a" * nets.NAME_MAX)
        failures += check("accepts a name at the limit", True)
        failures += check("the bridge fits IFNAMSIZ",
                          len(f"virbr-{'a' * nets.NAME_MAX}") <= 15)

        # --- the network XML ---------------------------------------------
        xml = nets._network_xml(LAB)
        failures += check("the network has no <forward>", "<forward" not in xml,
                          "a forward would route the segment somewhere")
        failures += check("the network has no <ip>", "<ip " not in xml,
                          "an <ip> gives the host an address on the bridge and "
                          "starts a dnsmasq -- that is not members-only")
        failures += check("...so it has no <dhcp> either", "<dhcp" not in xml)
        failures += check("it names the bridge", f"'{LAB.bridge}'" in xml)

        # --- addressing ---------------------------------------------------
        # A box's octet on a local net is its management octet, so the address
        # is derivable rather than looked up, and cannot collide.
        from vmorch import alloc
        a = alloc.allocate("boxa")
        octet = a.ip.rsplit(".", 1)[1]
        failures += check("the address reuses the management octet",
                          LAB.address("boxa") == f"192.168.160.{octet}",
                          f"{LAB.address('boxa')} vs octet {octet}")
        failures += check("the MAC ends in the same octet",
                          LAB.mac("boxa").endswith(f":{int(octet):02x}"),
                          LAB.mac("boxa"))
        # 0x01 is management and 0x02 is internet; nets start at 0x10 so a net
        # NIC can never be mistaken for either.
        failures += check("the MAC's net byte cannot collide with mgmt/wan",
                          LAB.mac("boxa").split(":")[4] not in ("01", "02"),
                          LAB.mac("boxa"))
        second = nets.LocalNet(name="two", index=1, subnet="192.168.161.0/24")
        failures += check("different nets give different MACs",
                          second.mac("boxa") != LAB.mac("boxa"))

        # --- the per-box filter -------------------------------------------
        fxml = nets.box_filter_xml(LAB, "boxa")
        failures += check("the filter pins the address",
                          f"value='{LAB.address('boxa')}'" in fxml, fxml)
        failures += check("...and turns learning off",
                          "CTRL_IP_LEARNING" in fxml and "'none'" in fxml,
                          "a learned address is one the guest chose")
        failures += check("it still references clean-traffic",
                          "clean-traffic" in fxml,
                          "anti-spoofing is the whole point on a shared segment")

        # --- the router role ------------------------------------------------
        #
        # Forwarding means emitting packets whose source belongs to somebody
        # else, which is indistinguishable from spoofing -- so a router cannot
        # keep the pin. Measured: with it, ping through a gateway box was 100%
        # loss; without it on that one box, 0% loss and HTTP 200 end to end.
        rxml = nets.box_filter_xml(LAB, "boxa", router=True)
        failures += check("a router has no IP pin",
                          "<parameter name='IP'" not in rxml, rxml)
        failures += check("...but keeps clean-traffic",
                          "clean-traffic" in rxml,
                          "MAC and ARP anti-spoofing still apply to a router")
        failures += check("...and still disables learning",
                          "CTRL_IP_LEARNING" in rxml)
        failures += check("a plain member still has the pin",
                          "<parameter name='IP'" in nets.box_filter_xml(LAB, "boxa"))

        # routes_for must name a net the box is actually on, or the spec reads
        # as though a firewall exists where none does.
        ok = spec_mod.parse({"name": "fw",
                             "network": {"nets": ["lab"], "routes_for": ["lab"]}})
        failures += check("routes_for round-trips", ok.routes_for == ["lab"])
        failures += check("...and is written to the spec file",
                          'routes_for = ["lab"]' in spec_mod.dump(ok))
        try:
            spec_mod.parse({"name": "fw",
                            "network": {"nets": [], "routes_for": ["lab"]}})
            failures += check("routing on a net you are not on is refused", False)
        except spec_mod.SpecError as exc:
            failures += check("routing on a net you are not on is refused", True)
            failures += check("...and says how to fix it",
                              "network.nets" in str(exc), str(exc))

        # The masquerade script must not name an interface: netplan does not
        # rename NICs, so `wan` is an id and the kernel still says enp2s0.
        from vmorch import guest
        script = guest.router_script(["192.168.160.0/24"])
        failures += check("masquerade matches on subnet, not interface",
                          "ip saddr 192.168.160.0/24" in script
                          and "oifname" not in script, script)
        failures += check("member-to-member traffic is not NATed",
                          "ip daddr != 192.168.160.0/24" in script)
        failures += check("forwarding is persisted, not just set",
                          "sysctl.d" in script,
                          "a router that stops routing after a reboot is the "
                          "worst kind to debug")
        failures += check("the nft rules are persisted too",
                          "systemctl enable" in script,
                          "nftables rules live in the kernel and do not survive "
                          "a reboot")
        failures += check("dropping the role cleans up",
                          "disable" in guest.router_script([]))

        # --- the domain XML -----------------------------------------------
        box = spec_mod.parse({"name": "boxa", "network": {"nets": ["lab"]}})
        real_get = nets.get
        nets.get = lambda n: LAB
        try:
            ifaces = domain._interfaces_xml(box, "52:54:00:6d:01:0a",
                                            "52:54:00:6d:02:0a")
        finally:
            nets.get = real_get

        failures += check("the net NIC is emitted",
                          LAB.libvirt_name in ifaces, ifaces)
        failures += check("it carries the per-box filter",
                          nets.box_filter_name("lab", "boxa") in ifaces)
        # The management NIC keeps its isolation; the net NIC must not have it,
        # or members cannot reach each other at all.
        net_block = ifaces.split(LAB.libvirt_name)[1]
        failures += check("the net NIC is NOT port-isolated",
                          "<port isolated" not in net_block,
                          "port isolation is exactly what this feature opts out "
                          "of; with it, the segment does nothing")
        failures += check("the management NIC still IS port-isolated",
                          "<port isolated='yes'/>" in
                          ifaces.split(LAB.libvirt_name)[0],
                          "joining a net must not weaken the other NICs")

        # --- the spec ------------------------------------------------------
        failures += check("nets round-trip through the spec file",
                          'nets = ["lab"]' in spec_mod.dump(box),
                          spec_mod.dump(box))
        reparsed = spec_mod.parse({"name": "boxa",
                                   "network": {"nets": ["lab", "lab", "two"]}})
        failures += check("duplicates are collapsed",
                          reparsed.nets == ["lab", "two"], str(reparsed.nets))
        failures += check("a bare string is accepted as one name",
                          spec_mod.parse({"name": "b",
                                          "network": {"nets": "lab"}}).nets
                          == ["lab"])
        for bad in (5, [5], [""], {"a": 1}):
            try:
                spec_mod.parse({"name": "b", "network": {"nets": bad}})
                failures += check(f"rejects nets = {bad!r}", False,
                                  "a net name that silently does not apply is a "
                                  "box quietly missing a NIC")
            except spec_mod.SpecError:
                failures += check(f"rejects nets = {bad!r}", True)

        # --- removal guard --------------------------------------------------
        # The net has to exist, or `remove` raises for the wrong reason and the
        # check passes without exercising the guard at all.
        nets._save({"lab": LAB})
        failures += check("the net is defined for the guard check",
                          nets.exists("lab"))
        try:
            nets.remove("lab", ["boxa", "boxb"])
            failures += check("refuses to remove a net with members", False)
        except nets.NetError as exc:
            failures += check("refuses to remove a net with members", True)
            failures += check("...and names them", "boxa" in str(exc), str(exc))
    finally:
        nets.NETS_FILE, config.BOXES_DIR = saved_file, saved_boxes
        config.ALLOC_FILE = saved_alloc

    print("FAILED" if failures else "local networks are correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
