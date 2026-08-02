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
    """Point the loader at a temporary images.toml.

    The version marker is included so migration does not re-add shipped images
    on top of whatever the test is asserting.
    """
    tmp = Path(tempfile.mkdtemp()) / "images.toml"
    tmp.write_text(f"catalogue_version = {images.CATALOGUE_VERSION}\n\n" + text)
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
        # 1. The file is the catalogue: an entry absent from it is GONE, with
        #    no invisible built-in list adding it back.
        with_catalogue('["ubuntu-24.04"]\nurl = "https://x/u.img"\n')
        failures += check("deleted shipped image stays deleted",
                          "debian-12" not in images.catalogue(include_hidden=True))
        failures += check("and stays gone on a second read",
                          "debian-12" not in images.catalogue(include_hidden=True))

        # 2. A dotted key round-trips. TOML treats "." as a key separator, so
        #    an unquoted [ubuntu-24.04] silently becomes table "04" under
        #    "ubuntu-24" and the image comes back renamed and empty.
        failures += check("dotted key survives",
                          "ubuntu-24.04" in images.catalogue(),
                          str(sorted(images.catalogue())))
        rendered = images.serialize_entry(images.CATALOGUE["ubuntu-24.04"])
        failures += check("dotted key is quoted when written",
                          rendered.startswith('["ubuntu-24.04"]'),
                          rendered.split("\n")[0])

        # 3. hidden still works, for tidying without deleting.
        with_catalogue('[thing]\nurl = "https://x/y.img"\nhidden = true\n')
        failures += check("hidden entry is out of the pick-list",
                          "thing" not in images.catalogue())
        failures += check("hidden entry still resolves when named",
                          images.get("thing").key == "thing")
        failures += check("--all still shows it",
                          "thing" in images.catalogue(include_hidden=True))

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

        # 5. A file with no marker is topped up rather than clobbered.
        tmp = Path(tempfile.mkdtemp()) / "images.toml"
        tmp.write_text('[mine]\nurl = "https://x/mine.img"\n')
        images.USER_CATALOGUE = tmp
        cat = images.catalogue()
        failures += check("migration keeps the user's own entry", "mine" in cat)
        failures += check("migration adds the shipped images",
                          "ubuntu-24.04" in cat)
        failures += check("migration marks the file so it runs once",
                          "catalogue_version" in tmp.read_text())
    finally:
        images.USER_CATALOGUE = original

    print("FAILED" if failures else "catalogue merging is correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
