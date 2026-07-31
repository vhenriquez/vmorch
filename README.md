# vmorch

Disposable, reconfigurable VM sandboxes for agents on a single workstation.

An agent gets **root inside the box**. The **host is exposed only by explicit
grant** — named folders (read-only by default), specific host services, and the
public internet. Nothing else.

Design notes live alongside this repo at
the design notes.

## Quick start

```bash
./vm new agent-alpha                  # isolated: no internet, no host access
ssh agent-alpha                       # works about a minute later

./vm share agent-alpha ~/code/thing   # read-only by default
./vm share agent-alpha ~/scratch --rw # writes reach the host: deliberate

./vm service agent-alpha ollama --host-port 11434   # GPU compute, no GPU handover

./vm stop agent-alpha
./vm snapshot agent-alpha before-experiment
./vm start agent-alpha
# ... let the agent break things ...
./vm stop agent-alpha && ./vm rollback agent-alpha 1
```

`./vm ls` lists stopped boxes alongside running ones. `./vm show <box>` prints
the full grant set, with writable shares called out loudly.

## What the boundary is

A box reaches **only** what its spec grants. Verified from inside a running box,
not inferred from config:

| | isolated | `--internet` |
|---|---|---|
| public internet | blocked | reachable |
| DNS | no resolver | resolves |
| LAN (router, NAS) | blocked | **blocked** |
| host services | blocked | blocked |
| granted host service | reachable | reachable |
| other boxes | blocked | blocked |

`--internet` means the *public internet only*. Reaching the local network needs
`--lan` as well, which defaults off. All of it is enforced host-side in
nwfilter, so an agent with root in the box cannot undo any of it.

### Read-only shares hold against root

Tested against the case that matters — an agent with root in the guest:

```
read                              works
write as agent                    refused
write as root                     refused
root remounts rw, then writes     refused
```

The remount succeeds *inside* the guest and writes still fail, because
`<readonly/>` is enforced by virtiofsd on the host side rather than by the
guest's mount options. The guest-side `ro` mount is the second layer, not the
only one.

## GPU

Boxes do not get the GPU. The host has one RTX 4070 and keeps it; agents reach
GPU compute through the host's Ollama, shared in as a service. Several boxes can
use it at once, models are stored once on the host, and — the part passthrough
could never do — **a box with `internet = false` still gets GPU inference**.

The limit is honest: agents get *inference*, not arbitrary CUDA. No in-guest
torch, training, or `nvidia-smi`.

## Layout

```
vmorch/config.py      host-verified constants, storage layout
vmorch/spec.py        the box spec; read-only default fails closed
vmorch/domain.py      libvirt domain XML generation
vmorch/network.py     isolated management network + nwfilter rules
vmorch/services.py    per-box service grants and in-guest relays
vmorch/snapshots.py   snapshot chain, rollback, pruning
vmorch/cloudinit.py   seed ISO: users, keys, mounts, network-config
vmorch/guest.py       the reconfigure path, over SSH
vmorch/boxes.py       lifecycle
```

State lives in `~/vmorch/` (**not** `~/.local/share` — see below). Downloaded
images are cached on `~/vmorch/cloud_images/`.

## Things this cost time to learn

Each is commented where it bites.

- **State cannot live in a dot-directory under `$HOME`.** Ubuntu's AppArmor
  profile for `virt-aa-helper` carries `audit deny @{HOME}/.*/** mrwkl`. It
  cannot read a disk there, so it cannot generate qemu's profile, and the box
  fails to start with a bare "Permission denied" that looks like a file-mode
  problem and is not.
- **`debian-12-genericcloud` never runs cloud-init.** No cloud-init units in the
  boot at all, hostname stays `localhost`, `ssh.service` fails for want of host
  keys. The identical seed drives Ubuntu correctly. Default is `ubuntu-24.04`.
- **cloud-init's fallback network config brings up one interface only.** With
  two NICs the internet NIC is created and never configured. The seed carries an
  explicit `network-config` matching both by MAC, which is why the WAN MAC is
  derived rather than left to libvirt.
- **`<ip>` nwfilter drops block TCP but not ICMP.** Use `<all>`. Then accept
  `ESTABLISHED` outbound first, or the guest→host drop also kills your SSH
  session — the guest's replies are addressed to the host.
- **The RFC1918 egress drop must carve out the NAT gateway**, not the management
  one: an internet box's resolver is itself at an RFC1918 address.
- **libvirt needs the existing UUID** to redefine a domain or an nwfilter.
- **libvirt chowns disks and console logs to `libvirt-qemu`/`root`** unless the
  source carries `<seclabel relabel='no'/>`.
- **With an ACL present, a file's group bits are the mask.** A 0644 file caps
  every named ACL entry at read-only.
- **known_hosts entries are hashed**, so removing one by plaintext match
  silently never fires. Use `ssh-keygen -R`.
- **XML comments cannot contain `--`.** `tests/test_generated_xml.py` guards it.

## Requirements

libvirt/qemu with virtiofs, `cloud-image-utils`, and membership of the `libvirt`
group. No root needed at runtime; `setfacl` grants qemu access to the state
directory as the owner.
