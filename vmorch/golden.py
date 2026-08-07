"""Build a golden image: a base with software already installed.

Why this exists: every box otherwise boots the plain cloud image, so anything
you want preinstalled has to be installed at first boot -- which is slow, and
outright impossible on an isolated box, because installing needs the internet
the box does not have. Baking the packages in once fixes both.

**This does not use virt-customize.** libguestfs cannot launch as a normal user
on this host: supermin needs to read /boot/vmlinuz, which Ubuntu ships
root:root 0600. Rather than require root, the image is built the way the
machine already knows how to build things -- boot a real box, install into it
over SSH, generalize it, shut it down cleanly, and flatten the result.

The generalize step is the part that is easy to get wrong. A disk copied
straight from a running system carries identity that must not be cloned:

  machine-id          systemd generates it once; clones would share it, and
                      DHCP leases keyed on it collide
  ssh host keys       every box would present the same host key
  cloud-init state    cloud-init records that it has already run, so a box made
                      from the image would never configure itself

All three are reset before shutdown.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from . import boxes, config, guest, images, virsh
from .spec import BoxSpec

#: Sensible defaults for an agent sandbox. tmux because you will want a session
#: that survives a dropped ssh connection.
DEFAULT_PACKAGES = ["tmux", "git", "curl", "rsync", "jq", "socat"]

BUILD_PREFIX = "golden-build-"


class GoldenError(RuntimeError):
    pass


GENERALIZE = r"""set -e
# cloud-init must forget it ever ran, or a box built from this image will skip
# its own configuration entirely.
cloud-init clean --logs --seed 2>/dev/null || true
rm -rf /var/lib/cloud/instances /var/lib/cloud/instance 2>/dev/null || true

# Host keys are per-machine. Leaving them would give every box the same
# identity, and ssh would rightly complain.
rm -f /etc/ssh/ssh_host_*

# machine-id must be EMPTY, not absent: systemd regenerates an empty one at
# boot, but a missing file is an error on some images.
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

