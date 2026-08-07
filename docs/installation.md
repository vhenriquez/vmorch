# Installation

## Requirements

**Python 3.11 or newer.** Config parsing uses `tomllib`, which entered the
standard library in 3.11. Installing on 3.10 is refused with a message rather
than failing later at import.

**Linux.** The allocation ledger is locked with `fcntl` and the TUI is drawn
with `curses`. There is no Windows path, and macOS is untested.

**No Python dependencies.** Everything is standard library. Installing vmorch
pulls in nothing.

Plenty is needed that is *not* a Python dependency:

| Needed for | Package (Debian/Ubuntu) |
|---|---|
| everything | `libvirt-daemon-system`, `qemu-kvm` |
| `qemu:///system` without sudo | membership of the `libvirt` group |
| building the cloud-init seed ISO | `cloud-image-utils` (`cloud-localds`) |
| overlays, snapshots, resizing | `qemu-img` (ships with `qemu-utils`) |
| granting qemu access to the state directory | `acl` (`setfacl`, `getfacl`) |
| shared folders | virtiofs support in libvirt/qemu |
| the connection audit | `nftables` (optional) |

```bash
sudo apt install libvirt-daemon-system qemu-kvm qemu-utils \
                 cloud-image-utils acl
sudo usermod -aG libvirt "$USER"
```

**Log out and back in** after the `usermod`, or the group membership will not
apply and every `virsh` call will ask for authentication.

No root is needed at runtime. The one thing that would have needed it — letting
qemu read the state directory — is done with an ACL granting exactly one system
account, as you, rather than by opening the directory to every local user.

## Install

```bash
pip install git+https://github.com/vhenriquez/vmorch
```

This gives you `vmorch` and `vmorch-tui` on your PATH. In a virtualenv or with
`pipx`, both work the same way.

### Or run from a clone, without installing

```bash
git clone https://github.com/vhenriquez/vmorch && cd vmorch
python3 -m vmorch --help
python3 -m vmorch.tui
```

Equally supported. The documentation writes `vmorch`; substitute
`python3 -m vmorch` throughout if this is how you are running it.

## First run

```bash
vmorch config
```

Prints where everything will live, creates any directory that does not exist
yet, and shows free space. Nothing else touches your system until you create a
box.

Then:

```bash
vmorch new agent-alpha
```

The first box takes longer than later ones, because the cloud image has to be
downloaded (a few hundred MB) and verified. After that, creating a box is a
qcow2 overlay over the shared base and takes seconds; boot to a reachable SSH
takes about a minute.

```bash
ssh agent-alpha
```

That works with no further setup: vmorch writes an ssh config fragment, keeps
its own `known_hosts`, and generates a dedicated key on first use. It never
edits your existing config beyond adding one `Include` line, and it backs the
file up before doing so.

## Where things go

| Path | What |
|---|---|
| `~/vmorch/bases/` | golden images. Boxes are overlays on these — **put this on your fastest disk** |
| `~/vmorch/boxes/<name>/` | per-box spec, disk, seed ISO, console log, snapshots |
| `~/vmorch/cloud_images/` | verified downloads, kept so a rebuild needs no network. Cold; fine on a slow disk |
| `~/vmorch/allocations.json` | the address, MAC and vsock CID ledger |
| `~/.config/vmorch/config.toml` | your overrides (optional) |
| `~/.ssh/config.d/vmorch` | generated ssh config, owned entirely by vmorch |
| `~/.ssh/vmorch_known_hosts` | separate host keys, so rebuilding a box does not trip a warning |

Every path is configurable:

```bash
vmorch config --write     # writes a commented starter config
```

**One constraint on those paths:** none of the directories holding disk images
may be hidden, or sit inside a hidden directory. On Ubuntu, AppArmor denies
libvirt access to dot-directories under `$HOME`, and a box stored in one fails
to start. vmorch refuses such a path at startup and tells you which setting to
change.

## Upgrading

```bash
pip install --upgrade git+https://github.com/vhenriquez/vmorch
```

Existing boxes keep working: their specs are on disk and the domain XML is
regenerated from them.

**If you are coming from before the 10.x network default**, existing boxes hold
reserved addresses on `192.168.150.0/24`. Either pin the old values before
upgrading:

```toml
# ~/.config/vmorch/config.toml
mgmt_subnet  = "192.168.150.0/24"
mgmt_gateway = "192.168.150.1"
```

or destroy and recreate the boxes.

vmorch checks this for you: if the live network is serving a different subnet
from the one configured, it **refuses to create or start anything** and prints
both values with the two ways out. It does not move the network on its own —
every existing box holds an address on the old subnet in the allocation ledger
and in your ssh config, so a silent move would strand all of them at once.

## Uninstalling

```bash
vmorch rm <box>            # each box: undefines the domain, reclaims the disk
pip uninstall vmorch
```

Then, if you want the state gone as well:

```bash
rm -rf ~/vmorch ~/.config/vmorch
rm ~/.ssh/config.d/vmorch ~/.ssh/vmorch_known_hosts ~/.ssh/vmorch_ed25519*
setfacl -bn ~/vmorch       # if the directory is still there
```

The `Include config.d/vmorch` line in `~/.ssh/config` is yours to remove; vmorch
does not edit that file again after adding it.
