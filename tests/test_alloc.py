"""Address and CID allocation.

Two properties pull against each other and both matter:

  stability   recreating a box under the same name must get the same address,
              or `ssh <name>` breaks and known_hosts complains
  finiteness  the pool is one /24. Never reclaiming would make 245 a hard
              lifetime limit on how many boxes may ever be created -- which a
              disposable-box workflow reaches in months

The resolution is to hold an address in reserve on destroy and reclaim the
oldest only when nothing fresh is left. These cases pin that, plus the rule
that CIDs are never recycled at all.

Run: python3 tests/test_alloc.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from vmorch import alloc, config  # noqa: E402


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    real_file, real_lo, real_hi = (config.ALLOC_FILE, config.ALLOC_IP_FIRST,
                                   config.ALLOC_IP_LAST)
    try:
        config.ALLOC_FILE = pathlib.Path(tempfile.mkdtemp()) / "alloc.json"
        config.ALLOC_IP_FIRST, config.ALLOC_IP_LAST = 10, 14

        first = alloc.allocate("a")
        failures += check("same name returns the same address",
                          alloc.allocate("a").ip == first.ip)

        for n in ("b", "c", "d", "e"):
            alloc.allocate(n)

        try:
            alloc.allocate("overflow")
            failures += check("full pool with no tombstones is refused", False)
        except alloc.AllocationError:
            failures += check("full pool with no tombstones is refused", True)

        alloc.release("a")
        time.sleep(1.1)          # released_at has second resolution
        alloc.release("c")

        reclaimed = alloc.allocate("new")
        failures += check("reclaims the address released longest ago",
                          reclaimed.ip == first.ip,
                          f"got {reclaimed.ip}, wanted {first.ip}")

        cids = {e.cid for e in alloc.all_allocations()}
        failures += check("CIDs are never recycled",
                          reclaimed.cid == max(cids),
                          f"cid {reclaimed.cid} vs max {max(cids)}")

        # Reclaiming must not leave the old owner pointing at an address that
        # now belongs to someone else.
        failures += check("the reclaimed box's record is gone",
                          alloc.get("a") is None)

        # Stability still wins while anything is free.
        before = alloc.get("d").ip
        alloc.release("d")
        failures += check("recreating a box keeps its address",
                          alloc.allocate("d").ip == before)

        failures += check("MAC follows the address",
                          alloc.get("d").mac.endswith(
                              f"{int(before.rsplit('.', 1)[1]):02x}"))
    finally:
        config.ALLOC_FILE, config.ALLOC_IP_FIRST, config.ALLOC_IP_LAST = (
            real_file, real_lo, real_hi)

    print("FAILED" if failures else "allocation is correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
