"""Paths, constants and host-specific facts.

Values here were established by the host capability check (2026-07-31); see
docs/host-capability-check.md
for why each one is what it is.
"""

from pathlib import Path

# --- domains -----------------------------------------------------------------

# Every domain this tool creates is prefixed. Nothing outside the prefix is ever
# touched: the owner's authorization is scoped to what we create.
DOMAIN_PREFIX = "vmorch-"

LIBVIRT_URI = "qemu:///system"

# --- management network ------------------------------------------------------

# 192.168.150.0/24 was verified free: the LAN is 192.168.1.0/24, and the host
# also carries 192.168.4.0/24 and libvirt's default 192.168.122.0/24.
MGMT_NET = "vmorch-mgmt"
MGMT_BRIDGE = "virbr-vmorch"          # 12 chars, under the 15-char IFNAMSIZ limit
MGMT_SUBNET = "192.168.150.0/24"
MGMT_GATEWAY = "192.168.150.1"
MGMT_NETMASK = "255.255.255.0"
MGMT_DHCP_START = "192.168.150.10"
MGMT_DHCP_END = "192.168.150.254"

# First address handed out by our own allocator. Kept clear of the gateway.
ALLOC_IP_FIRST = 10
ALLOC_IP_LAST = 254

# The NAT network used for `internet = true`. libvirt ships this.
NAT_NET = "default"

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
# ~/vmorch is a 7200rpm HDD; root is NVMe. qcow2 backing chains read
# unmodified blocks from the base on every access, so bases and overlays must
# live on NVMe or every box is slow for its whole life. Only the cold download
# archive goes on spinning disk.

STATE_DIR = Path.home() / ".local" / "share" / "vmorch"   # NVMe
BOXES_DIR = STATE_DIR / "boxes"
BASES_DIR = STATE_DIR / "bases"                            # golden images, NVMe
ALLOC_FILE = STATE_DIR / "allocations.json"

DOWNLOAD_CACHE = Path("~/vmorch/cloud_images")  # HDD, cold

# --- ssh ---------------------------------------------------------------------

SSH_DIR = Path.home() / ".ssh"
SSH_CONFIG = SSH_DIR / "config"
SSH_CONFIG_D = SSH_DIR / "config.d"
SSH_FRAGMENT = SSH_CONFIG_D / "vmorch"        # this tool owns this file entirely
SSH_KNOWN_HOSTS = SSH_DIR / "vmorch_known_hosts"
SSH_INCLUDE_LINE = "Include config.d/vmorch"

# --- defaults ----------------------------------------------------------------

DEFAULT_CPUS = 4
DEFAULT_MEMORY = "8G"
DEFAULT_DISK = "40G"
DEFAULT_USER = "agent"          # one shared agent user across all boxes
DEFAULT_IMAGE = "debian-12"

# Snapshot layers above the box overlay. Creating a 4th commits the oldest down.
MAX_SNAPSHOT_LAYERS = 3
