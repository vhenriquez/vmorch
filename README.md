# vmorch

Disposable, reconfigurable VM sandboxes for agents on a single workstation.

An agent gets **root inside the box**. The **host is exposed only by explicit
grant** — named folders (read-only by default), specific host services, and the
public internet. Nothing else.

## Two front ends

`vmorch-tui` is a Norton Commander style control panel — two panels, Tab between
them, function keys along the bottom. Everything the CLI does is reachable from
it, and F9 lists every action with its shortcut. Left panel = boxes, right panel
= the selected box's grants.

Colours come from the fixed 16-255 region of the xterm-256 palette, not the
8-colour names. Colours 0-15 are remapped by the terminal theme, and on a light
theme "white on blue" lands around 1.5:1 — unreadable. Every pair is now
measured: `python3 tests/test_contrast.py` prints the WCAG ratio for each, and
all of them clear AAA (7:1), the lowest being dim text at 8.5:1.

F8 is contextual: on the left it destroys a box, on the right it revokes the one
folder or service under the cursor. Enter on a box opens a real ssh session;
Enter on a snapshot rolls back to it.

## Install

Python 3.11 or newer (config parsing uses `tomllib`), Linux, and **no Python
dependencies at all** — everything is standard library, so installing this
pulls in nothing.

```bash
pip install git+https://github.com/vhenriquez/vmorch
```

That puts `vmorch` and `vmorch-tui` on your PATH. Or run it straight from a
clone, with no install step:

```bash
git clone https://github.com/vhenriquez/vmorch && cd vmorch
python3 -m vmorch --help
python3 -m vmorch.tui
```

Both are supported. The rest of this README writes `vmorch`; substitute
`python3 -m vmorch` if you are running from a clone.

What is **not** a Python dependency, and has to be there anyway:

| Needed for | |
|---|---|
| `libvirt` + `qemu-kvm` | everything |
| membership of the `libvirt` group | `qemu:///system` without sudo |
| `cloud-image-utils` (`cloud-localds`) | building the seed ISO |
| `qemu-img` | overlays, snapshots, resizing |
| `acl` (`setfacl`/`getfacl`) | granting qemu access to the state directory |
| virtiofs support in libvirt/qemu | shared folders |

On Debian or Ubuntu:

```bash
sudo apt install libvirt-daemon-system qemu-kvm cloud-image-utils acl
sudo usermod -aG libvirt "$USER"     # log out and back in
```

No root is needed at runtime.

## Quick start

```bash
vmorch new agent-alpha                  # isolated: no internet, no host access
ssh agent-alpha                         # works about a minute later

vmorch share agent-alpha ~/code/thing   # read-only by default
vmorch share agent-alpha ~/scratch --rw # writes reach the host: deliberate

vmorch service agent-alpha ollama --host-port 11434  # GPU compute, no GPU handover

vmorch stop agent-alpha
vmorch snapshot agent-alpha before-experiment
vmorch start agent-alpha
# ... let the agent break things ...
vmorch stop agent-alpha && vmorch rollback agent-alpha 1
```

`vmorch ls` lists stopped boxes alongside running ones. `vmorch show <box>`
prints the full grant set, with writable shares called out loudly.

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

All state lives under `~/vmorch/` (**not** `~/.local/share` — see below):
`bases/` for golden images, `boxes/` per box, `cloud_images/` for verified
downloads. Every one is created on demand and can be moved from
`~/.config/vmorch/config.toml`; `vm config` shows where they are and creates any
that are missing.

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

## Known limitations

- **Image checksums are not signatures.** Downloads are verified against the
  distro's published `SHA256SUMS`, fetched over HTTPS from the same host as the
  image. That catches a truncated download or a rolled release; it does not
  catch a hostile mirror, which can serve a matching sums file. Verifying the
  detached GPG signature would close this and is not implemented.
- **`via: ssh` and `via: vsock` service sharing are designed, not built.**
  `vmorch service` refuses them rather than recording a grant that does nothing.
- **No golden image ships.** `vmorch golden` builds one; until you do, boxes boot
  the plain cloud image and `packages:` needs a box with internet.
- **DNS-over-HTTPS is invisible to the audit.** It is HTTPS to port 443 and
  indistinguishable from other web traffic without blocking providers outright.
  The connection log still shows the flow.
- **Boxes do not autostart after a host reboot.** `vmorch ls` still lists them;
  `vmorch start <box>` brings one back.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `./run-tests` needs
nothing but Python 3.11+ — no libvirt, no network, no dependencies — and it has
to stay that way.

## Security

The isolation properties above are claims, and they were tested rather than
inferred. If you find one that does not hold, see [SECURITY.md](SECURITY.md) —
it also lists what is deliberately *not* a boundary, which saves reporting
things that are working as designed.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
