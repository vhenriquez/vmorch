"""Stable per-box identifiers: IP, MAC and vsock CID.

Two properties matter here:

**Stability.** A box's IP must survive a rebuild, or `ssh <name>` breaks and the
host key warning fires every time. The allocation is recorded once and reused
for that name forever.

**No CID reuse, ever.** If a deleted box's context ID were handed to a new box,
a stale host-side vsock relay would serve the wrong box. The space is 32-bit, so
there is no reason to recycle: CIDs only ever go up.

**Addresses are held in reserve, then reclaimed.** Destroying a box tombstones
its address rather than freeing it, so recreating a box under the same name gets
the same address back and `ssh <name>` keeps working. But the pool is one /24 —
245 usable — and never reclaiming would turn that into a hard lifetime limit on
how many boxes may ever be created. So when nothing fresh is left, the address
of the box destroyed longest ago is reused. Stability where it is cheap,
recycling only when it is needed.

Reuse is only safe because destroy now cleans up after itself: the DHCP
reservation is removed, the known_hosts entry dropped, the ssh config fragment
regenerated and the per-box filter deleted. Leave any of those behind and a
recycled address points at a ghost.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from dataclasses import asdict, dataclass

from . import config


@contextlib.contextmanager
def _locked():
    """Hold an exclusive lock over the ledger for a read-modify-write.

    allocate() reads the file, picks the first free octet and CID, and writes
    the result back. The TUI and the CLI are separate processes and the TUI
    polls on a timer, so two `vmorch new` runs could interleave and hand the same
    address -- and the same vsock CID -- to two boxes. A CID handed out twice is
    the one thing this module says must never happen, because a stale host-side
    relay would then serve the wrong box.

    A sidecar lock file rather than the ledger itself: the ledger is replaced by
    rename, so a lock held on it would be a lock on an unlinked inode.
    """
    config.ALLOC_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = config.ALLOC_FILE.with_suffix(".json.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass
class Allocation:
    name: str
    ip: str
    mac: str
    cid: int
    released: bool = False   # tombstone: box gone, address held in reserve
    released_at: str = ""    # when, so the oldest is reclaimed first

    @property
    def wan_mac(self) -> str:
        """MAC for the internet NIC, derived from the management one.

        Derived rather than stored so that existing ledgers keep loading -- no
        migration, no schema bump. It has to be predictable at all because
        cloud-init's network config matches interfaces by MAC, and letting
        libvirt auto-generate this one would leave nothing to match against.
        """
        head, tail = self.mac.rsplit(":", 2)[0], self.mac.rsplit(":", 1)[1]
        return f"{head}:02:{tail}"


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


def _in_mgmt_subnet(ip: str) -> bool:
    """True if this address is on the management subnet as configured now.

    Ledger entries outlive configuration changes, so an address recorded under
    an older `mgmt_subnet` is still sitting there long after nothing serves it.
    """
    import ipaddress
    try:
        return (ipaddress.ip_address(ip)
                in ipaddress.ip_network(config.MGMT_SUBNET, strict=False))
    except ValueError:
        return False


def stale_allocations() -> list[Allocation]:
    """Every recorded address that is no longer on the management subnet.

    Reported rather than silently repaired where it is user-visible: these are
    boxes that will not answer to their own name until they are recreated.
    """
    return [a for a in all_allocations() if not _in_mgmt_subnet(a.ip)]


def allocate(name: str) -> Allocation:
    """Return this box's identifiers, creating them on first call.

    Re-allocating an existing name returns the original values, including for a
    tombstoned entry: recreating `agent-alpha` gets `agent-alpha`'s old address
    back, which is exactly what makes the ssh config entry keep working.

    The one exception is an address on a subnet this host no longer serves --
    see `_allocate_locked`.
    """
    with _locked():
        return _allocate_locked(name)


def _allocate_locked(name: str) -> Allocation:
    data = _load()
    allocations = data["allocations"]

    if name in allocations:
        existing = Allocation(**allocations[name])
        # ...unless it is no longer an address this host serves. Reusing a name
        # normally returns its old address on purpose, so a rebuilt box keeps
        # working. But if `mgmt_subnet` has changed since, that address is on a
        # network nothing routes: the box comes up on some other lease and
        # `ssh <name>` times out.
        #
        # Destroying the box does not clear it either -- release() tombstones
        # rather than deletes, which is what makes the address stable in the
        # first place. So recreating under the same name resurrects the broken
        # address, and no amount of destroy-and-retry escapes it. Observed
        # exactly that: a `test` box destroyed and recreated came back on the
        # same stale 10.150.0.49 twice.
        if _in_mgmt_subnet(existing.ip):
            if existing.released:
                existing.released = False
                allocations[name] = asdict(existing)
                _save(data)
            return existing
        # Drop it and fall through to a fresh address on the current subnet.
        del allocations[name]

    used_octets = {
        int(e["ip"].rsplit(".", 1)[1]) for e in allocations.values()
        if _in_mgmt_subnet(e["ip"])
    }
    used_cids = {int(e["cid"]) for e in allocations.values()}

    octet = next(
        (o for o in range(config.ALLOC_IP_FIRST, config.ALLOC_IP_LAST + 1)
         if o not in used_octets),
        None,
    )

    if octet is None:
        # Pool exhausted: reclaim the address of the box destroyed longest ago.
        # Held in reserve rather than freed on destroy so that recreating a box
        # keeps its address, but a finite pool cannot hold them forever -- 245
        # addresses is a few months of disposable boxes, not a lifetime.
        tombstones = sorted(
            (e for e in allocations.values() if e.get("released")),
            key=lambda e: e.get("released_at", ""),
        )
        if not tombstones:
            raise AllocationError(
                f"no free address in {config.MGMT_SUBNET}: "
                f"{len(used_octets)} in use and none released. Destroy a box, "
                "or widen the range in config."
            )
        oldest = tombstones[0]
        octet = int(oldest["ip"].rsplit(".", 1)[1])
        del allocations[oldest["name"]]

    # CIDs are never recycled even when an address is. The space is 32-bit, so
    # there is no pressure, and a stale host-side vsock relay handed a
    # different box would be a genuine confusion of identity.
    cid = max(used_cids, default=config.CID_FIRST - 1) + 1

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
    """Tombstone a box's allocation.

    Not freed immediately: recreating a box under the same name should get its
    old address back, so `ssh <name>` keeps working and no known_hosts entry is
    left pointing somewhere else. The address returns to the pool only when
    nothing fresh is left -- see `allocate`.
    """
    from datetime import datetime
    with _locked():
        data = _load()
        if name in data["allocations"]:
            data["allocations"][name]["released"] = True
            data["allocations"][name]["released_at"] = datetime.now().isoformat(
                timespec="seconds")
            _save(data)
