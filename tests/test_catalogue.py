"""User catalogue merging.

`vm images` lists what you *can* use, not what is on disk, so a built-in entry
is offered whether or not its files are present. Overriding one is done by key
in ~/vmorch/images.toml -- and that override has to **patch** the built-in.

Building a fresh entry from the file alone shipped once: writing

    [debian-12]
    hidden = true

blanked that image's url and checksum, so `vm new --image debian-12` had
nothing to download. A one-line override silently breaking an image is exactly
the kind of thing nobody notices until they need it.

Run: python3 tests/test_catalogue.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vmorch import images  # noqa: E402


def with_catalogue(text: str):
    """Point the loader at a temporary images.toml."""
    tmp = Path(tempfile.mkdtemp()) / "images.toml"
    tmp.write_text(text)
    images.USER_CATALOGUE = tmp
    return tmp


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    original = images.USER_CATALOGUE
    try:
        # 1. A partial override patches the built-in rather than replacing it.
        with_catalogue("[debian-12]\nhidden = true\n")
        e = images.get("debian-12")
        failures += check("partial override keeps the built-in url",
                          bool(e.url), f"url was {e.url!r}")
        failures += check("partial override keeps the description",
                          "Debian 12" in e.description, e.description)
        failures += check("partial override applies the new field", e.hidden)
        failures += check("and does not lose other built-in flags", e.broken)

        # 2. Hiding removes it from the listing but not from use.
        failures += check("hidden entry is out of the pick-list",
                          "debian-12" not in images.catalogue())
        failures += check("hidden entry still resolves when named",
                          images.get("debian-12").key == "debian-12")
        failures += check("--all still shows it",
                          "debian-12" in images.catalogue(include_hidden=True))

        # 3. A brand-new entry needs no built-in to exist.
        with_catalogue('[mydistro]\nurl = "https://x/y.qcow2"\n')
        m = images.get("mydistro")
        failures += check("new entry works without a built-in",
                          m.url == "https://x/y.qcow2")
        failures += check("new entry defaults its description to the key",
                          m.description == "mydistro")

        # 4. Unknown keys are ignored, not fatal.
        with_catalogue('[weird]\nnonsense_key = 1\nurl = "https://a/b.img"\n')
        failures += check("unknown keys are ignored",
                          images.get("weird").url == "https://a/b.img")

        # 5. Built-ins survive an empty file.
        with_catalogue("")
        failures += check("built-ins present with an empty catalogue",
                          "ubuntu-24.04" in images.catalogue())
    finally:
        images.USER_CATALOGUE = original

    print("FAILED" if failures else "catalogue merging is correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
