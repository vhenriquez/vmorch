# Usage

## The model

A **box** is a VM that reaches nothing except what its spec grants it.

Everything else follows from that one sentence. There is no "allow" list to
maintain and no firewall to write: a new box starts isolated, and each grant is
a separate, visible decision. All of it is enforced on the host — in libvirt
nwfilter rules and the domain definition — so an agent with root inside the box
cannot undo any of it.

A box has four kinds of grant:

| Grant | Default | What it opens |
|---|---|---|
| **folders** | none | a named host directory, **read-only unless you say `rw`** |
| **internet** | off | the public internet — *not* your LAN |
| **lan** | off | the router, the NAS, other machines on your network |
| **services** | none | one host port, for that one box |

Plus one deliberate exception to box-to-box isolation, **local networks**, which
you opt into by name.

The spec is a file — `~/vmorch/boxes/<name>/box.toml` — and it is the source of
truth. The libvirt domain XML is generated from it, so `vmorch apply` reconciles
a box to its spec rather than anyone hand-editing XML.

### Boxes are not disposable-only

They are durable. A box you made in March is still listed in September, keeps
its address, and can be started, reconfigured and reasoned about. `vmorch ls`
shows stopped boxes alongside running ones, because "which boxes do I have" is
the question being asked, and a stopped box costs only its overlay.

## Everyday tasks

### Make a box

```bash
vmorch new agent-alpha                        # isolated
vmorch new builder --internet                 # public internet, no LAN
vmorch new bigbox --cpus 8 --memory 16G --disk 80G
```

Then `ssh agent-alpha`. That works immediately — no key copying, no IP lookup.

To see what an existing box actually has:

```bash
vmorch show agent-alpha
```

Writable shares and service grants are highlighted.

### Share a folder

```bash
vmorch share agent-alpha ~/code/thing          # read-only
vmorch share agent-alpha ~/scratch --rw        # writes reach the host
```

Read-only is the default and it holds **against root in the guest**: the agent
can remount the share `rw` inside the box and writes still fail, because
`<readonly/>` is enforced host-side by virtiofsd rather than by the guest's
mount options.

`--rw` is a real grant. A rooted agent can write a `.git` hook or a Makefile
that *you* later run on the host. Share narrowly. vmorch refuses to share the
host's own code and credential directories (`/usr`, `/boot`, `/etc`, `~/.ssh`
and friends) outright.

```bash
vmorch unshare agent-alpha thing
```

Both work on a running box, without a reboot.

### Grant internet — and understand what that means

```bash
vmorch new scraper --internet          # public internet only
vmorch new scanner --internet --lan    # ...and the local network
```

`--internet` gives the box a NAT interface and then **drops egress to all
private address space** on it, so it can reach the internet but not your router,
your NAS, or another machine on your desk. `--lan` removes that drop.

The split matters because ordinary NAT reaches your LAN as well as the
internet. If you want a box that can install packages but cannot touch your
network, that is `--internet` on its own.

An isolated box (neither flag) has no resolver at all, which also closes DNS as
a covert channel.

### Share a host service

```bash
vmorch service agent-alpha ollama --host-port 11434
```

Guest-to-host traffic is blocked by default; this punches one hole, for one
port, for that one box. A second, less-trusted box does not inherit it.

vmorch also sets up an in-guest relay so the service appears on the box's own
`127.0.0.1:11434` — tooling overwhelmingly assumes loopback, and nothing inside
the box needs `OLLAMA_HOST` set.

A service grant works regardless of the box's network grants: a box with no
internet at all can still reach a service you shared with it.

Note what a service grant costs. Behind the hole, the service's own
authentication is the only remaining control — and some services, Ollama
included, have none. See [SECURITY.md](../SECURITY.md).

```bash
vmorch revoke agent-alpha ollama
```

### Snapshot before letting an agent loose

```bash
vmorch stop agent-alpha
vmorch snapshot agent-alpha before-experiment
vmorch start agent-alpha
# ... let it break things ...
vmorch stop agent-alpha
vmorch rollback agent-alpha 1
```

Snapshots need the box stopped: snapshotting a running box gives you a
crash-consistent image at best.

