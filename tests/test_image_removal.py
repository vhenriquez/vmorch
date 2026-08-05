"""Removing an image: the files, the cache and the catalogue entry.

Three things have to happen together, and each one fails differently:

  * The golden base is what every box built on the image reads its unmodified
    blocks from. Deleting it while a box exists does not free space, it breaks
    that box -- and quietly, because the box keeps running on cached clusters
    until it next touches an untouched block. So the refusal is tested, not
    just the deletion.
  * images.toml is mostly comments: the file header explaining the format, plus
    whatever the user wrote above their own entries. A parse-and-rewrite through
    tomllib would delete every one of them as the price of removing four lines,
    so the surgery is textual and the comments are asserted to survive.
  * The size shown in the confirmation prompt must still be the size reported
    afterwards. A property that stat()s the files gets that right before the
    removal and reports zero after it.

Run: python3 tests/test_image_removal.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vmorch import config, images  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


CATALOGUE = '''\
# vmorch image catalogue. Comments in here are the documentation, and they
# have to survive an entry being deleted.
catalogue_version = 1

[keepme]
description = "not the one being removed"
url = "https://example.invalid/keepme.qcow2"

["dotted-1.0"]
description = "a key TOML would split on the dot"
url = "https://example.invalid/dotted.qcow2"

[trailer]
description = "comes after, must survive"
url = "https://example.invalid/trailer.qcow2"
'''


class Sandbox:
    """A throwaway state dir with fake bases, cache and boxes."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.bases = self.root / "bases"
        self.cache = self.root / "cache"
        self.boxes = self.root / "boxes"
        for d in (self.bases, self.cache, self.boxes):
            d.mkdir(parents=True)
        self.catalogue = self.root / "images.toml"
        self.catalogue.write_text(CATALOGUE)

        self.saved = (images.USER_CATALOGUE, config.BASES_DIR,
                      config.DOWNLOAD_CACHE, config.BOXES_DIR)
        images.USER_CATALOGUE = self.catalogue
        config.BASES_DIR = self.bases
        config.DOWNLOAD_CACHE = self.cache
        config.BOXES_DIR = self.boxes

    def restore(self):
        (images.USER_CATALOGUE, config.BASES_DIR,
         config.DOWNLOAD_CACHE, config.BOXES_DIR) = self.saved

    def base(self, key: str, size: int) -> Path:
        # 0444, as ensure_base leaves it -- unlink still works but chmod is
        # what the real removal has to do first on some filesystems.
        p = self.bases / f"{key}.qcow2"
        if p.exists():
            p.chmod(0o644)       # a base left behind by --force is still 0444
        p.write_bytes(b"\0" * size)
        p.chmod(0o444)
        return p

    def cached(self, name: str, size: int) -> Path:
        p = self.cache / name
        p.write_bytes(b"\0" * size)
        return p

    def box(self, name: str, image: str) -> None:
        d = self.boxes / name
        d.mkdir()
        (d / "box.toml").write_text(f'name = "{name}"\nimage = "{image}"\n')


