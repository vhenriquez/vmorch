"""Cloud image catalogue, download cache and checksum verification.

The whole premise of this project is that you never sit through an OS installer:
distro cloud images boot to a ready system, and cloud-init does the per-box
setup on first boot.

Storage split, forced by the hardware (see docs/host-capability-check.md):

  ~/vmorch/cloud_images/   pristine downloads. HDD, cold, written
                                         once, kept so a rebuild needs no network
  ~/.local/share/vmorch/bases/           golden images. NVMe, because qcow2
                                         backing chains read the base on every
                                         access to an unmodified block

Nothing is ever used before its checksum matches the distro's published sums
file.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class CatalogueEntry:
    key: str
    description: str
    url: str
    sums_url: str
    sums_algo: str          # distros disagree; Debian ships SHA512, Ubuntu SHA256
    os_variant: str         # for virt-install --osinfo
    package_manager: str

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
    ),
    "debian-13": CatalogueEntry(
        key="debian-13",
        description="Debian 13 (trixie) genericcloud amd64",
        url="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
        sums_url="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
        sums_algo="sha512",
        os_variant="debian13",
        package_manager="apt",
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


def get(key: str) -> CatalogueEntry:
    try:
        return CATALOGUE[key]
    except KeyError:
        raise ImageError(
            f"unknown image {key!r}. Known: {', '.join(sorted(CATALOGUE))}"
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


def ensure_base(entry: CatalogueEntry) -> Path:
    """Put a verified image on NVMe, ready to be a backing file.

    The base is copied out of the cold cache and marked read-only: every box
    overlays it, and a corrupted base would silently corrupt every box built on
    it.
    """
    base = base_path(entry)
    if base.exists():
        return base

    cached = download(entry)
    config.BASES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = base.with_suffix(".qcow2.tmp")
    shutil.copy2(cached, tmp)
    tmp.rename(base)
    base.chmod(0o444)
    return base