**Three layers maximum.** Creating a fourth commits the oldest into the box's
overlay first, and that rollback point is *gone*, not archived. `vmorch
snapshots <box>` lists what you have.

**Snapshots do not cover shared folders.** A shared folder is the live host
filesystem, not part of the box's disk, so rolling back does not undo anything
the agent wrote into a writable share. The tool reminds you of this every time
you take one.

### Change a box after the fact

Edit `~/vmorch/boxes/<name>/box.toml`, then:

```bash
vmorch apply agent-alpha
```

This regenerates the domain from the spec and reconciles what it can in place —
folder modes, the guest's network configuration, disk size — restarting only if
something needs it. It reports what it did.

`vmorch apply` does not re-run first-boot configuration. If a box's SSH has
broken badly enough that you need that, use `vmorch reseed <box>` instead — a
bigger hammer, which regenerates host keys and every file cloud-init owns.

### Grow a disk

```bash
vmorch disk agent-alpha 80G      # absolute
vmorch disk agent-alpha +20G     # increment
```

Grows the qcow2, the partition table and the filesystem, and updates the spec —
all three, because stopping halfway is what makes a resize look broken. Works on
a running box. **Never shrinks**: shrinking a qcow2 discards the end of the
device, which is where the filesystem keeps its data.

### Bake software into an image

Boxes boot the plain cloud image, so anything you want preinstalled has to be
installed at first boot — slow, and impossible on an isolated box, which has no
internet to install *from*. A golden image fixes both:

```bash
vmorch golden agent-base --packages tmux,git,ripgrep
vmorch new worker --image agent-base
```

Or set a box up by hand and freeze it:

```bash
vmorch stop scratch
vmorch golden agent-base --from-box scratch
```

Either way the image is generalized — machine-id, SSH host keys and cloud-init
state are reset — so boxes built from it do not share an identity.

### See what a box did

```bash
vmorch audit --since -1h
vmorch audit --blocked            # refused attempts: usually the interesting ones
vmorch audit --box agent-alpha
```

Two streams, both written by the host and neither reachable from inside a box:
DNS queries, and connections. Names are resolved back to boxes, and connections
are joined to the lookup that produced the address — auditing bare IPs ages
badly.

Both need turning on once:

```bash
vmorch audit --enable-dns         # restarts the libvirt networks
vmorch audit --install            # prints one root command for you to review
```

`--install` never runs anything as root; it prints the ruleset for you to read
and run yourself.

### Local networks

The one place box-to-box traffic is allowed, and you opt in by name:

```bash
vmorch net create lab
vmorch net attach web lab
vmorch net attach db lab
vmorch net ls
```

A local network is members-only: no gateway, no host address, no DHCP, no
internet. Members reach each other and nothing else. Addresses are written
straight into each guest, so a box knows its peers before either has booted.

One member can be the **router**, forwarding for the rest:

```bash
vmorch net attach firewall lab --router
```

Note the consequence: **a box behind a router reaches whatever the router can
reach, regardless of its own `--internet` grant.** That is what the role is for,
and it is easy to overlook.

## The TUI

```bash
vmorch-tui
```

A Norton Commander style panel: boxes on the left, the selected box's grants on
the right, function keys along the bottom, `F9` for every action with its
shortcut.

`F8` is contextual — on the left it destroys a box, on the right it revokes the
one folder or service under the cursor. `Enter` on a box opens a real SSH
session; `Enter` on a snapshot rolls back to it.

Everything the CLI does is reachable here, and every option carries a
description of what it does.

## Command reference

