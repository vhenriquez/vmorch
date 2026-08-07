"""Paths, constants and host-specific facts.

Values here were established by the host capability check (2026-07-31); see
docs/host-capability-check.md
for why each one is what it is.
"""

import os
import tomllib
from pathlib import Path

# --- user configuration ------------------------------------------------------
#
# Everything below can be overridden from a TOML file. It deliberately does NOT
# live in the state directory, because the state directory is one of the things
# it configures.

CONFIG_FILE = Path(
    os.environ.get("VMORCH_CONFIG",
                   Path.home() / ".config" / "vmorch" / "config.toml")
)


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:                          # noqa: BLE001
        raise SystemExit(f"config error: {CONFIG_FILE}: {exc}")


_CFG = _load()


def _path(key: str, default: Path) -> Path:
    raw = _CFG.get(key)
    return Path(str(raw)).expanduser() if raw else default


def _value(key: str, default):
    got = _CFG.get(key, default)
    return type(default)(got) if not isinstance(got, type(default)) else got

# --- domains -----------------------------------------------------------------

# Every domain this tool creates is prefixed. Nothing outside the prefix is ever
# touched: the owner's authorization is scoped to what we create.
DOMAIN_PREFIX = "vmorch-"

LIBVIRT_URI = "qemu:///system"

# --- management network ------------------------------------------------------

# Defaults sit in 10.0.0.0/8 rather than 192.168/16, because home and office
# LANs overwhelmingly live in the latter and libvirt's own default network is
# already at 192.168.122.0/24. A clash here is not cosmetic: the management
# network is what `ssh <box>` uses, so it must not collide with a route the host
# already has.
#
# Check yours with `ip route` before changing these, and change them only BEFORE
# creating any box -- existing boxes hold reserved addresses on the old subnet.
MGMT_NET = "vmorch-mgmt"
MGMT_BRIDGE = "virbr-vmorch"          # 12 chars, under the 15-char IFNAMSIZ limit
MGMT_SUBNET = _value("mgmt_subnet", "10.150.0.0/24")
MGMT_GATEWAY = _value("mgmt_gateway", "10.150.0.1")


