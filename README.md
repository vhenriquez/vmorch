# vmorch

Disposable, reconfigurable VM sandboxes on a single Linux workstation.

Give something untrusted — an AI agent, a build script, a dependency you have
not read — a whole machine to work on, and decide exactly what of yours it can
reach. It gets root inside its box. Your host stays closed unless you open a
specific door: a named folder, one host service, the internet.

Boxes come up in about a minute, cost a few megabytes each, and are still there
next month if you want them.

```bash
vmorch new agent-alpha              # isolated: no internet, no host access
ssh agent-alpha
```

## What a box can reach

Nothing, until you say so. Each grant is separate and visible, and all of it is
enforced on the host, so root inside the box cannot undo any of it.

|  | isolated (default) | `--internet` | `--internet --lan` |
|---|---|---|---|
| public internet | no | **yes** | **yes** |
| your LAN — router, NAS, other machines | no | no | **yes** |
| host services | no | no | no |
| a service you granted | **yes** | **yes** | **yes** |
| other boxes | no | no | no |
| host folders | none | none | none |

`--internet` means the public internet *only*. Reaching your own network is a
separate flag, off by default, because "let it install packages" and "let it
scan my NAS" are different decisions.

**Shared folders are read-only unless you ask otherwise, and read-only holds
against root in the guest.** The agent can remount the share writable inside its
box and writes will still fail — the restriction is enforced by the host, not by
the guest's mount options.

## Install

Python 3.11+, Linux, and **no Python dependencies** — it is all standard
library.

```bash
pip install git+https://github.com/vhenriquez/vmorch
```

You also need libvirt and a few tools that are not Python packages:

```bash
sudo apt install libvirt-daemon-system qemu-kvm qemu-utils \
                 cloud-image-utils acl
sudo usermod -aG libvirt "$USER"     # then log out and back in
```

No root is needed to run it. Full details, other distributions and the
run-from-a-clone option are in [docs/installation.md](docs/installation.md).

## Using it

```bash
vmorch new agent-alpha                   # isolated box
ssh agent-alpha                          # ready about a minute later

vmorch share agent-alpha ~/code/thing    # read-only
vmorch share agent-alpha ~/scratch --rw  # writable: a real grant

vmorch service agent-alpha ollama --host-port 11434   # reach one host service

vmorch stop agent-alpha
vmorch snapshot agent-alpha before-experiment
vmorch start agent-alpha
# ... let it break things ...
vmorch stop agent-alpha && vmorch rollback agent-alpha 1

vmorch ls                                # every box, running or not
vmorch show agent-alpha                  # everything this box can reach
```

Boxes are durable. One you made months ago still lists, still has its address,
and can be started and reconfigured. Edit its spec file and run
`vmorch apply <box>`.

**[docs/usage.md](docs/usage.md)** covers the model, every command, the spec
file and configuration.

## The terminal UI

```bash
vmorch-tui
```

A two-panel control panel: boxes on the left, the selected box's grants on the
right, function keys along the bottom, `F9` for every action. `Enter` on a box
opens an SSH session. Everything the CLI does is reachable from it.

## Known limitations

- **Image checksums are not signatures.** Downloads are verified against the
  distribution's published `SHA256SUMS`, fetched over HTTPS from the same
  server. That catches a corrupted or truncated download, not a hostile mirror.
- **Two of the three service-sharing mechanisms are not built.** `vmorch
  service` accepts `--via filter` and refuses the others rather than recording
  a grant that does nothing.
- **No prebuilt image ships.** `vmorch golden` builds one with your software
  baked in; until then boxes boot a plain cloud image.
- **DNS-over-HTTPS is invisible to the audit log.** Plain DNS and DNS-over-TLS
  are covered; DoH is indistinguishable from other HTTPS traffic.
- **Boxes do not start automatically after a host reboot.** `vmorch ls` still
  lists them; `vmorch start <box>` brings one back.

## Documentation

- [Installation](docs/installation.md) — requirements, install, first run
- [Usage](docs/usage.md) — the model, everyday tasks, command reference
- [Security](SECURITY.md) — what the boundary is, and what it is not
- [Contributing](CONTRIBUTING.md) — running the tests, conventions

## Licence

Apache-2.0. See [LICENSE](LICENSE).
