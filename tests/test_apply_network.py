"""`vm apply` must reconcile the guest's network, not just the domain XML.

Granting internet to an existing box adds a NIC at the hypervisor and nothing
else: the guest's config was written by cloud-init at first boot, cloud-init does
not run again, and the interface sits DOWN while `vm apply` reports success. Same
shape as the disk field before it was reconciled -- the spec describing a box
that does not exist, with a success message on top.

What is checked here is the decision logic, not the ssh call: when the guest is
asked at all, what it is asked, and that a box which cannot be fixed from the
host says so instead of reporting a clean success.

Run: python3 tests/test_apply_network.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vmorch import boxes, guest, spec as spec_mod  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def make_spec(**kw):
    return spec_mod.parse({"name": "demo", "network": kw})


def main() -> int:
    failures = 0
    saved = guest.has_wan_config

    try:
        # --- when the guest gets asked at all ---------------------------
        asked = []
        guest.has_wan_config = lambda n: asked.append(n) or False

        asked.clear()
        failures += check(
            "an isolated box is never asked",
            not boxes._needs_wan_config("demo", make_spec(internet=False), True))
        failures += check("...and the guest is not contacted", asked == [])

        asked.clear()
        failures += check(
            "a stopped box is never asked",
            not boxes._needs_wan_config("demo", make_spec(internet=True), False))
        failures += check("...and the guest is not contacted", asked == [],
                          "a stopped box has no ssh; asking would hang")

        asked.clear()
        failures += check(
            "a running box with internet and no wan config needs it",
            boxes._needs_wan_config("demo", make_spec(internet=True), True))
        failures += check("...and the guest was asked", asked == ["demo"])

        # Already configured -- including a box created with internet from the
        # start, whose cloud-init wrote it at first boot. Rewriting it every
        # apply would be harmless but is still work nobody asked for.
        guest.has_wan_config = lambda n: True
        failures += check(
            "a box that already has wan config is left alone",
            not boxes._needs_wan_config("demo", make_spec(internet=True), True))

        # An unreachable box must not take the whole apply down with it: the
        # domain, the filters and the disk are all still worth reconciling.
        def unreachable(n):
            raise guest.GuestError("ssh: connect to host demo port 22: timed out")

        guest.has_wan_config = unreachable
        try:
            got = boxes._needs_wan_config("demo", make_spec(internet=True), True)
            failures += check("an unreachable box does not raise", True)
            failures += check("...and is reported as not needing it", got is False)
        except guest.GuestError as exc:
            failures += check("an unreachable box does not raise", False, str(exc))

        # --- what gets written -----------------------------------------
        #
        # Matched by MAC, because interface names follow PCI enumeration order.
        # A file keyed to enp2s0 would be silently wrong on any box that
        # enumerates differently.
        scripts = []
        real_run = guest.run
        guest.run = lambda name, script, check=True: (
            scripts.append(script) or ("yes" if "command -v netplan" in script
                                       else ""))
        try:
            guest.configure_wan("demo", "52:54:00:6d:02:23")
            written = scripts[-1]
            failures += check("the config matches on MAC",
                              'macaddress: "52:54:00:6d:02:23"' in written,
                              written)
            failures += check("it asks for dhcp", "dhcp4: true" in written)
            failures += check("it does NOT run `netplan apply`",
                              "netplan apply" not in written,
                              "applying live can tear down the ssh link this "
                              "very command runs over; the restart does it")
            failures += check("it is not cloud-init's own file",
                              "50-cloud-init" not in written)
            failures += check("the file is not world-readable",
                              "chmod 600" in written)
        finally:
            guest.run = real_run

        # A guest without netplan must be told to reseed, not left with a
        # half-written config it does not read.
        guest.run = lambda name, script, check=True: "no"
        try:
            guest.configure_wan("demo", "52:54:00:00:00:01")
            failures += check("a non-netplan guest raises", False)
        except guest.GuestError as exc:
            failures += check("a non-netplan guest raises", True)
            failures += check("...and the error points at `vm reseed`",
                              "vm reseed" in str(exc), str(exc))
        finally:
            guest.run = real_run

        # --- the note survives to the caller ----------------------------
        box = boxes.Box(spec=make_spec(internet=True), state="running",
                        ip="192.168.150.99", cid=199)
        failures += check("a Box carries no note by default", box.note == "")
        box.note = "configured the internet NIC inside the box"
        failures += check("a note can be attached for the caller to print",
                          box.note.startswith("configured"))
    finally:
        guest.has_wan_config = saved

    print("FAILED" if failures else "apply reconciles the guest network")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