def _netmask(cidr: str) -> str:
    """Dotted netmask for a CIDR. Derived, never assumed.

    MGMT_SUBNET is configurable, so a hardcoded 255.255.255.0 silently produced
    a network that disagreed with its own address range the moment anyone used
    a prefix other than /24.
    """
    bits = int(cidr.split("/")[1])
    packed = ((1 << bits) - 1) << (32 - bits)
    return ".".join(str((packed >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _host_range(cidr: str, first: int, last: int) -> tuple[str, str]:
    """(first, last) usable host address in `cidr`, clamped to the prefix.

    The DHCP range used to be three literal 10.150.0.x strings next to a
    configurable subnet. Overriding mgmt_subnet therefore emitted a <range>
    outside its own <ip>, which libvirt rejects outright -- so a documented
    config key could not actually be used.
    """
    base = [int(o) for o in cidr.split("/")[0].split(".")]
    bits = int(cidr.split("/")[1])
    net = (base[0] << 24) | (base[1] << 16) | (base[2] << 8) | base[3]
    net &= ((1 << bits) - 1) << (32 - bits)
    size = 1 << (32 - bits)
    lo, hi = net + min(first, size - 2), net + min(last, size - 2)

    def dotted(n: int) -> str:
        return ".".join(str((n >> s) & 0xFF) for s in (24, 16, 8, 0))
    return dotted(lo), dotted(hi)


MGMT_NETMASK = _netmask(MGMT_SUBNET)

# First address handed out by our own allocator. Kept clear of the gateway.
ALLOC_IP_FIRST = 10
ALLOC_IP_LAST = 254

MGMT_DHCP_START, MGMT_DHCP_END = _host_range(
    MGMT_SUBNET, ALLOC_IP_FIRST, ALLOC_IP_LAST)

# The NAT network used for `internet = true`. libvirt ships this.
NAT_NET = "default"

# --- local networks ----------------------------------------------------------
#
# Members-only segments boxes can be attached to, one /24 taken from this pool
# per network. Sits clear of the management network, and in 10.0.0.0/8 for the
# same reason it does. Nothing routes here -- these segments have no gateway --
# so a clash would only ever confuse a guest's own routing table, but there is
# no reason to invite one.
#
# The prefix length is a real limit, not decoration: a /20 holds 16 /24s, and
# `vm net create` refuses the 17th rather than quietly handing out a subnet
# outside the pool it was told to use.
LOCALNET_POOL = _value("localnet_pool", "10.150.16.0/20")

# RFC1918 + link-local. Dropped on the internet NIC unless `lan = true`.
# The real LAN (192.168.1.0/24) falls inside 192.168/16, so this does bite.
PRIVATE_RANGES = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
]

# --- vsock -------------------------------------------------------------------

# CIDs 0-2 are reserved by the vsock spec (hypervisor/local/host).
CID_FIRST = 100

# --- storage -----------------------------------------------------------------
#
# Everything lives under one visible directory in $HOME by default, so a fresh
# clone works with no configuration at all. Each path can be pointed elsewhere
# from config.toml.
#
# **Put bases and boxes on your fastest disk.** A qcow2 backing chain reads
# unmodified blocks from the base on every access, so a base on a spinning disk
# makes every box built from it slow for its whole life. The download cache is
# the opposite: cold, written once, read only when a base is rebuilt -- that is
# the one worth sending to a slow spare drive if you have one.

# NOT under ~/.local, and not anywhere hidden. Ubuntu's AppArmor profile for
# virt-aa-helper carries:
#
#     audit deny @{HOME}/.* mrwkl,
#     audit deny @{HOME}/.*/** mrwkl,
#     @{HOME}/** r,
#
# virt-aa-helper is what generates each domain's AppArmor profile, so if it
# cannot read a disk image, qemu never gets permission for it and the box fails
# to start with a bare "Permission denied" that looks like a DAC problem and is
# not. Any dot-directory under $HOME is off limits; a visible one is fine.
STATE_DIR = _path("state_dir", Path.home() / "vmorch")     # NVMe, non-hidden
BASES_DIR = _path("bases_dir", STATE_DIR / "bases")        # golden images, NVMe
BOXES_DIR = _path("boxes_dir", STATE_DIR / "boxes")
ALLOC_FILE = STATE_DIR / "allocations.json"

# Verified downloads, kept so rebuilding a base needs no network.
#
# Under the state dir by default, because a default has to work on a machine
# that is not the author's. This once pointed at a mount that existed on exactly
# one host, and anywhere else the first `vm new` died with a bare PermissionError
# from the mkdir, before any of the tool's own error handling ran.
DOWNLOAD_CACHE = _path("download_cache", STATE_DIR / "cloud_images")

# The AppArmor rule above is not advice, it is a hard constraint, so anything
# holding a disk image is checked rather than trusted.
for _label, _dir in (("state_dir", STATE_DIR), ("bases_dir", BASES_DIR),
                     ("boxes_dir", BOXES_DIR)):
    if any(part.startswith(".") for part in _dir.parts):
        raise SystemExit(
            f"config error: {_label} = {_dir}\n"
            "  A path with a hidden component cannot hold VM disks: AppArmor\n"
            "  denies virt-aa-helper any dot-directory, so boxes fail to start\n"
            f"  with a bare 'Permission denied'. Edit {CONFIG_FILE}."
        )


def ensure_dir(path: Path, key: str) -> Path:
    """mkdir -p, turning a bad configured path into a sentence rather than a
    traceback.

    Every one of these directories can be pointed anywhere from config.toml, so
    "it does not exist and I may not create it" is a configuration mistake, not
    a bug -- and the raw OSError names only the first missing parent, which is
    rarely the part that was got wrong.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"cannot create {key} = {path}\n"
            f"  {exc.strerror}: {getattr(exc, 'filename', path)}\n"
            f"  Create it yourself, or point {key} somewhere writable in\n"
            f"  {CONFIG_FILE}."
        ) from None
    return path


#: Every directory the tool needs to exist before it can do anything.
#: Keyed by its config.toml name so a failure names the key you would edit.
STATE_DIRS = {
    "state_dir": STATE_DIR,
    "bases_dir": BASES_DIR,
    "boxes_dir": BOXES_DIR,
    "download_cache": DOWNLOAD_CACHE,
}


def ensure_state_dirs() -> list[Path]:
    """Create every state directory that does not exist yet.

    Called before any operation that writes, so a fresh clone -- or a
    config.toml pointing somewhere new -- works without the user first having to
    guess which directories to mkdir. Returns the ones that had to be created,
    so the caller can say so rather than doing it silently.

    Creation is not the same as being usable: ensure_dir turns a path we may not
    write into one clear sentence naming the key to edit, instead of an OSError
    whose filename is the first missing parent rather than the part got wrong.
    """
    created = []
    for key, path in STATE_DIRS.items():
        if not path.exists():
            created.append(path)
        ensure_dir(path, key)
    return created


# --- ssh ---------------------------------------------------------------------

SSH_DIR = Path.home() / ".ssh"
SSH_CONFIG = SSH_DIR / "config"
SSH_CONFIG_D = SSH_DIR / "config.d"
SSH_FRAGMENT = SSH_CONFIG_D / "vmorch"        # this tool owns this file entirely
SSH_KNOWN_HOSTS = SSH_DIR / "vmorch_known_hosts"
SSH_INCLUDE_LINE = "Include config.d/vmorch"

# --- defaults ----------------------------------------------------------------

DEFAULT_CPUS = _value("default_cpus", 4)
DEFAULT_MEMORY = _value("default_memory", "8G")
DEFAULT_DISK = _value("default_disk", "40G")
DEFAULT_USER = _value("default_user", "agent")   # shared across all boxes
# Ubuntu, not Debian. The debian-12-genericcloud image was tested on 2026-07-31
# and cloud-init never runs on it: zero cloud-init units in the boot, hostname
# left at "localhost", and ssh.service fails because no host keys are generated.
# The identical seed ISO drives Ubuntu 24.04 correctly, so the pipeline is fine
# and the image is not. See the catalogue note in images.py.
DEFAULT_IMAGE = _value("default_image", "ubuntu-24.04")

# Snapshot layers above the box overlay. Creating a 4th commits the oldest down.
#: What the agent user gets for privilege escalation inside its own box.
#:
#:   "nopasswd"  passwordless sudo to root (default). The agent owns the box,
#:               which is the premise -- the boundary is the VM, not in-guest
#:               privilege.
#:   "none"      no sudo at all. Raises an unprivileged in-guest compromise
#:               (a hostile dependency, a prompt injection) from one step to
#:               root into two, which matters because most paths at the
#:               hypervisor need kernel context. Costs the agent the ability to
#:               install packages itself -- bake them into a golden image.
#:
#: A password-protected sudo is deliberately NOT offered: the password would
#: have to be reachable by the agent, which makes it decoration, or not, which
#: is "none" with worse failure modes.
#:
#: vmorch keeps its own root path either way, so `vm share`, `vm service` and
#: `vm golden` work in both modes. Changing this on an existing box needs
#: `vm reseed` -- cloud-init only runs at first boot.
AGENT_SUDO = _value("agent_sudo", "nopasswd")

#: Emit a <vsock> device on every box. It was added so `via: vsock` service
#: sharing could be turned on later without a stop/start -- but that mechanism
#: is not built, so today it is a host<->guest transport nothing uses. Set false
#: to drop it: every device removed is emulated code the guest cannot reach.
VSOCK_DEVICE = _value("vsock_device", True)

MAX_SNAPSHOT_LAYERS = _value("max_snapshot_layers", 3)