| Command | |
|---|---|
| `vmorch new <name>` | create a box — `--image --cpus --memory --disk --internet --lan --sudo --nested --no-start` |
| `vmorch ls` | every box, stopped and running |
| `vmorch show <name>` | full grant set |
| `vmorch apply <name>` | regenerate from the spec and reconcile |
| `vmorch start/stop <name>` | `stop --force` pulls the plug |
| `vmorch rm <name>` | destroy and reclaim the disk — `--yes` to skip the prompt |
| `vmorch share <name> <path>` | grant a folder — `--tag`, `--rw` |
| `vmorch unshare <name> <tag>` | revoke it |
| `vmorch mount <name>` | re-mount shares that did not come up |
| `vmorch service <name> <svc>` | grant a host service — `--host-port` (required), `--guest-port`, `--via` |
| `vmorch revoke <name> <svc>` | revoke it |
| `vmorch snapshot <name> [label]` | freeze the disk (box must be stopped) |
| `vmorch snapshots <name>` | list them |
| `vmorch rollback <name> <index>` | rewind |
| `vmorch disk <name> <size>` | grow — `80G` or `+20G`. Never shrinks |
| `vmorch reseed <name>` | re-run first-boot config to repair a box |
| `vmorch sudo <name> [mode]` | show or set the agent's sudo rights |
| `vmorch password <name>` | print the sudo password (password mode only) |
| `vmorch golden <name>` | build an image — `--from-box`, `--image`, `--packages`, `--run`, `--keep-build-box` |
| `vmorch images` | the catalogue — `--all`, `--restore-defaults` |
| `vmorch rmimage <name>` | remove an image — `--keep-cache`, `--keep-entry`, `--force`, `--yes` |
| `vmorch logs <name>` | console log — `-n`/`--lines`, `--clean`, `--raw` |
| `vmorch audit` | what boxes looked up and reached — `--since --box --blocked --install --enable-dns` |
| `vmorch config` | paths, defaults, disk usage — `--write` for a starter file |
| `vmorch net` | ensure the management network — `--prune` |
| `vmorch net ls/create/rm` | local networks |
| `vmorch net attach/detach <box> <net>` | membership — `--router` |

`vmorch <command> --help` for the full options on any of them.

## The spec file

`~/vmorch/boxes/<name>/box.toml`. Edit it and run `vmorch apply <name>`.

```toml
name = "agent-alpha"
image = "ubuntu-24.04"
cpus = 4
memory = "8G"
disk = "40G"
user = "agent"
sudo = "nopasswd"
nested = false

[network]
# internet = true grants the PUBLIC internet only.
# Reaching the router/NAS/other machines needs lan = true.
internet = false
lan = false
nets = []
routes_for = []

# Folders are READ-ONLY unless mode = "rw" is set.
[[folders]]
host = "/home/you/code/thing"
tag = "thing"
mode = "ro"

[[services.from_host]]
name = "ollama"
host = 11434
guest = 11434
via = "filter"
```

Anything malformed is refused rather than guessed at. A `mode` that is missing,
empty or misspelled resolves to `ro` — never to `rw`.

## Configuration

`~/.config/vmorch/config.toml`, and `vmorch config --write` gives you a
commented starter. Everything is optional.

```toml
state_dir      = "~/vmorch"
bases_dir      = "~/vmorch/bases"
boxes_dir      = "~/vmorch/boxes"
download_cache = "~/vmorch/cloud_images"

default_image  = "ubuntu-24.04"
default_cpus   = 4
default_memory = "8G"
default_disk   = "40G"
default_user   = "agent"

max_snapshot_layers = 3

# Only change these BEFORE creating any box: existing boxes hold reserved
# addresses on the old subnet and would be stranded.
mgmt_subnet    = "10.150.0.0/24"
mgmt_gateway   = "10.150.0.1"
localnet_pool  = "10.150.16.0/20"
```

## When something goes wrong

**`ssh <box>` refuses the connection, but the box pings.** Usually sshd failed
to start for want of host keys. `vmorch logs <box>` will show it. `vmorch reseed
<box>` regenerates them.

**A box has "internet" but resolves nothing.** Check it actually has the NIC:
`vmorch show` reports the grant, and `vmorch apply <box>` reconciles the guest's
network configuration if the box gained internet after it was created.

**"Permission denied" starting a box, with nothing else to go on.** Almost
always a state directory in a hidden path — AppArmor denies `virt-aa-helper`
any dot-directory under `$HOME`. vmorch refuses such a path at startup, so this
means something outside its control (a symlink through a dot-directory, for
instance).

**All my boxes are gone after a reboot.** They are not: boxes do not autostart.
`vmorch ls` still lists them and `vmorch start <box>` brings one back.

**A share is attached but not mounted.** `vmorch mount <box>` re-mounts
everything the spec grants. This happens if a box was stopped when the share was
added.

**`vmorch new` failed halfway and now the name is taken.** `vmorch rm <name>`
clears the partial state, then create it again.
