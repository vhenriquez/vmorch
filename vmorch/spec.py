"""The box spec: the source of truth for a box.

The libvirt domain XML is *generated* from this. Nobody hand-edits XML, and
anything found in the XML that is not derivable from here is a bug.

Format is TOML, because it is stdlib-readable (tomllib), supports comments, and
the spec is meant to be edited by hand.

The single most important rule in this file: **a shared folder is read-only
unless the spec explicitly says otherwise.** A missing, empty, malformed or
unrecognised mode resolves to "ro". A rooted agent writing into a shared folder
is the one realistic path back out of the box, so this default must never fail
open.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config

VALID_MODES = {"ro", "rw"}
VALID_SUDO = {"nopasswd", "password", "none"}
VALID_VIA_FROM_HOST = {"filter", "ssh", "vsock"}
VALID_VIA_TO_HOST = {"direct", "ssh"}

# Sharing any of these hands over the host outright: they hold the credentials,
# the code the host executes, or the configuration that decides what it trusts.
FORBIDDEN_FOLDERS = [
    (Path.home(), "the home directory itself"),
    (Path.home() / ".ssh", "SSH keys"),
    (Path.home() / ".gnupg", "GPG keys"),
    (Path.home() / ".aws", "cloud credentials"),
    (Path("/"), "the host root"),
    (Path("/etc"), "host system configuration"),
]


class SpecError(ValueError):
    """The spec is invalid. Never resolved by guessing — always raised."""


@dataclass
class Folder:
    host: Path
    tag: str
    mode: str = "ro"

    @property
    def readonly(self) -> bool:
        # Anything that is not exactly "rw" is read-only. Written this way round
        # deliberately: an unexpected value fails closed, not open.
        return self.mode != "rw"


@dataclass
class Service:
    name: str
    host_port: int
    guest_port: int
    via: str


#: A box name becomes a directory under BOXES_DIR, a libvirt domain name, an
#: nwfilter name and an ssh config Host. Anything outside this set escapes at
#: least one of them.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: Room for the "vmorch-" prefix and the "golden-build-" one, inside libvirt's
#: own limits and a comfortable margin for the generated filenames.
NAME_MAX = 48


def validate_name(name: object) -> str:
    """The single gate every box name passes through.

    This lived inside `parse()`, which is only reached when *re-reading* a
    box.toml -- so neither `vm new` nor the TUI ever ran it, because both build
    a BoxSpec directly. A name was therefore validated only after it had already
    been used to create directories and write files. `BoxSpec(name="../../x")`
    put a box outside BOXES_DIR entirely, and a name containing a quote escaped
    the attribute it was interpolated into in the generated domain XML.

    It is now enforced in BoxSpec.__post_init__, so there is no way to construct
    an invalid spec at all, whichever path builds it.
    """
    if not isinstance(name, str) or not name:
        raise SpecError(f"box name must be a non-empty string, got {name!r}")
    if len(name) > NAME_MAX:
        raise SpecError(
            f"box name {name!r} is {len(name)} characters; the limit is "
            f"{NAME_MAX}"
        )
    if not _NAME_RE.match(name):
        raise SpecError(
            f"box name {name!r} must be letters, digits, - or _, starting with "
            "a letter or digit. The name becomes a directory, a libvirt domain "
            "and an ssh host alias."
        )
    return name


@dataclass
class BoxSpec:
    name: str
    image: str = config.DEFAULT_IMAGE
    cpus: int = config.DEFAULT_CPUS
    memory: str = config.DEFAULT_MEMORY
    disk: str = config.DEFAULT_DISK
    user: str = config.DEFAULT_USER
    #: What the agent user may do with sudo. Per box, because one box
    #: may be doing package work while another only runs code.
    sudo: str = config.AGENT_SUDO
    #: Expose vmx/svm so the box can run its own VMs. Off by default: it is a
    #: large amount of extra KVM surface reachable from the guest. Needed for
    #: the Android emulator (AVD), Genymotion, or anything else built on
    #: hardware virtualisation.
    nested: bool = False
    internet: bool = False
    lan: bool = False
    #: Local networks this box is attached to, by name. Each one becomes a NIC
    #: on a members-only segment shared with the other boxes on it -- the single
    #: deliberate exception to box-to-box isolation, which is why it is a list
    #: of names you opted into rather than a boolean.
    nets: list[str] = field(default_factory=list)
    #: Local nets this box may *forward* on -- the firewall/gateway role.
    #:
    #: It relaxes one control, deliberately and narrowly. Every other member of
    #: a net has its source address pinned, so it cannot impersonate a peer. A
    #: router cannot: forwarding means emitting packets whose source is somebody
    #: else's, which is indistinguishable from spoofing. So the pin is dropped
    #: for this box on these nets only, and nowhere else -- MAC and ARP
    #: anti-spoofing stay on even here.
    #:
    #: Named rather than a boolean because the relaxation has to be visible in
    #: the spec: `vm show` and `vm net ls` can then say which box holds it.
    routes_for: list[str] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)
    from_host: list[Service] = field(default_factory=list)
    to_host: list[Service] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_name(self.name)

    @property
    def domain(self) -> str:
        return config.DOMAIN_PREFIX + self.name

    @property
    def writable_folders(self) -> list[Folder]:
        """Surfaced prominently by `vm show` — an rw grant must never be quiet."""
        return [f for f in self.folders if not f.readonly]


def _parse_mode(raw: object, tag: str) -> str:
    """Resolve a folder mode, failing closed.

    Absent mode -> "ro". Anything unrecognised (empty string, wrong type, a
    typo) raises rather than being quietly downgraded, so a spec that meant to
    grant rw and fumbled it gets an error instead of silent read-only surprise.

    Case and surrounding whitespace are normalised: "RW " is an unambiguous
    intent to grant writes, not a malformed value.

    Defence in depth: Folder.mode defaults to "ro" and Folder.readonly tests
    `mode != "rw"`, so any path that bypasses this function still fails closed.
    """
    if raw is None:
        return "ro"
    if not isinstance(raw, str) or raw.strip().lower() not in VALID_MODES:
        raise SpecError(
            f"folder {tag!r}: mode must be 'ro' or 'rw', got {raw!r}. "
            "Treating as 'ro'."
        )
    return raw.strip().lower()


def _parse_nets(raw: object) -> list[str]:
    """Local network names, de-duplicated, order preserved.

    A string is accepted where a list was meant -- `nets = "lab"` is an obvious
    intent and failing on it teaches nothing. Anything else raises: a net name
    that silently does not apply is a box quietly missing a NIC it was supposed
    to have.
    """
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise SpecError(f"network.nets must be a list of names, got {raw!r}")

    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SpecError(f"network.nets entries must be names, got {item!r}")
        name = item.strip()
        if name not in out:
            out.append(name)
    return out


def _parse_routes_for(raw: object, nets: list[str]) -> list[str]:
    """Nets this box routes on. Must be nets it is actually attached to.

    Routing on a net you are not on is meaningless, and silently accepting it
    would leave a spec that reads as though a firewall exists when none does.
    """
    routers = _parse_nets(raw)
    stray = [n for n in routers if n not in nets]
    if stray:
        raise SpecError(
            f"network.routes_for names {stray} but the box is not attached to "
            f"{'them' if len(stray) > 1 else 'it'}. Add to network.nets first."
        )
    return routers


def _parse_sudo(raw: object) -> str:
    if raw is None:
        return config.AGENT_SUDO
    value = str(raw).strip().lower()
    if value not in VALID_SUDO:
        raise SpecError(
            f"sudo must be one of {sorted(VALID_SUDO)}, got {raw!r}"
        )
    return value


def _parse_folder(raw: dict, index: int) -> Folder:
    if "host" not in raw:
        raise SpecError(f"folder #{index}: missing 'host'")
    host = Path(str(raw["host"])).expanduser()
    tag = str(raw.get("tag") or host.name)

    if not tag.isidentifier() and not tag.replace("-", "_").isidentifier():
        raise SpecError(f"folder #{index}: tag {tag!r} is not a usable mount tag")

    resolved = host.resolve()
    for forbidden, why in FORBIDDEN_FOLDERS:
        if resolved == forbidden.resolve():
            raise SpecError(
                f"folder {tag!r}: refusing to share {resolved} ({why}). "
                "Share a narrower directory instead."
            )

    return Folder(host=resolved, tag=tag, mode=_parse_mode(raw.get("mode"), tag))


#: A service name becomes a systemd unit filename inside the guest and part of
#: a shell command run there as root, so it is held to the same set as a box
#: name rather than taken as-is.
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def validate_service_name(name: str) -> str:
    if not _SERVICE_RE.match(str(name)):
        raise SpecError(
            f"service name {name!r}: letters, digits, - or _ only, starting "
            "with a letter or digit."
        )
    return str(name)


def _parse_service(raw: dict, index: int, valid_via: set[str], default_via: str) -> Service:
    if "name" not in raw:
        raise SpecError(f"service #{index}: missing 'name'")
    name = str(raw["name"])
    if not _SERVICE_RE.match(name):
        raise SpecError(
            f"service name {name!r}: letters, digits, - or _ only, starting "
            "with a letter or digit. The name becomes a systemd unit filename "
            "inside the box."
        )
    via = str(raw.get("via", default_via)).strip().lower()
    if via not in valid_via:
        raise SpecError(
            f"service {name!r}: via must be one of {sorted(valid_via)}, got {via!r}"
        )
    try:
        host_port = int(raw["host"])
        guest_port = int(raw["guest"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(
            f"service {name!r}: needs integer 'host' and 'guest' ports"
        ) from exc
    for label, port in (("host", host_port), ("guest", guest_port)):
        if not 1 <= port <= 65535:
            raise SpecError(
                f"service {name!r}: {label} port {port} is out of range"
            )
    return Service(name=name, host_port=host_port, guest_port=guest_port, via=via)


def parse(data: dict, name: str | None = None) -> BoxSpec:
    """Build a BoxSpec from already-loaded TOML data."""
    name = name or data.get("name")
    if not name:
        raise SpecError("spec has no 'name'")
    name = validate_name(str(name))

    network = data.get("network", {})
    services = data.get("services", {})

    folders = [
        _parse_folder(f, i) for i, f in enumerate(data.get("folders", []), start=1)
    ]

    tags = [f.tag for f in folders]
    duplicates = {t for t in tags if tags.count(t) > 1}
    if duplicates:
        raise SpecError(f"duplicate folder tags: {sorted(duplicates)}")

    return BoxSpec(
        name=name,
        image=str(data.get("image", config.DEFAULT_IMAGE)),
        cpus=int(data.get("cpus", config.DEFAULT_CPUS)),
        memory=str(data.get("memory", config.DEFAULT_MEMORY)),
        disk=str(data.get("disk", config.DEFAULT_DISK)),
        user=str(data.get("user", config.DEFAULT_USER)),
        sudo=_parse_sudo(data.get("sudo")),
        nested=bool(data.get("nested", False)),
        internet=bool(network.get("internet", False)),
        lan=bool(network.get("lan", False)),
        nets=_parse_nets(network.get("nets", [])),
        routes_for=_parse_routes_for(network.get("routes_for", []),
                                     _parse_nets(network.get("nets", []))),
        folders=folders,
        from_host=[
            _parse_service(s, i, VALID_VIA_FROM_HOST, "filter")
            for i, s in enumerate(services.get("from_host", []), start=1)
        ],
        to_host=[
            _parse_service(s, i, VALID_VIA_TO_HOST, "direct")
            for i, s in enumerate(services.get("to_host", []), start=1)
        ],
        packages=[str(p) for p in data.get("packages", [])],
    )


def load(path: Path) -> BoxSpec:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return parse(data)


def _toml_str(value) -> str:
    """A TOML basic string, escaped.

    Hand-rolled emission is fine for a schema this small, but bare f-string
    quoting is not: a shared folder's path may legally contain a backslash or a
    double quote, and either one produced a box.toml the tool could no longer
    read back -- a spec that writes but does not load.
    """
    text = str(value)
    for raw, esc in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"),
                     ("\r", "\\r"), ("\t", "\\t")):
        text = text.replace(raw, esc)
    return f'"{text}"'


def dump(spec: BoxSpec) -> str:
    """Emit a box.toml.

    Hand-rolled rather than pulling in a TOML writer: the schema is small,
    fixed, and benefits from the explanatory comments we can place inline.
    """
    lines = [
        f"name = {_toml_str(spec.name)}",
        f"image = {_toml_str(spec.image)}",
        f"cpus = {spec.cpus}",
        f"memory = {_toml_str(spec.memory)}",
        f"disk = {_toml_str(spec.disk)}",
        f"user = {_toml_str(spec.user)}",
        f"sudo = {_toml_str(spec.sudo)}",
        f"nested = {str(spec.nested).lower()}",
        "",
        "[network]",
        "# internet = true grants the PUBLIC internet only.",
        "# Reaching the router/NAS/other machines needs lan = true.",
        f"internet = {str(spec.internet).lower()}",
        f"lan = {str(spec.lan).lower()}",
        "# nets: local networks this box shares with other boxes. Members-only",
        "# segments -- no gateway, no host, no internet. `vm net ls` lists them.",
        "nets = [" + ", ".join(_toml_str(n) for n in spec.nets) + "]",
        "# routes_for: nets this box FORWARDS on -- the firewall role. Its own",
        "# source-address pin is dropped on those nets, because forwarding means",
        "# sending packets that are not yours. Every other member stays pinned.",
        "routes_for = [" + ", ".join(_toml_str(n) for n in spec.routes_for) + "]",
        "",
    ]

    if spec.folders:
        lines.append("# Folders are READ-ONLY unless mode = \"rw\" is set.")
    for f in spec.folders:
        lines.append("[[folders]]")
        lines.append(f"host = {_toml_str(f.host)}")
        lines.append(f"tag = {_toml_str(f.tag)}")
        lines.append(f"mode = {_toml_str(f.mode)}")
        lines.append("")

    for svc in spec.from_host:
        lines.append("[[services.from_host]]")
        lines.append(f"name = {_toml_str(svc.name)}")
        lines.append(f"host = {svc.host_port}")
        lines.append(f"guest = {svc.guest_port}")
        lines.append(f"via = {_toml_str(svc.via)}")
        lines.append("")

    for svc in spec.to_host:
        lines.append("[[services.to_host]]")
        lines.append(f"name = {_toml_str(svc.name)}")
        lines.append(f"guest = {svc.guest_port}")
        lines.append(f"host = {svc.host_port}")
        lines.append(f"via = {_toml_str(svc.via)}")
        lines.append("")

    if spec.packages:
        pkgs = ", ".join(_toml_str(p) for p in spec.packages)
        lines.append(f"packages = [{pkgs}]")
        lines.append("")

    return "\n".join(lines)
