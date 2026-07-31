"""Generate libvirt domain XML from a box spec.

The XML is derived state. The spec is the source of truth, and `vm apply`
regenerates this whole document rather than patching it -- which is what keeps
reconfiguration honest.

Three things here are load-bearing and easy to get wrong:

1. **Shared-memory backing is emitted for every box, always**, even one with no
   shared folders. virtiofs requires it, and adding it to a box that lacks it is
   a memory-topology change rather than a device hotplug. Paying for it upfront
   is what makes "share a new folder with an existing box" a cheap operation
   later.

2. **A <vsock> device is emitted for every box, always**, for the same reason.
   It is inert until something listens on it.

3. **Two NICs, never one.** Management lives on an isolated network so that
   `ssh <name>` keeps working when internet = false. Wiring SSH through the NAT
   NIC would mean revoking internet also locks the owner out of the box.
"""

from __future__ import annotations

from . import config
from .spec import BoxSpec


def _memory_kib(memory: str) -> int:
    text = memory.strip().upper()
    multipliers = {"G": 1024 * 1024, "M": 1024, "K": 1}
    if text and text[-1] in multipliers:
        return int(float(text[:-1]) * multipliers[text[-1]])
    return int(float(text)) * 1024 * 1024      # bare number means GiB


def _filesystem_xml(spec: BoxSpec) -> str:
    parts = []
    for folder in spec.folders:
        # <readonly/> is the enforcement point. It is emitted unless the spec
        # explicitly said rw; the guest also mounts ro, so two layers have to
        # fail before an agent can write to the host.
        readonly = "\n      <readonly/>" if folder.readonly else ""
        parts.append(
            f"""    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtiofs'/>
      <source dir='{folder.host}'/>
      <target dir='{folder.tag}'/>{readonly}
    </filesystem>"""
        )
    return "\n".join(parts)


def _interfaces_xml(spec: BoxSpec, mac: str, wan_mac: str) -> str:
    box_filter = f'vmorch-box-{spec.name}'
    # nic0: management. Always present. <port isolated='yes'/> stops this box
    # reaching any other box on the same bridge -- boxes are single-agent and
    # have no reason to talk to each other. libvirt 12.0.0 supports it.
    interfaces = [
        f"""    <interface type='network'>
      <source network='{config.MGMT_NET}'/>
      <mac address='{mac}'/>
      <model type='virtio'/>
      <port isolated='yes'/>
      <filterref filter='{box_filter}'/>
    </interface>"""
    ]

    # nic1: internet. Present only when granted. virtio NICs hot-plug, so this
    # can be attached and detached on a running box.
    if spec.internet:
        wan_filter = "vmorch-wan-lan" if spec.lan else "vmorch-wan-nolan"
        interfaces.append(
            f"""    <interface type='network'>
      <source network='{config.NAT_NET}'/>
      <mac address='{wan_mac}'/>
      <model type='virtio'/>
      <filterref filter='{wan_filter}'/>
    </interface>"""
        )

    return "\n".join(interfaces)


def build(spec: BoxSpec, disk_path: str, mac: str, wan_mac: str, cid: int,
          console_log: str, seed_iso: str | None = None,
          uuid: str | None = None) -> str:
    """Render the complete domain XML for a box."""
    seed = ""
    if seed_iso:
        seed = f"""    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{seed_iso}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
"""

    filesystems = _filesystem_xml(spec)
    if filesystems:
        filesystems += "\n"

    # Redefining an existing domain requires its current UUID: libvirt rejects
    # the XML outright otherwise, which would make `vm apply` work exactly once
    # -- at creation -- and fail on every reconfiguration after that.
    uuid_line = f"\n  <uuid>{uuid}</uuid>" if uuid else ""

    return f"""<domain type='kvm'>
  <name>{spec.domain}</name>{uuid_line}
  <memory unit='KiB'>{_memory_kib(spec.memory)}</memory>
  <currentMemory unit='KiB'>{_memory_kib(spec.memory)}</currentMemory>
  <vcpu placement='static'>{spec.cpus}</vcpu>

  <!-- Required by virtiofs. Emitted for every box, shared folders or not, so
       that adding a folder later is a device hotplug rather than surgery. -->
  <memoryBacking>
    <source type='memfd'/>
    <access mode='shared'/>
  </memoryBacking>

  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>

  <features>
    <acpi/>
    <apic/>
  </features>

  <cpu mode='host-passthrough' check='none' migratable='on'/>

  <clock offset='utc'>
    <timer name='rtc' tickpolicy='catchup'/>
    <timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/>
  </clock>

  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>

  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>

    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <!-- relabel='no' keeps the whole backing chain owned by the box's owner.
           Otherwise libvirt chowns every layer to libvirt-qemu and leaves it
           that way, and snapshot pruning - which rebases those layers with
           qemu-img as the owner - fails with "Permission denied". qemu reaches
           them through the ACL on the state directory instead. -->
      <source file='{disk_path}'>
        <seclabel model='dac' relabel='no'/>
      </source>
      <target dev='vda' bus='virtio'/>
    </disk>
{seed}
{filesystems}{_interfaces_xml(spec, mac, wan_mac)}

    <!-- Host<->guest channel that does not use IP at all. Inert until a relay
         listens. CIDs are never reused, so a stale host-side relay can never
         be handed a different box. -->
    <vsock model='virtio'>
      <cid auto='no' address='{cid}'/>
    </vsock>

    <!-- Serial console on a pty, with libvirt tee-ing it to a log file.

         NOT <console type='file'>. That makes the log file libvirt's own
         chardev *source*, which libvirtd creates as root:root 0600 - leaving
         the owner of the box unable to read their own boot output. A
         <seclabel relabel='no'/> does not help: it only controls whether
         libvirt then chowns the file to the qemu user, so the file stays root
         either way. Verified directly, both with and without it.

         <log> is a different path: libvirt opens it for append rather than
         creating it, so a file we pre-create keeps our ownership and stays
         readable. boxes._ensure_console_log makes it before every start.

         append='on' keeps history across restarts, which is when you most want
         it. Using a pty for the console itself also makes `virsh console`
         work, which <console type='file'> does not. -->
    <console type='pty'>
      <log file='{console_log}' append='on'/>
      <target type='serial' port='0'/>
    </console>

    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>

    <!-- No clipboard, no SPICE agent, no shared display: those are the
         conveniences that quietly re-open the boundary. -->
    <graphics type='vnc' port='-1' listen='127.0.0.1'/>
    <video>
      <model type='virtio'/>
    </video>

    <rng model='virtio'>
      <backend model='random'>/dev/urandom</backend>
    </rng>

    <memballoon model='none'/>
  </devices>
</domain>
"""
