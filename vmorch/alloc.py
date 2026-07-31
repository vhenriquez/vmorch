"""Stable per-box identifiers: IP, MAC and vsock CID.

Two properties matter here:

**Stability.** A box's IP must survive a rebuild, or `ssh <name>` breaks and the
host key warning fires every time. The allocation is recorded once and reused
for that name forever.

**No CID reuse, ever.** If a deleted box's context ID were handed to a new box,
a stale host-side vsock relay would happily serve the wrong box. Deleting a box
therefore releases nothing: the record is tombstoned, not removed. IPs follow
the same rule for the same reason (a stale ssh config entry or known_hosts line
pointing at a recycled address is the same class of bug).

The ledger is append-only. That is the point.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import config


@dataclass
class Allocation:
    name: str
    ip: str
    mac: str
    cid: int
    released: bool = False   # tombstone: box gone, identifiers still burned


class AllocationError(RuntimeError):
    pass


def _load() -> dict:
    if not config.ALLOC_FILE.exists():
        return {"allocations": {}}
    with open(config.ALLOC_FILE) as fh:
        return json.load(fh)


def _save(data: dict) -> None:
    config.ALLOC_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.ALLOC_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(config.ALLOC_FILE)   # atomic: never a half-written ledger


def get(name: str) -> Allocation | None:
    entry = _load()["allocations"].get(name)
    return Allocation(**entry) if entry else None


def all_allocations() -> list[Allocation]:
    return [Allocation(**e) for e in _load()["allocations"].values()]


def _mac_for(ip_last_octet: int) -> str:
    # 52:54:00 is the QEMU/KVM OUI. Deriving the tail from the octet keeps the
    # MAC and the DHCP reservation trivially consistent.
    return f"52:54:00:6d:01:{ip_last_octet:02x}"


def allocate(name: str) -> Allocation:
    """Return this box's identifiers, creating them on first call.

    Re-allocating an existing name returns the original values, including for a
    tombstoned entry: recreating `agent-alpha` gets `agent-alpha`'s old address
    back, which is exactly what makes the ssh config entry keep working.
    """
    data = _load()
    allocations = data["allocations"]

    if name in allocations:
        existing = Allocation(**allocations[name])
        if existing.released:
            existing.released = False
            allocations[name] = asdict(existing)
            _save(data)
        return existing

    used_octets = {
        int(e["ip"].rsplit(".", 1)[1]) for e in allocations.values()
    }
    used_cids = {int(e["cid"]) for e in allocations.values()}

    octet = next(
        (o for o in range(config.ALLOC_IP_FIRST, config.ALLOC_IP_LAST + 1)
         if o not in used_octets),
        None,
    )
    if octet is None:
        raise AllocationError(
            f"no free address in {config.MGMT_SUBNET}; "
            f"{len(used_octets)} allocated (tombstones included, by design)"
        )

    cid = config.CID_FIRST
    while cid in used_cids:
        cid += 1

    prefix = config.MGMT_GATEWAY.rsplit(".", 1)[0]
    allocation = Allocation(
        name=name,
        ip=f"{prefix}.{octet}",
        mac=_mac_for(octet),
        cid=cid,
    )
    allocations[name] = asdict(allocation)
    _save(data)
    return allocation


def release(name: str) -> None:
    """Tombstone a box's allocation. The identifiers stay burned."""
    data = _load()
    if name in data["allocations"]:
        data["allocations"][name]["released"] = True
        _save(data)
