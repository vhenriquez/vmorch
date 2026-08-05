"""Command-line interface: `vm <subcommand>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (audit, boxes, config, consoletext, golden, images, network,
               snapshots, spec as spec_mod, virsh)
from .spec import BoxSpec


EXAMPLE_CONFIG = """\
# vmorch configuration. Every key is optional; delete what you do not need.
# Applies to both `vm` and `vmtui`. Show the effective values with `vm config`.

# --- storage -----------------------------------------------------------------
# Golden images and box overlays. These must be on FAST storage: a qcow2 chain
# reads unmodified blocks from the base on every access, so a base on a spinning
# disk makes every box slow for its whole life.
#
# They must also NOT contain a hidden directory component. AppArmor denies
# virt-aa-helper any dot-directory, so a box under one fails to start with a
# bare "Permission denied". vmorch refuses such a path rather than let you
# discover that later.
# state_dir      = "~/vmorch"
# bases_dir      = "~/vmorch/bases"
# boxes_dir      = "~/vmorch/boxes"

# Verified downloads, kept so a rebuild needs no network. Cold and written once,
# so this is the one directory that belongs on a slow disk if you have one.
# Defaults under state_dir; point it at a spare drive to keep several hundred
# megabytes per image off your root filesystem.
# download_cache = "~/vmorch/cache"

# --- defaults for `vm new` ---------------------------------------------------
# default_image  = "ubuntu-24.04"
# default_cpus   = 4
# default_memory = "8G"
# default_disk   = "40G"
# default_user   = "agent"

# Snapshot layers kept per box. A fourth commits the oldest into the base.
# max_snapshot_layers = 3