# Leftovers that would otherwise be cloned into every box.
rm -f /etc/netplan/50-cloud-init.yaml
apt-get clean 2>/dev/null || true
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
rm -f /root/.bash_history /home/*/.bash_history
sync
"""


def _install_script(packages: list[str], run: list[str]) -> str:
    lines = ["set -e", "export DEBIAN_FRONTEND=noninteractive",
             "apt-get update"]
    if packages:
        lines.append("apt-get install -y --no-install-recommends "
                     + " ".join(packages))
    lines += run
    lines.append("sync")
    return "\n".join(lines) + "\n"


def _flatten(src_disk: Path, dest: Path) -> None:
    """Collapse the box's overlay and its backing chain into one image.

    `qemu-img convert` reads the whole chain and writes a standalone file, so
    the golden image does not depend on the upstream base still being present.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".qcow2.building")
    tmp.unlink(missing_ok=True)
    subprocess.run(
        ["qemu-img", "convert", "-O", "qcow2", "-c", str(src_disk), str(tmp)],
        check=True, capture_output=True,
    )
    tmp.rename(dest)
    dest.chmod(0o444)      # every box overlays this; nothing may write to it


def build(name: str, from_image: str | None = None,
          packages: list[str] | None = None,
          run: list[str] | None = None,
          keep_build_box: bool = False,
          progress=lambda msg: None) -> Path:
    """Build a golden image and register it. Returns the image path."""
    from_image = from_image or config.DEFAULT_IMAGE
    packages = DEFAULT_PACKAGES if packages is None else packages
    run = run or []

    if name == from_image:
        raise GoldenError("the golden image needs a different name to its source")

    dest = config.BASES_DIR / f"{name}.qcow2"
    if dest.exists():
        raise GoldenError(f"{dest} already exists; remove it or pick another name")

    build_box = f"{BUILD_PREFIX}{name}"[:28]
    if boxes.exists(build_box):
        raise GoldenError(
            f"a previous build box {build_box!r} is still around; "
            f"remove it with `vmorch rm {build_box}`"
        )

    # The build box needs the internet -- that is the entire point, it is where
    # the packages come from. The resulting image is then usable by boxes that
    # have none.
    spec = BoxSpec(name=build_box, image=from_image, internet=True, lan=False,
                   memory="2G", disk="20G")

    progress(f"creating build box from {from_image}")
    boxes.create(spec, start=True)

    try:
        progress("waiting for first boot")
        boxes._wait_reachable(build_box, timeout=420)

        # cloud-init may still be installing; racing it breaks apt.
        progress("waiting for cloud-init to settle")
        guest.run(build_box, "cloud-init status --wait >/dev/null 2>&1 || true\n",
                  check=False)

        progress(f"installing {len(packages)} packages")
        guest.run(build_box, _install_script(packages, run))

        progress("generalizing (machine-id, host keys, cloud-init state)")
        guest.run(build_box, GENERALIZE)

        progress("shutting down cleanly")
        boxes.stop(build_box)
        _wait_stopped(build_box)

        progress("flattening image")
        _flatten(boxes.disk_path(build_box), dest)

    finally:
        if not keep_build_box and boxes.exists(build_box):
            progress("removing build box")
            try:
                boxes.destroy(build_box)
            except Exception:                          # noqa: BLE001
                pass

    desc = f"{from_image} + " + (", ".join(packages) if packages else "customizations")
    images.register_local(name, desc)
    progress(f"registered as image {name!r}")
    return dest


def build_from_box(name: str, source_box: str, keep_build_box: bool = False,
                   progress=lambda msg: None) -> Path:
    """Freeze an existing box into a reusable image.

    The hands-on counterpart to `build`: make a box, ssh in, set it up however
    you like, then turn it into an image. Nothing has to be expressible as a
    package list.

    The source box is **not modified**. Generalizing has to happen -- machine-id
    and ssh host keys cannot be cloned -- but doing it in place would strip the
    identity of a box you still use. So its disk is flattened into a staging
    image, a throwaway box is booted from that, generalized and flattened again.
    Two passes over the disk, and the box you built by hand is left alone.
    """
    if not boxes.exists(source_box):
        raise GoldenError(f"no such box: {source_box}")

    src = boxes.load(source_box)
    if src.state == "running":
        raise GoldenError(
            f"stop {source_box} first (`vmorch stop {source_box}`). Imaging a live "
            "box captures whatever was mid-write."
        )

    dest = config.BASES_DIR / f"{name}.qcow2"
    if dest.exists():
        raise GoldenError(f"{dest} already exists; remove it or pick another name")

    build_box = f"{BUILD_PREFIX}{name}"[:28]
    if boxes.exists(build_box):
        raise GoldenError(f"remove the leftover build box first: vmorch rm {build_box}")

    # Flatten straight to the image's real path: the throwaway box below is
    # created *from* this image, so it has to be where the catalogue looks.
    # The second flatten later replaces it in place (via a temp file, so the
    # convert never reads and writes the same path).
    progress(f"flattening {source_box}")
    _flatten(boxes.disk_path(source_box), dest)

    # Registered before the throwaway box, because that box is created *from*
    # this image and boxes.create resolves it through the catalogue. The entry
    # is provisional until the generalize pass below succeeds -- see the
    # rollback in the except clause, without which a failure here left a
    # registered image that was a straight flatten of the source box, carrying
    # its machine-id and its ssh host keys. Cloning those is the one thing this
    # function exists to prevent.
    images.register_local(name, f"built by hand from {source_box} (building)")
    try:
        progress("booting a throwaway copy to generalize it")
        spec = BoxSpec(name=build_box, image=name, internet=False,
                       memory=src.spec.memory, disk=src.spec.disk)
        boxes.create(spec, start=True)

        boxes._wait_reachable(build_box, timeout=420)
        progress("generalizing (machine-id, host keys, cloud-init state)")
        guest.run(build_box, GENERALIZE)

        progress("shutting down cleanly")
        boxes.stop(build_box)
        _wait_stopped(build_box)

        progress("flattening image")
        _flatten(boxes.disk_path(build_box), dest)
    except Exception:
        # A half-built image is worse than none: it boots, and every box made
        # from it shares an identity with the source. Leave nothing behind that
        # `vmorch images` would offer.
        dest.unlink(missing_ok=True)
        images.remove_from_catalogue(name)
        raise
    finally:
        if not keep_build_box and boxes.exists(build_box):
            progress("removing throwaway box")
            try:
                boxes.destroy(build_box)
            except Exception:                          # noqa: BLE001
                pass

    # Rewrites the provisional entry now that the image is actually usable.
    images.register_local(name, f"built by hand from {source_box}", replace=True)
    progress(f"registered as image {name!r}")
    return dest


def _wait_stopped(name: str, timeout: int = 180) -> None:
    """A clean shutdown matters: unflushed writes would be baked in missing."""
    domain = config.DOMAIN_PREFIX + name
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if virsh.domain_state(domain) == "shut off":
            return
        time.sleep(3)
    virsh.run("destroy", domain, check=False)
    raise GoldenError(
        f"{name} did not shut down within {timeout}s; the image would be "
        "crash-consistent at best, so the build was abandoned"
    )


def remove(name: str) -> None:
    path = config.BASES_DIR / f"{name}.qcow2"
    if not path.exists():
        raise GoldenError(f"no golden image {name!r}")
    path.unlink()
