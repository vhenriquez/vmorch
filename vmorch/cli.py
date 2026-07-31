"""Command-line interface: `vm <subcommand>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import boxes, config, images, network, snapshots, spec as spec_mod, virsh
from .spec import BoxSpec


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_images(args) -> None:
    for key, entry in sorted(images.CATALOGUE.items()):
        mark = "cached" if entry.cached.exists() else "      "
        base = "base" if images.base_path(entry).exists() else "    "
        print(f"  {mark} {base}  {key:<14} {entry.description}")


def cmd_new(args) -> None:
    box_spec = BoxSpec(
        name=args.name,
        image=args.image,
        cpus=args.cpus,
        memory=args.memory,
        disk=args.disk,
        internet=args.internet,
        lan=args.lan,
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


def cmd_apply(args) -> None:
    box = boxes.apply(args.name)
    print(f"applied. {box.name} is {box.state}")


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
    lines = text.splitlines()
    for line in lines[-args.lines:]:
        print(line)


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


def cmd_net(args) -> None:
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

    sub.add_parser("images", help="list the image catalogue").set_defaults(
        func=cmd_images)

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

    sl = sub.add_parser("snapshots", help="list snapshots")
    sl.add_argument("name")
    sl.set_defaults(func=cmd_snapshots)

    rb = sub.add_parser("rollback", help="rewind to a snapshot")
    rb.add_argument("name")
    rb.add_argument("index", type=int)
    rb.set_defaults(func=cmd_rollback)

    lg = sub.add_parser("logs", help="show a box's console log")
    lg.add_argument("name")
    lg.add_argument("-n", "--lines", type=int, default=40)
    lg.set_defaults(func=cmd_logs)

    sub.add_parser("net", help="ensure the management network exists").set_defaults(
        func=cmd_net)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (boxes.BoxError, images.ImageError, spec_mod.SpecError,
            snapshots.SnapshotError, virsh.VirshError) as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