# --- network -----------------------------------------------------------------
# Only change these BEFORE creating any box: existing boxes hold reserved
# addresses on the old subnet and would be stranded.
# mgmt_subnet    = "192.168.150.0/24"
# mgmt_gateway   = "192.168.150.1"
"""


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_images(args) -> None:
    if args.restore_defaults:
        added = images.restore_defaults()
        print(f"restored: {', '.join(added)}" if added
              else "nothing to restore; all shipped images are present")
        print()
    # This is a catalogue of what you CAN use, not an inventory of what is on
    # disk -- built-in entries are always listed. The two columns say what is
    # present locally.
    print(f"  {'DOWNLOADED':<11}{'BASE':<6}{'STATUS':<8}{'NAME':<15}DESCRIPTION")
    for key, entry in sorted(images.catalogue(include_hidden=args.all).items()):
        mark = "yes" if entry.cached.exists() else "-"
        base = "yes" if images.base_path(entry).exists() else "-"
        if entry.hidden:
            flag = "hidden"
        elif entry.broken:
            flag = "BROKEN"
        elif entry.local:
            flag = "local "
        elif entry.verified:
            flag = "      "
        else:
            flag = "new?  "
        print(f"  {mark:<11}{base:<6}{flag:<8}{key:<15}{entry.description}")

    print(f"\n  catalogue: {images.USER_CATALOGUE}"
          "\n  delete an entry's block there to remove it for good"
          "\n"
          "\n  DOWNLOADED = verified original in the cache"
          f" ({config.DOWNLOAD_CACHE})"
          f"\n  BASE       = ready to build boxes from ({config.BASES_DIR})"
          "\n  STATUS     = local (you built it) · new? (untested) · BROKEN")


def describe_removal(plan) -> list[str]:
    """The warning shown before an image is removed.

    Built from the plan rather than re-derived, so the prompt cannot describe
    one set of files while the removal deletes another. Shared with the TUI for
    the same reason.
    """
    lines = [f"{plan.key}  --  {plan.description}", ""]
    if plan.empty:
        if plan.keeps_base:
            return lines + [
                "Nothing left to remove. The golden image is still the base for "
                f"{', '.join(plan.used_by)},", "so it stays.",
            ]
        return lines + ["Nothing to remove: no files on disk, no catalogue entry."]

    lines.append("This deletes:")
    for which, path, label in (
            ("base", plan.base, "golden image (boxes build from this)"),
            ("cached", plan.cached, "cached download (verified original)"),
            ("partial", plan.partial, "partial download")):
        if not path:
            continue
        # A base something still boots from is never deleted, with or without
        # --force. Saying otherwise would make this prompt a lie, and the
        # "frees N" figure roughly double what actually comes back.
        kept = which == "base" and plan.keeps_base
        lines.append(f"  {images.human_size(plan.size_of(which)):>7}  {path}")
        lines.append(f"           {label}"
                     + ("   -- KEPT, still in use" if kept else ""))
    if plan.in_catalogue:
        lines.append(f"           entry [{plan.key}] in {images.USER_CATALOGUE}")
    if plan.freed:
        lines.append("")
        lines.append(f"Frees {images.human_size(plan.freed)}.")

    if plan.used_by:
        lines += ["",
                  f"IN USE by: {', '.join(plan.used_by)}",
                  "  Those boxes are overlays on this base -- their disks hold",
                  "  only their own changes. Removing it breaks them."]
    elif plan.entry.local:
        lines += ["",
                  "This is a golden image you built. There is no download to",
                  "fall back on -- getting it back means `vm golden` again."]
    elif plan.base and plan.cached:
        lines += ["",
                  "Both copies go, so rebuilding this image needs the network."]
    elif plan.base and plan.entry.url and plan.entry.cached.exists():
        # Only say this when a download really is being left behind. Saying it
        # whenever `cached` is unset would promise an offline rebuild for an
        # image whose cache was cleared long ago.
        lines += ["",
                  "The cached download is kept, so the base can be rebuilt",
                  "offline by creating a box from this image."]
    elif plan.base:
        lines += ["",
                  "There is no cached download, so rebuilding this image needs",
                  "the network."]

    if plan.shipped and plan.in_catalogue:
        lines += ["",
                  "Ships with vmorch: `vm images --restore-defaults` re-adds the",
                  "entry later (it does not re-download anything)."]
    return lines


def cmd_rmimage(args) -> None:
    plan = images.plan_removal(args.name, keep_cache=args.keep_cache,
                               keep_entry=args.keep_entry)
    print("\n".join(describe_removal(plan)))
    sys.stdout.flush()      # so the refusal on stderr lands after the warning
    if plan.empty:
        return

    if not args.yes:
        print()
        try:
            if input(f"Remove {plan.key}? [y/N] ").strip().lower() \
                    not in ("y", "yes"):
                print("cancelled")
                return
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled")
            return

    done = images.remove(plan, force=args.force)
    print()
    for path in done.files:
        print(f"removed {path}")
    if done.in_catalogue:
        print(f"removed entry [{done.key}] from {images.USER_CATALOGUE}")
    if plan.used_by and args.force:
        print(f"kept    {images.base_path(plan.entry)}"
              f"  (still in use by {', '.join(plan.used_by)})")


def cmd_new(args) -> None:
    box_spec = BoxSpec(
        name=args.name,
        image=args.image,
        cpus=args.cpus,
        memory=args.memory,
        disk=args.disk,
        internet=args.internet,
        lan=args.lan,
        sudo=args.sudo,
        nested=args.nested,
    )
    print(f"creating {box_spec.name} from {box_spec.image} ...")
    box = boxes.create(box_spec, start=not args.no_start)
    print(f"  ip      {box.ip}")
    print(f"  vsock   cid {box.cid}")
    print(f"  state   {box.state}")
    print(f"\nssh {box.name}    (once cloud-init finishes first boot)")


def cmd_ls(args) -> None:
    all_boxes = boxes.list_boxes()
    if not all_boxes:
        print("no boxes")
        return
    print(f"{'NAME':<18} {'STATE':<10} {'IP':<16} {'NET':<14} SHARES")
    for box in all_boxes:
        if box.spec.internet:
            net = "lan+wan" if box.spec.lan else "internet"
        else:
            net = "isolated"
        shares = ", ".join(
            f"{f.tag}:{'rw' if not f.readonly else 'ro'}" for f in box.spec.folders
        ) or "-"
        print(f"{box.name:<18} {box.state:<10} {box.ip:<16} {net:<14} {shares}")


def cmd_show(args) -> None:
    box = boxes.load(args.name)
    s = box.spec
    print(f"name      {s.name}")
    print(f"domain    {s.domain}")
    print(f"state     {box.state}")
    print(f"image     {s.image}")
    print(f"resources {s.cpus} cpu, {s.memory} ram, {s.disk} disk")
    print(f"address   {box.ip}   (vsock cid {box.cid})")
    print(f"network   internet={s.internet} lan={s.lan}")
    print(f"sudo      {_sudo_label(s.sudo)}")
    print(f"nested    {'yes — box can run its own VMs' if s.nested else 'no'}")

    print("\nfolders")
    if not s.folders:
        print("  (none)")
    for f in s.folders:
        mode = "ro" if f.readonly else "RW"
        print(f"  [{mode}] {f.tag:<12} {f.host}")

    # An rw grant is the one realistic path back to the host. It never appears
    # quietly.
    if s.writable_folders:
        print(f"\n  !! {len(s.writable_folders)} WRITABLE share(s): "
              f"{', '.join(f.tag for f in s.writable_folders)}")
        print("     An agent with root in this box can write to these host paths.")

    print("\nservices")
    if not (s.from_host or s.to_host):
        print("  (none)")
    for svc in s.from_host:
        print(f"  host->box  {svc.name:<12} host:{svc.host_port} -> "
              f"guest:{svc.guest_port}  via {svc.via}")
    for svc in s.to_host:
        print(f"  box->host  {svc.name:<12} guest:{svc.guest_port} -> "
              f"host:{svc.host_port}  via {svc.via}")


SUDO_LABELS = {
    "nopasswd": "passwordless — the agent can become root at will",
    "password": "password required — secret held on the host, not in the box",
    "none":     "no sudo — the agent cannot escalate",
}


def _sudo_label(mode: str) -> str:
    return f"{mode}  ({SUDO_LABELS.get(mode, '?')})"


def cmd_sudo(args) -> None:
    box = boxes.load(args.name)
    if not args.mode:
        print(f"{box.name}: {_sudo_label(box.spec.sudo)}")
        return
    box = boxes.set_sudo(args.name, args.mode)
    print(f"{box.name}: sudo set to {_sudo_label(box.spec.sudo)}")
    print("  cloud-init only runs at first boot, so this takes effect after")
    print(f"  `vm reseed {box.name}`.")


def cmd_password(args) -> None:
    from . import cloudinit
    box = boxes.load(args.name)
    if box.spec.sudo != "password":
        _die(f"{box.name} is in sudo mode {box.spec.sudo!r}; no password is set")
    path = cloudinit.password_path(args.name)
    if not path.exists():
        _die(f"no password stored yet; reseed {box.name} to generate one")
    print(path.read_text().strip())


def cmd_apply(args) -> None:
    box = boxes.apply(args.name)
    print(f"applied. {box.name} is {box.state}")
    if box.note:
        print(f"  {box.note}")


def cmd_start(args) -> None:
    boxes.start(args.name)
    print(f"{args.name} starting")


def cmd_stop(args) -> None:
    boxes.stop(args.name, force=args.force)
    print(f"{args.name} stopping")


def cmd_rm(args) -> None:
    box = boxes.load(args.name)
    if not args.yes:
        print(f"This destroys box {box.name!r} ({box.state}) and its disk.")
        print(f"Its address {box.ip} and vsock cid {box.cid} stay reserved.")
        if input("type the box name to confirm: ").strip() != box.name:
            _die("not confirmed")
    boxes.destroy(args.name)
    print(f"{args.name} destroyed")


def cmd_share(args) -> None:
    # rw is opt-in at the CLI as well as in the spec parser. Two independent
    # layers have to be wrong before a host folder becomes writable.
    mode = "rw" if args.rw else "ro"
    box = boxes.share(args.name, Path(args.path), tag=args.tag, mode=mode)
    tag = args.tag or Path(args.path).expanduser().resolve().name
    print(f"shared {args.path} -> /mnt/{tag} [{mode}] on {box.name}")
    if mode == "rw":
        print("  !! WRITABLE: an agent with root in this box can modify that "
              "host path.")


def cmd_unshare(args) -> None:
    box = boxes.unshare(args.name, args.tag)
    print(f"unshared {args.tag} from {box.name}")


def cmd_logs(args) -> None:
    log = boxes.console_log(args.name)
    if not log.exists():
        _die(f"no console log for {args.name}")
    text = log.read_text(errors="replace")

    # On a terminal the guest's own ANSI colour renders correctly and is worth
    # keeping. Piped to a file or another program it is just noise, so strip it
    # there -- the same convention as `ls --color=auto`. --raw and --clean force
    # either way.
    strip = args.clean or (not sys.stdout.isatty() and not args.raw)
    if strip:
        text = consoletext.sanitize(text)

    for line in text.splitlines()[-args.lines:]:
        print(line)

    if strip and sys.stdout.isatty():
        print(f"\n-- {consoletext.summary(text)}", file=sys.stderr)


def cmd_service(args) -> None:
    guest_port = args.guest_port or args.host_port
    box = boxes.grant_service(args.name, args.service, args.host_port,
                              guest_port, args.via)
    print(f"granted {args.service}: host:{args.host_port} -> "
          f"{box.name} guest:{guest_port} via {args.via}")
    print("  note: this is a hole in the guest->host block. The service's own "
          "auth is the only\n        control behind it.")


def cmd_revoke(args) -> None:
    box = boxes.revoke_service(args.name, args.service)
    print(f"revoked {args.service} from {box.name}")


def cmd_snapshot(args) -> None:
    snap = boxes.snapshot(args.name, args.label)
    print(f"snapshot {snap.index} ({snap.label}) created")
    kept = boxes.list_snapshots(args.name)
    print(f"  {len(kept)}/{config.MAX_SNAPSHOT_LAYERS} layers: "
          f"{', '.join(str(s.index) for s in kept)}")
    print("  note: snapshots do NOT cover shared folders. Anything the agent")
    print("        wrote into an rw share is already on the host and stays.")


def cmd_disk(args) -> None:
    res = boxes.resize_disk(args.name, args.size)
    if res["was"] == res["now"]:
        print(f"{res['name']} disk is already {res['now']}")
    else:
        print(f"{res['name']} disk {res['was']} -> {res['now']}")
    if res["filesystem"]:
        print(f"  filesystem: {res['filesystem']}")
    elif res["running"]:
        print("  box is running but not reachable over ssh; start it and "
              f"re-run `vm disk {res['name']} {res['now']}` to grow the filesystem")
    else:
        print("  box is stopped, so only the virtual disk grew. Start it and "
              f"run `vm disk {res['name']} {res['now']}` to grow the filesystem "
              "into the new space.")


def cmd_snapshots(args) -> None:
    snaps = boxes.list_snapshots(args.name)
    if not snaps:
        print("no snapshots")
        return
    for s in snaps:
        print(f"  {s.index:>3}  {s.created}  {s.label}")


def cmd_rollback(args) -> None:
    snap = boxes.rollback(args.name, args.index)
    print(f"rolled back to {snap.index} ({snap.label})")
    print("  shared folders were NOT rolled back.")


def cmd_mount(args) -> None:
    tags = boxes.sync_mounts(args.name)
    print(f"mounted {len(tags)} share(s) on {args.name}: {', '.join(tags) or '-'}")


def cmd_golden(args) -> None:
    show = lambda m: print(f"  {m}")            # noqa: E731
    if args.from_box:
        if args.packages or args.run or args.image:
            _die("--from-box images an existing box; --image/--packages/--run "
                 "belong to the unattended form")
        path = golden.build_from_box(args.name, args.from_box,
                                     keep_build_box=args.keep_build_box,
                                     progress=show)
    else:
        packages = args.packages.split(",") if args.packages else None
        path = golden.build(
            args.name, from_image=args.image, packages=packages,
            run=args.run or [], keep_build_box=args.keep_build_box,
            progress=show,
        )
    print(f"\ngolden image ready: {path}")
    print(f"use it with:  vm new mybox --image {args.name}")


def cmd_reseed(args) -> None:
    print(f"re-running first-boot configuration on {args.name} ...")
    box = boxes.reseed(args.name)
    print(f"  {box.name} restarting; cloud-init will re-apply user, ssh keys "
          "and mounts")


def _dir_size(path) -> str:
    if not path.exists():
        return "-"
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / 1024**3:.1f}G"


def _free(path) -> str:
    """Free space on the filesystem that would hold `path`.

    Walks up to the first directory that exists: a configured path is often not
    created yet, and "?" is unhelpful when the whole point is to tell you
    whether there is room for it.
    """
    import shutil as _sh
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return f"{_sh.disk_usage(probe).free / 1024**3:.0f}G free"
    except OSError:
        return "?"


def cmd_config(args) -> None:
    import shutil as _sh
    print(f"config file   {config.CONFIG_FILE}"
          f"{'' if config.CONFIG_FILE.exists() else '   (not present; using defaults)'}")
    print()
    print("storage")
    for label, path in (("bases", config.BASES_DIR),
                        ("boxes", config.BOXES_DIR),
                        ("cache", config.DOWNLOAD_CACHE)):
        print(f"  {label:<8} {str(path):<44} {_dir_size(path):>7}  {_free(path)}")
    print()
    print("defaults")
    for label, val in (("image", config.DEFAULT_IMAGE),
                       ("cpus", config.DEFAULT_CPUS),
                       ("memory", config.DEFAULT_MEMORY),
                       ("disk", config.DEFAULT_DISK),
                       ("user", config.DEFAULT_USER),
                       ("snapshot layers", config.MAX_SNAPSHOT_LAYERS)):
        print(f"  {label:<16} {val}")
    print()
    print("network")
    print(f"  management     {config.MGMT_SUBNET}  gateway {config.MGMT_GATEWAY}")
    from . import alloc as _alloc
    entries = _alloc.all_allocations()
    live = sum(1 for e in entries if not e.released)
    pool = config.ALLOC_IP_LAST - config.ALLOC_IP_FIRST + 1
    print(f"  addresses      {live} in use, {len(entries) - live} held in "
          f"reserve, {pool - len(entries)} never used  (pool {pool})")
    if len(entries) >= pool:
        print("                 pool full — the address released longest ago "
              "is recycled on the next box")

    if args.write and not config.CONFIG_FILE.exists():
        config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_FILE.write_text(EXAMPLE_CONFIG)
        print(f"\nwrote a commented starter config to {config.CONFIG_FILE}")
    elif args.write:
        print(f"\n{config.CONFIG_FILE} already exists; not overwriting")


def cmd_audit(args) -> None:
    if args.enable_dns:
        running = [b.name for b in boxes.list_boxes() if b.state == "running"]
        if running:
            print("Enabling DNS logging restarts both libvirt networks, which "
                  "detaches every\nrunning box from its NICs. Currently running: "
                  f"{', '.join(running)}")
            print("\nStop them first, or accept that they will lose networking "
                  "until restarted.")
            if input("continue anyway? [y/N] ").strip().lower() != "y":
                return
        done = network.enable_dns_logging()
        print(f"DNS query logging enabled on: {', '.join(done) or 'already on'}")
        print("Queries now go to the journal; read them with `vm audit`.")
        if running:
            print(f"\nRestart these to restore networking: {', '.join(running)}")
        return

    if args.install:
        path = "/etc/nftables.d/vmorch-audit.nft"
        print("Connection logging needs one root command. Review, then run:\n")
        print(f"  sudo mkdir -p /etc/nftables.d \\\n"
              f"    && sudo tee {path} >/dev/null <<'NFT'\n"
              f"{audit.nft_ruleset()}NFT\n"
              f"  sudo nft -f {path}\n")
        print("To survive a reboot, load it from /etc/nftables.conf or a unit.")
        print("To remove entirely:  sudo nft delete table inet vmorch_audit")
        return

    state = audit.available()
    if not state["dns"]:
        print("! DNS logging is OFF. Enable with:  vm audit --enable-dns\n")
    if not state["connections"]:
        print("! Connection logging is OFF. Set it up with:  vm audit --install\n")

    events = audit.collect(since=args.since, box=args.box,
                           blocked_only=args.blocked)
    if not events:
        print(f"no events since {args.since}")
        return

    print(f"{'WHEN':<20}{'BOX':<14}{'KIND':<7}DETAIL")
    for e in events:
        if e.kind == "dns":
            detail = e.detail
        else:
            name = f"  ({e.hostname})" if e.hostname else ""
            detail = f"{e.verdict:<16}{e.proto:<5}{e.src} -> {e.dst}{name}"
        print(f"{e.when:<20}{e.box:<14}{e.kind:<7}{detail}")
    print(f"\n  {len(events)} events since {args.since}"
          f"{' (blocked only)' if args.blocked else ''}")


def cmd_net(args) -> None:
    if args.prune:
        live = {b.name for b in boxes.list_boxes()}
        removed = network.prune_reservations(live)
        print(f"removed {len(removed)} stale DHCP reservation(s)"
              + (f": {', '.join(removed)}" if removed else ""))
        return
    created = network.ensure_base()
    print(f"management network {config.MGMT_NET}: "
          f"{'created' if created else 'already present'}")
    print(f"  subnet  {config.MGMT_SUBNET}  gateway {config.MGMT_GATEWAY}")
    print("  filters vmorch-mgmt-filter, vmorch-wan-lan, vmorch-wan-nolan")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vm", description="Disposable, reconfigurable agent sandbox VMs"
    )
    sub = p.add_subparsers(dest="command", required=True)

    im = sub.add_parser("images", help="list the image catalogue")
    im.add_argument("--all", action="store_true",
                    help="include entries hidden in images.toml")
    im.add_argument("--restore-defaults", action="store_true",
                    help="re-add shipped images you have deleted")
    im.set_defaults(func=cmd_images)

    ri = sub.add_parser("rmimage",
                        help="remove an image: files, cache and catalogue entry")
    ri.add_argument("name", help="image key, as shown by `vm images`")
    ri.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt")
    ri.add_argument("--keep-cache", action="store_true",
                    help="delete the golden image but keep the verified "
                         "download, so it can be rebuilt without the network")
    ri.add_argument("--keep-entry", action="store_true",
                    help="delete the files but leave the block in images.toml")
    ri.add_argument("--force", action="store_true",
                    help="proceed even if boxes use this image; their base file "
                         "is kept regardless, so they keep working")
    ri.set_defaults(func=cmd_rmimage)

    new = sub.add_parser("new", help="create a box")
    new.add_argument("name")
    new.add_argument("--image", default=config.DEFAULT_IMAGE)
    new.add_argument("--cpus", type=int, default=config.DEFAULT_CPUS)
    new.add_argument("--memory", default=config.DEFAULT_MEMORY)
    new.add_argument("--disk", default=config.DEFAULT_DISK)
    new.add_argument("--internet", action="store_true",
                     help="grant the public internet (not the LAN)")
    new.add_argument("--lan", action="store_true",
                     help="also grant local network access")
    new.add_argument("--sudo", default=config.AGENT_SUDO,
                     choices=sorted(spec_mod.VALID_SUDO),
                     help="agent's sudo: nopasswd (default), password, none")
    new.add_argument("--nested", action="store_true",
                     help="expose vmx/svm so the box can run its own VMs "
                          "(needed for the Android emulator); more attack surface")
    new.add_argument("--no-start", action="store_true")
    new.set_defaults(func=cmd_new)

    sub.add_parser("ls", help="list boxes, stopped and running").set_defaults(
        func=cmd_ls)

    show = sub.add_parser("show", help="show a box's full grant set")
    show.add_argument("name")
    show.set_defaults(func=cmd_show)

    ap = sub.add_parser("apply", help="regenerate a box from its spec")
    ap.add_argument("name")
    ap.set_defaults(func=cmd_apply)

    st = sub.add_parser("start")
    st.add_argument("name")
    st.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop")
    sp.add_argument("name")
    sp.add_argument("--force", action="store_true", help="pull the plug")
    sp.set_defaults(func=cmd_stop)

    rm = sub.add_parser("rm", help="destroy a box and its disk")
    rm.add_argument("name")
    rm.add_argument("--yes", action="store_true")
    rm.set_defaults(func=cmd_rm)

    sh = sub.add_parser("share", help="grant a box a host folder (read-only)")
    sh.add_argument("name")
    sh.add_argument("path")
    sh.add_argument("--tag", help="mount tag; defaults to the directory name")
    sh.add_argument("--rw", action="store_true",
                    help="grant WRITE access (default is read-only)")
    sh.set_defaults(func=cmd_share)

    un = sub.add_parser("unshare", help="revoke a shared folder")
    un.add_argument("name")
    un.add_argument("tag")
    un.set_defaults(func=cmd_unshare)

    sv = sub.add_parser("service", help="grant a box access to a host service")
    sv.add_argument("name")
    sv.add_argument("service", help="e.g. ollama")
    sv.add_argument("--host-port", type=int, required=True)
    sv.add_argument("--guest-port", type=int)
    sv.add_argument("--via", default="filter",
                    choices=sorted(spec_mod.VALID_VIA_FROM_HOST))
    sv.set_defaults(func=cmd_service)

    rv = sub.add_parser("revoke", help="revoke a granted service")
    rv.add_argument("name")
    rv.add_argument("service")
    rv.set_defaults(func=cmd_revoke)

    sn = sub.add_parser("snapshot", help="freeze the box's disk state")
    sn.add_argument("name")
    sn.add_argument("label", nargs="?")
    sn.set_defaults(func=cmd_snapshot)

    dk = sub.add_parser("disk", help="grow a box's disk (never shrinks)")
    dk.add_argument("name")
    dk.add_argument("size",
                    help="new size, e.g. 60G, or +20G to add to the current size")
    dk.set_defaults(func=cmd_disk)

    sl = sub.add_parser("snapshots", help="list snapshots")
    sl.add_argument("name")
    sl.set_defaults(func=cmd_snapshots)

    rb = sub.add_parser("rollback", help="rewind to a snapshot")
    rb.add_argument("name")
    rb.add_argument("index", type=int)
    rb.set_defaults(func=cmd_rollback)

    rs = sub.add_parser("reseed",
                        help="re-run first-boot config to repair a box")
    rs.add_argument("name")
    rs.set_defaults(func=cmd_reseed)

    au = sub.add_parser("audit", help="what boxes looked up and connected to")
    au.add_argument("--since", default="-24h",
                    help="journalctl time spec, e.g. -1h, today, '2026-08-01'")
    au.add_argument("--box", help="only this box")
    au.add_argument("--blocked", action="store_true",
                    help="only refused attempts, usually the interesting ones")
    au.add_argument("--install", action="store_true",
                    help="print the one root command for connection logging")
    au.add_argument("--enable-dns", action="store_true",
                    help="turn on DNS query logging (restarts the networks)")
    au.set_defaults(func=cmd_audit)

    cf = sub.add_parser("config", help="show paths, defaults and disk usage")
    cf.add_argument("--write", action="store_true",
                    help="write a commented starter config file")
    cf.set_defaults(func=cmd_config)

    sd = sub.add_parser("sudo", help="show or change the agent's sudo rights")
    sd.add_argument("name")
    sd.add_argument("mode", nargs="?", choices=sorted(spec_mod.VALID_SUDO))
    sd.set_defaults(func=cmd_sudo)

    pw = sub.add_parser("password",
                        help="print the sudo password (password mode only)")
    pw.add_argument("name")
    pw.set_defaults(func=cmd_password)

    mt = sub.add_parser("mount", help="re-mount a box's shared folders")
    mt.add_argument("name")
    mt.set_defaults(func=cmd_mount)

    gd = sub.add_parser("golden", help="build a base image with software baked in")
    gd.add_argument("name", help="name for the new image")
    gd.add_argument("--from-box", metavar="BOX",
                    help="freeze an existing box you set up by hand "
                         "(it must be stopped; it is not modified)")
    gd.add_argument("--image", help=f"source image (default {config.DEFAULT_IMAGE})")
    gd.add_argument("--packages",
                    help="comma-separated; default: "
                         + ",".join(golden.DEFAULT_PACKAGES))
    gd.add_argument("--run", action="append", metavar="CMD",
                    help="extra command to run in the image; repeatable")
    gd.add_argument("--keep-build-box", action="store_true",
                    help="leave the temporary build box for inspection")
    gd.set_defaults(func=cmd_golden)

    lg = sub.add_parser("logs", help="show a box's console log")
    lg.add_argument("name")
    lg.add_argument("-n", "--lines", type=int, default=40)
    lg.add_argument("--clean", action="store_true",
                    help="strip terminal control codes even on a tty")
    lg.add_argument("--raw", action="store_true",
                    help="keep control codes even when piped")
    lg.set_defaults(func=cmd_logs)

    nt = sub.add_parser("net", help="ensure the management network exists")
    nt.add_argument("--prune", action="store_true",
                    help="drop DHCP reservations for boxes that no longer exist")
    nt.set_defaults(func=cmd_net)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (boxes.BoxError, golden.GoldenError, images.ImageError,
            spec_mod.SpecError, snapshots.SnapshotError, virsh.VirshError) as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
