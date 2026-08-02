"""Cloud image catalogue, download cache and checksum verification.

The whole premise of this project is that you never sit through an OS installer:
distro cloud images boot to a ready system, and cloud-init does the per-box
setup on first boot.

Storage split, forced by the hardware (see docs/host-capability-check.md):

  ~/vmorch/cloud_images/   pristine downloads. HDD, cold, written
                                         once, kept so a rebuild needs no network
  ~/vmorch/bases/                        golden images. NVMe, because qcow2
                                         backing chains read the base on every
                                         access to an unmodified block

Nothing is ever used before its checksum matches the distro's published sums
file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from . import config


@dataclass(frozen=True)
class CatalogueEntry:
    key: str
    description: str
    url: str = ""
    sums_url: str = ""
    sums_algo: str = "sha256"   # distros disagree; Debian ships SHA512
    os_variant: str = "linux2022"
    package_manager: str = "apt"
    # False for images known not to work. Surfaced in the UI so that pressing
    # Enter through a dialog cannot land you on a broken image.
    verified: bool = True
    #: Some distros publish the disk inside a tarball rather than as a bare
    #: qcow2 -- Kali does. The published checksum covers the *archive*, so it is
    #: verified first and the disk extracted afterwards.
    archive_member: str = ""
    #: A golden image built locally by `vm golden`. Nothing to download.
    local: bool = False
    #: Hide a built-in entry you never want to see. The built-in catalogue is
    #: compiled in, so this is the only way to get rid of one -- deleting its
    #: base file frees the disk but the entry is still offered.
    hidden: bool = False
    #: Known NOT to work, as opposed to merely untested. Kept separate from
    #: `verified` because "nobody has booted this yet" and "this is known to be
    #: broken" are very different things to tell someone.
    broken: bool = False

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def cached(self) -> Path:
        return config.DOWNLOAD_CACHE / self.filename


# Index pages are stable; exact filenames move per release, so these all point
# at the distro's "latest"/"current" alias rather than a pinned build.
#
# VERIFIED WORKING: ubuntu-24.04 (boots, cloud-init applies user-data, SSH up).
#
# KNOWN BROKEN: debian-12. Tested 2026-07-31 -- the genericcloud image boots to
# a login prompt but cloud-init never runs. No cloud-init units appear in the
# boot at all, the hostname stays "localhost", and ssh.service fails for want of
# host keys. The same seed ISO drives Ubuntu correctly, so this is the image,
# not the seed. Left in the catalogue because the download and verify paths are
# exercised by it, but do not make it a default until someone works out why.
CATALOGUE: dict[str, CatalogueEntry] = {
    "debian-12": CatalogueEntry(
        key="debian-12",
        description="Debian 12 (bookworm) genericcloud amd64",
        url="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        sums_url="https://cloud.debian.org/images/cloud/bookworm/latest/SHA512SUMS",
        sums_algo="sha512",
        os_variant="debian12",
        package_manager="apt",
        verified=False,
        broken=True,        # cloud-init never runs; see the note above
    ),
    "debian-13": CatalogueEntry(
        key="debian-13",
        description="Debian 13 (trixie) genericcloud amd64",
        url="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
        sums_url="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
        sums_algo="sha512",
        os_variant="debian13",
        package_manager="apt",
        verified=False,     # untested; debian-12 is broken, assume nothing
    ),
    "ubuntu-24.04": CatalogueEntry(
        key="ubuntu-24.04",
        description="Ubuntu 24.04 LTS (noble) server cloud amd64",
        # Ubuntu's cloud images carry a .img extension but are qcow2.
        url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        sums_url="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        sums_algo="sha256",
        os_variant="ubuntu24.04",
        package_manager="apt",
    ),
}


class ImageError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# User catalogue
#
# Adding an image should not mean editing this file. ~/vmorch/images.toml is
# merged over the built-ins, so a new distro is a few lines of config, and
# golden images built by `vm golden` register themselves there.
# --------------------------------------------------------------------------

USER_CATALOGUE = config.STATE_DIR / "images.toml"

EXAMPLE_USER_CATALOGUE = '''\
# Extra images for vmorch. Merged over the built-in catalogue, so an entry
# here with the same key overrides the built-in one.
#
# A remote image:
#
#   [kali]
#   description = "Kali Linux rolling cloud image"
#   url = "https://kali.download/cloud-images/current/kali-linux-2026.2-cloud-genericcloud-amd64.tar.xz"
#   sums_url = "https://kali.download/cloud-images/current/SHA256SUMS"
#   sums_algo = "sha256"
#   archive_member = "*.qcow2"   # only if the download is a tarball
#   os_variant = "kali"
#   verified = false             # set true once you have booted it
#
# To hide a built-in image you never use, override it with hidden = true:
#
#   [debian-12]
#   hidden = true
#
# A golden image built locally is added for you by `vm golden`.
'''


#: Bumped only if the shipped catalogue changes in a way existing users should
#: pick up. Its presence is what marks a file as already migrated.
CATALOGUE_VERSION = 1

_FIELD_ORDER = ("description", "url", "sums_url", "sums_algo", "archive_member",
                "os_variant", "package_manager", "verified", "local", "hidden",
                "broken")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return f'"{v}"'


def _table_name(key: str) -> str:
    """TOML table header for an image key.

    A dot is TOML's key separator, so a bare [ubuntu-24.04] declares table "04"
    nested inside "ubuntu-24" -- the image silently comes back named
    "ubuntu-24" with everything else lost. Keys with dots must be quoted.
    """
    return f'["{key}"]' if not key.replace("-", "").replace("_", "").isalnum() \
        else f"[{key}]"


def serialize_entry(entry: CatalogueEntry) -> str:
    """Render one entry as a TOML block, omitting anything left at default."""
    default = CatalogueEntry(key="_", description="_")
    lines = [_table_name(entry.key)]
    for field in _FIELD_ORDER:
        value = getattr(entry, field)
        if field != "description" and value == getattr(default, field):
            continue
        if value == "":
            continue
        lines.append(f"{field} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


CATALOGUE_HEADER = f"""\
# vmorch image catalogue. THIS FILE IS THE CATALOGUE -- nothing is hidden in
# the code. `vm images` lists exactly what is here.
#
# To remove an image you do not want, delete its block. That is all.
# To add one, copy a block and edit it; `vm golden` appends its own.
# To keep an entry but hide it from listings, set hidden = true.
#
# Restore the images that ship with vmorch: vm images --restore-defaults

