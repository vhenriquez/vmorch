"""Per-box snapshots and rollback.

The motivating case is the one this whole project exists for: let an agent try
something destructive, then rewind.

Chain layout, newest last:

    golden base (shared, read-only)  ->  snap ... snap  ->  disk.qcow2 (active)

Only `disk.qcow2` is ever written by a running box. Taking a snapshot freezes
the current active layer under a name and starts a fresh active layer on top of
it.

**Pruning must never commit downward into the golden base.** That base is shared
by every box built on the same image; merging one box's changes into it would
silently corrupt all of them. Dropping the oldest snapshot is therefore done by
*rebasing the next one onto the golden base* -- which copies the departing
layer's clusters upward into the survivor -- and only then deleting it.

Snapshot and rollback both require the box to be stopped. Live external
snapshots are possible but bring a pile of failure modes, and for a sandbox
"stop, snapshot, experiment, rewind" is a perfectly good workflow.

**Snapshots do not cover shared folders.** virtiofs mounts are the live host
filesystem, not part of the disk image, so rolling back does not undo anything
the agent wrote into an rw share. Callers are expected to say so out loud.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import config


class SnapshotError(RuntimeError):
    pass


@dataclass
class Snapshot:
    index: int
    label: str
    created: str
    filename: str


def _snap_dir(box_dir: Path) -> Path:
    return box_dir / "snapshots"


def _meta_path(box_dir: Path) -> Path:
    return _snap_dir(box_dir) / "index.json"


def load_all(box_dir: Path) -> list[Snapshot]:
    path = _meta_path(box_dir)
    if not path.exists():
        return []
    return [Snapshot(**s) for s in json.loads(path.read_text())]


def _save_all(box_dir: Path, snaps: list[Snapshot]) -> None:
    _snap_dir(box_dir).mkdir(parents=True, exist_ok=True)
    _meta_path(box_dir).write_text(
        json.dumps([asdict(s) for s in snaps], indent=2) + "\n"
    )


DISK_MODE = 0o660   # see boxes._create_overlay: the ACL mask needs the group bits


def _qemu_img(*args: str) -> None:
    proc = subprocess.run(["qemu-img", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SnapshotError(f"qemu-img {' '.join(args)}: {proc.stderr.strip()}")


def _backing_of(disk: Path) -> str | None:
    out = subprocess.run(
        ["qemu-img", "info", "--output=json", str(disk)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out).get("backing-filename")


#: A label becomes part of a filename, so it may not contain a path separator or
#: traverse out of the snapshot directory. Without this, `vm snapshot box ../x`
#: renamed the box's live disk outside its own directory.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _clean_label(label: str, index: int) -> str:
    if label is None or label == "":
        return f"snap{index}"
    if not _LABEL_RE.match(label) or ".." in label:
        raise SnapshotError(
            f"invalid snapshot label {label!r}: letters, digits, dot, dash and "
            "underscore only, starting with a letter or digit. The label "
            "becomes part of a filename."
        )
    return label


def create(box_dir: Path, disk: Path, label: str | None = None) -> Snapshot:
    """Freeze the active layer and start a new one on top."""
    snaps = load_all(box_dir)
    index = (max((s.index for s in snaps), default=0)) + 1
    label = _clean_label(label, index)

    _snap_dir(box_dir).mkdir(parents=True, exist_ok=True)
    frozen = _snap_dir(box_dir) / f"{index:03d}-{label}.qcow2"
    if frozen.exists():
        raise SnapshotError(f"snapshot file already exists: {frozen}")

    backing = _backing_of(disk)
    if backing is None:
        raise SnapshotError(f"{disk} has no backing file; refusing to snapshot")

    disk.rename(frozen)
    try:
        _qemu_img("create", "-f", "qcow2", "-F", "qcow2",
                  "-b", str(frozen), str(disk))
        disk.chmod(DISK_MODE)
    except SnapshotError:
        frozen.rename(disk)      # put it back rather than leave a box with no disk
        raise

    snap = Snapshot(
        index=index,
        label=label,
        created=datetime.now().isoformat(timespec="seconds"),
        filename=frozen.name,
    )
    snaps.append(snap)
    _save_all(box_dir, snaps)

    _prune(box_dir, snaps)
    return snap


def _prune(box_dir: Path, snaps: list[Snapshot]) -> list[Snapshot]:
    """Keep the chain at MAX_SNAPSHOT_LAYERS by merging the oldest upward."""
    while len(snaps) > config.MAX_SNAPSHOT_LAYERS:
        oldest = snaps[0]
        survivor = snaps[1]
        oldest_path = _snap_dir(box_dir) / oldest.filename
        survivor_path = _snap_dir(box_dir) / survivor.filename

        golden = _backing_of(oldest_path)
        if golden is None:
            raise SnapshotError(
                f"{oldest_path} has no backing file; refusing to prune blindly"
            )

        # Rebase the survivor onto the golden base. This copies the departing
        # layer's clusters *up* into the survivor. Committing downward instead
        # would write them into the shared golden base and corrupt every other
        # box using that image.
        _qemu_img("rebase", "-b", golden, "-F", "qcow2", str(survivor_path))
        oldest_path.unlink(missing_ok=True)

        snaps.pop(0)
        _save_all(box_dir, snaps)
    return snaps


def rollback(box_dir: Path, disk: Path, index: int,
             size: str | None = None) -> Snapshot:
    """Discard everything above a snapshot and resume from it.

    `size` keeps a rollback from undoing a disk resize. A new overlay inherits
    its backing file's virtual size, so rolling back to a snapshot taken before
    `vm disk` would silently shrink the box back to the old size -- and the box
    would still boot, because the partition table inside that snapshot matches,
    which is precisely what makes the shrink easy to miss. Passing the spec's
    size instead creates the overlay at full size; an overlay larger than its
    backing layer is normal, and the tail is simply unpartitioned until
    `vm disk <name>` grows the filesystem into it.
    """
    snaps = load_all(box_dir)
    target = next((s for s in snaps if s.index == index), None)
    if target is None:
        raise SnapshotError(f"no snapshot {index}")

    target_path = _snap_dir(box_dir) / target.filename
    if not target_path.exists():
        raise SnapshotError(
            f"snapshot {index} is recorded but its file is missing: "
            f"{target_path}. Nothing has been changed."
        )

    # Build the replacement overlay FIRST, under a temporary name.
    #
    # This used to unlink the active disk and every newer snapshot before
    # calling qemu-img, so a create that failed for any reason -- no space, a
    # permission problem, a corrupt backing file -- left the box with no disk at
    # all and the discarded snapshots already gone. Nothing recoverable.
    # `create()` has always been careful to rename the old layer back on
    # failure; this is the same care applied to the destructive direction.
    staged = disk.with_suffix(".qcow2.rollback")
    staged.unlink(missing_ok=True)
    args = ["create", "-f", "qcow2", "-F", "qcow2",
            "-b", str(target_path), str(staged)]
    if size:
        args.append(size)
    try:
        _qemu_img(*args)
    except SnapshotError:
        staged.unlink(missing_ok=True)
        raise

    # Only now is anything discarded. The rename is atomic, so there is no
    # instant at which the box has no disk.
    staged.chmod(DISK_MODE)
    staged.replace(disk)
    for snap in [s for s in snaps if s.index > index]:
        (_snap_dir(box_dir) / snap.filename).unlink(missing_ok=True)

    _save_all(box_dir, [s for s in snaps if s.index <= index])
    return target
