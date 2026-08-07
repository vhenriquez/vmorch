"""Human sizes, parsed and rendered in exactly one place.

There were two parsers with different unit tables -- boxes._bytes_of understood
T, domain._memory_kib did not -- so the same string was valid for a disk and a
traceback for memory. And rendering rounded to one decimal while parsing did
not, so a size could fail to survive a round trip: `vm disk` wrote "60.5G" back
into the spec, the next `vm apply` read it as smaller than the real disk, and
refused with "refusing to shrink" on a box nobody had shrunk.

Rendering is therefore exact by construction: whole GiB where it divides, and
otherwise the unit that does divide, never a rounded figure.
"""

from __future__ import annotations

UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


class SizeError(ValueError):
    """The size string is not one we can read."""


def parse(size: str) -> int:
    """Bytes from a string like "40G", "512M" or a bare number (meaning GiB)."""
    text = str(size).strip().upper().removesuffix("IB").removesuffix("B")
    if not text:
        raise SizeError("empty size")
    try:
        if text[-1] in UNITS:
            return int(float(text[:-1]) * UNITS[text[-1]])
        return int(float(text) * UNITS["G"])
    except ValueError:
        raise SizeError(
            f"cannot read size {size!r}: use e.g. 40G, 512M, or a bare number "
            "for GiB"
        ) from None


def render(size_bytes: int) -> str:
    """A spec-friendly string that parses back to exactly `size_bytes`.

    Never rounds. Falls back to the byte count rather than emit a figure that
    would not round-trip -- a size the spec cannot reproduce is what made
    `vm apply` refuse a disk it had itself just written.
    """
    for suffix in ("T", "G", "M", "K"):
        unit = UNITS[suffix]
        if size_bytes >= unit and size_bytes % unit == 0:
            return f"{size_bytes // unit}{suffix}"
    return str(size_bytes)