def main() -> int:
    failures = 0
    sb = Sandbox()
    try:
        # --- catalogue surgery ------------------------------------------
        sb.base("keepme", 4096)
        sb.cached("keepme.qcow2", 2048)

        plan = images.plan_removal("keepme")
        failures += check("plan finds the base", plan.base is not None)
        failures += check("plan finds the cached download", plan.cached is not None)
        failures += check("plan sizes both files", plan.freed == 4096 + 2048,
                          f"got {plan.freed}")
        failures += check("plan sees the catalogue entry", plan.in_catalogue)
        failures += check("plan sees no boxes", plan.used_by == ())

        done = images.remove(plan)
        text = sb.catalogue.read_text()
        failures += check("base file is gone", not (sb.bases / "keepme.qcow2").exists())
        failures += check("cached download is gone",
                          not (sb.cache / "keepme.qcow2").exists())
        failures += check("entry is out of images.toml", "[keepme]" not in text)
        failures += check("freed size survives the removal",
                          done.freed == 4096 + 2048, f"got {done.freed}")
        failures += check("the file's comments survive",
                          "have to survive" in text)
        failures += check("catalogue_version survives",
                          "catalogue_version = 1" in text)
        failures += check("the following entry survives", "[trailer]" in text)
        failures += check("the removed image is really gone from the catalogue",
                          "keepme" not in images.catalogue(include_hidden=True))
        failures += check("the rest of the catalogue still parses",
                          {"dotted-1.0", "trailer"} <=
                          set(images.catalogue(include_hidden=True)),
                          str(sorted(images.catalogue(include_hidden=True))))

        # A quoted, dotted header must be matched too. Left unhandled, the
        # entry stays in the file and the image comes back on the next listing.
        images.remove(images.plan_removal("dotted-1.0"))
        failures += check("quoted dotted key is removed",
                          "dotted-1.0" not in images.catalogue(include_hidden=True),
                          sb.catalogue.read_text())

        # --- a box depends on it ----------------------------------------
        sb.catalogue.write_text(CATALOGUE)
        base = sb.base("keepme", 8192)
        sb.box("sandbox", "keepme")

        plan = images.plan_removal("keepme")
        failures += check("a dependent box is found", plan.used_by == ("sandbox",),
                          str(plan.used_by))
        try:
            images.remove(plan)
            failures += check("removal is refused while a box uses it", False)
        except images.ImageError as exc:
            failures += check("removal is refused while a box uses it", True)
            failures += check("the refusal names the box", "sandbox" in str(exc),
                              str(exc))

        # --force must still not delete the base out from under a live box.
        done = images.remove(plan, force=True)
        failures += check("force keeps the base a box is using", base.exists())
        failures += check("force still removes the catalogue entry",
                          "[keepme]" not in sb.catalogue.read_text())
        failures += check("force reports only what it deleted", done.freed == 0,
                          f"got {done.freed}")

        # --- the backing chain, not just the spec -----------------------
        #
        # A box whose spec names something else can still be reading from this
        # base -- the entry may have been renamed under it. The spec answers
        # what was asked for; the chain answers what deleting a file breaks.
        if shutil.which("qemu-img"):
            sb.catalogue.write_text(CATALOGUE)
            real = sb.bases / "keepme.qcow2"
            if real.exists():
                real.chmod(0o644)
            subprocess.run(["qemu-img", "create", "-f", "qcow2", str(real), "64M"],
                           check=True, capture_output=True)
            d = sb.boxes / "renamed"
            d.mkdir(exist_ok=True)
            (d / "box.toml").write_text('name = "renamed"\nimage = "something-else"\n')
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                 "-b", str(real), str(d / "renamed.qcow2"), "64M"],
                check=True, capture_output=True)
            failures += check("a box is found by its backing chain",
                              "renamed" in images.boxes_using("keepme"),
                              str(images.boxes_using("keepme")))
            shutil.rmtree(d)
        else:
            print("  --   qemu-img absent, backing-chain check skipped")

        # --- keep-cache / keep-entry ------------------------------------
        sb.catalogue.write_text(CATALOGUE)
        sb.base("trailer", 1024)
        sb.cached("trailer.qcow2", 512)

        plan = images.plan_removal("trailer", keep_cache=True)
        failures += check("keep-cache leaves the download out of the plan",
                          plan.cached is None)
        failures += check("keep-cache still plans the base", plan.base is not None)
        images.remove(plan)
        failures += check("keep-cache keeps the file",
                          (sb.cache / "trailer.qcow2").exists())

        sb.catalogue.write_text(CATALOGUE)
        sb.base("trailer", 1024)
        plan = images.plan_removal("trailer", keep_entry=True)
        failures += check("keep-entry leaves the block out of the plan",
                          not plan.in_catalogue)
        images.remove(plan)
        failures += check("keep-entry keeps the block",
                          "[trailer]" in sb.catalogue.read_text())

        # --- nothing to do ----------------------------------------------
        plan = images.plan_removal("trailer", keep_entry=True, keep_cache=True)
        failures += check("an already-clean image plans as empty", plan.empty)

        # --- unknown image ----------------------------------------------
        try:
            images.plan_removal("no-such-image")
            failures += check("unknown image is an error", False)
        except images.ImageError:
            failures += check("unknown image is an error", True)

        # --- the warning text describes the plan, not a second guess -----
        from vmorch import cli
        shutil.rmtree(sb.boxes / "sandbox", ignore_errors=True)
        sb.catalogue.write_text(CATALOGUE)
        sb.base("keepme", 3 * 1024 ** 2)
        sb.cached("keepme.qcow2", 1024 ** 2)
        plan = images.plan_removal("keepme")
        warning = "\n".join(cli.describe_removal(plan))
        failures += check("warning lists something", bool(plan.files))
        for path in plan.files:
            failures += check(f"warning names {path.name}", str(path) in warning)
        failures += check("warning names the catalogue file",
                          str(images.USER_CATALOGUE) in warning)
        failures += check("warning states the space freed",
                          images.human_size(plan.freed) in warning, warning)
        failures += check("the freed figure is both files",
                          plan.freed == 4 * 1024 ** 2, str(plan.freed))

        # With a box on it, the base is not counted and is not offered: the
        # prompt must not promise back space that --force will never return.
        sb.box("sandbox", "keepme")
        plan = images.plan_removal("keepme")
        warning = "\n".join(cli.describe_removal(plan))
        failures += check("warning flags the dependent box",
                          "sandbox" in warning, warning)
        failures += check("the in-use base is marked KEPT", "KEPT" in warning,
                          warning)
        failures += check("the in-use base is out of the delete list",
                          plan.base not in plan.files)
        failures += check("the freed figure excludes the kept base",
                          plan.freed == 1024 ** 2, str(plan.freed))
    finally:
        sb.restore()

    print("FAILED" if failures else "image removal is correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