catalogue_version = {CATALOGUE_VERSION}
"""


def ensure_user_catalogue() -> None:
    """Materialise the shipped catalogue into the user's file, once.

    The built-ins used to be merged in from code on every run, so an entry
    could not be deleted -- only hidden -- and there was no file to point at
    when someone asked where an image was defined. Writing them out once makes
    this file the single source of truth: delete a block and the image is gone.

    Runs only when the version marker is absent, so a deleted entry stays
    deleted.
    """
    if USER_CATALOGUE.exists():
        text = USER_CATALOGUE.read_text()
        if "catalogue_version" in text:
            return
        # Pre-existing file from before this change: top it up with any shipped
        # entry it does not already define, rather than clobbering their work.
        missing = [e for k, e in CATALOGUE.items()
                   if _table_name(k) not in text]
        USER_CATALOGUE.write_text(
            CATALOGUE_HEADER
            + "\n# --- your entries (kept from the previous file) ---\n\n"
            + text.lstrip("\n")
            + ("\n\n# --- images shipped with vmorch ---\n\n"
               + "\n".join(serialize_entry(e) for e in missing) if missing else "")
        )
        return

    USER_CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
    USER_CATALOGUE.write_text(
        CATALOGUE_HEADER + "\n"
        + "\n".join(serialize_entry(e) for e in CATALOGUE.values())
    )


def restore_defaults() -> list[str]:
    """Re-add any shipped image the user has deleted. Returns what was added."""
    ensure_user_catalogue()
    text = USER_CATALOGUE.read_text()
    missing = [e for k, e in CATALOGUE.items()
                   if _table_name(k) not in text]
    if missing:
        USER_CATALOGUE.write_text(
            text.rstrip("\n") + "\n\n"
            + "\n".join(serialize_entry(e) for e in missing)
        )
    return [e.key for e in missing]


def load_user_catalogue() -> dict[str, CatalogueEntry]:
    """Parse ~/vmorch/images.toml.

    An entry naming a built-in **patches** it rather than replacing it. Writing

        [debian-12]
        hidden = true

    should hide that image, not blank its url and checksum -- which is exactly
    what building a fresh entry from the file alone did, leaving `vm new --image
    debian-12` unable to download anything.
    """
    if not USER_CATALOGUE.exists():
        return {}
    with open(USER_CATALOGUE, "rb") as fh:
        data = tomllib.load(fh)

    out: dict[str, CatalogueEntry] = {}
    for key, raw in data.items():
        if not isinstance(raw, dict):
            continue
        overrides = {k: v for k, v in raw.items()
                     if k in CatalogueEntry.__dataclass_fields__ and k != "key"}
        base = CATALOGUE.get(key)
        if base is not None:
            out[key] = replace(base, **overrides)
        else:
            overrides.setdefault("description", key)
            out[key] = CatalogueEntry(key=key, **overrides)
    return out


def catalogue(include_hidden: bool = False) -> dict[str, CatalogueEntry]:
    """Every image, read from the user's catalogue file.

    The file is authoritative. An entry deleted from it is gone -- there is no
    invisible built-in list adding it back.
    """
    ensure_user_catalogue()
    try:
        entries = load_user_catalogue()
    except Exception as exc:                          # noqa: BLE001
        raise ImageError(f"{USER_CATALOGUE}: {exc}") from None
    if include_hidden:
        return entries
    return {k: v for k, v in entries.items() if not v.hidden}


def register_local(key: str, description: str) -> None:
    """Record a locally built golden image in the user catalogue."""
    USER_CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
    if not USER_CATALOGUE.exists():
        USER_CATALOGUE.write_text(EXAMPLE_USER_CATALOGUE)

    text = USER_CATALOGUE.read_text()
    if f"\n[{key}]" in text or text.startswith(f"[{key}]"):
        return                                        # already registered
    USER_CATALOGUE.write_text(
        text.rstrip("\n")
        + f'\n\n[{key}]\ndescription = "{description}"\nlocal = true\n'
    )


def get(key: str) -> CatalogueEntry:
    known = catalogue(include_hidden=True)
    try:
        return known[key]
    except KeyError:
        raise ImageError(
            f"unknown image {key!r}. Known: {', '.join(sorted(known))}\n"
            f"Add your own in {USER_CATALOGUE}"
        ) from None


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _expected_digest(entry: CatalogueEntry) -> str:
    """Pull this image's digest out of the distro's sums file."""
    for line in _fetch_text(entry.sums_url).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == entry.filename:
            return parts[0].lower()
    raise ImageError(
        f"{entry.filename} not listed in {entry.sums_url} -- the distro may "
        "have rolled to a new release; check the catalogue URL"
    )


def _digest_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def verify(entry: CatalogueEntry) -> bool:
    """True if the cached file matches the published digest."""
    if not entry.cached.exists():
        return False
    return _digest_file(entry.cached, entry.sums_algo) == _expected_digest(entry)


def download(entry: CatalogueEntry, force: bool = False) -> Path:
    """Fetch into the cache and verify. Returns the cached path.

    A cached file that already verifies is left alone -- these are multi-hundred
    -megabyte downloads and re-fetching them is pure waste.
    """
    config.DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)

    if entry.cached.exists() and not force:
        if verify(entry):
            return entry.cached
        # A cached file that fails verification is a truncated or superseded
        # download. Replace it rather than trusting it.
        entry.cached.unlink()

    expected = _expected_digest(entry)
    partial = entry.cached.with_suffix(entry.cached.suffix + ".part")

    with urllib.request.urlopen(entry.url, timeout=120) as resp, open(partial, "wb") as out:
        shutil.copyfileobj(resp, out, length=4 * 1024 * 1024)

    actual = _digest_file(partial, entry.sums_algo)
    if actual != expected:
        partial.unlink()
        raise ImageError(
            f"checksum mismatch for {entry.filename}\n"
            f"  expected {expected}\n  got      {actual}"
        )

    partial.rename(entry.cached)   # only a verified file ever gets the real name
    return entry.cached


def base_path(entry: CatalogueEntry) -> Path:
    return config.BASES_DIR / f"{entry.key}.qcow2"


ARCHIVE_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst", ".tgz", ".tar")


def is_archive(entry: CatalogueEntry) -> bool:
    return entry.filename.endswith(ARCHIVE_SUFFIXES)


def _extract_disk(archive: Path, pattern: str, dest: Path) -> Path:
    """Pull the disk image out of a downloaded tarball.

    Kali and friends publish `.tar.xz` rather than a bare qcow2. The published
    checksum covers the archive, so verification happens before this runs and
    the extraction is trusted only because the archive was.

    With no pattern the largest regular file wins. That is deliberately dumb and
    almost always right: these archives hold one disk plus, at most, a small
    licence or checksum file. Requiring the caller to name the member means
    knowing the archive's internals before ever downloading it -- and guessing
    wrong is easy, since Kali ships `disk.raw` where the obvious guess is
    `*.qcow2`.
    """
    import fnmatch
    import tarfile

    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        if not members:
            raise ImageError(f"{archive.name} contains no files")

        if pattern:
            matches = [m for m in members
                       if fnmatch.fnmatch(Path(m.name).name, pattern)]
            if not matches:
                names = ", ".join(Path(m.name).name for m in members[:8])
                raise ImageError(
                    f"no member matching {pattern!r} in {archive.name}. "
                    f"Contains: {names}. Leave archive_member unset to take "
                    "the largest file."
                )
        else:
            matches = members

        member = max(matches, key=lambda m: m.size)
        src = tar.extractfile(member)
        if src is None:
            raise ImageError(f"could not read {member.name} from {archive.name}")
        with src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, length=4 * 1024 * 1024)
    return dest


def ensure_base(entry: CatalogueEntry) -> Path:
    """Put a verified image on NVMe, ready to be a backing file.

    The base is copied out of the cold cache and marked read-only: every box
    overlays it, and a corrupted base would silently corrupt every box built on
    it.
    """
    base = base_path(entry)
    if base.exists():
        return base

    if entry.local:
        raise ImageError(
            f"golden image {entry.key!r} is registered but its file is missing: "
            f"{base}. Rebuild it with `vm golden`."
        )

    cached = download(entry)
    config.BASES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = base.with_suffix(".qcow2.tmp")

    if is_archive(entry):
        _extract_disk(cached, entry.archive_member, tmp)
        # An extracted disk may be raw rather than qcow2; normalise so the
        # overlay chain always has a qcow2 backing file.
        info = subprocess.run(
            ["qemu-img", "info", "--output=json", str(tmp)],
            capture_output=True, text=True, check=True,
        ).stdout
        if json.loads(info).get("format") != "qcow2":
            conv = base.with_suffix(".qcow2.conv")
            subprocess.run(
                ["qemu-img", "convert", "-O", "qcow2", str(tmp), str(conv)],
                check=True, capture_output=True,
            )
            tmp.unlink()
            conv.rename(tmp)
    else:
        shutil.copy2(cached, tmp)

    tmp.rename(base)
    base.chmod(0o444)
    return base
