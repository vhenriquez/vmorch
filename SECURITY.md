# Security

vmorch exists to put a boundary between an agent with root in a VM and the host
it runs on. That boundary is the product, so this file says exactly where it is,
what is deliberately outside it, and how to report a case where it does not hold.

## Reporting a vulnerability

Use **GitHub's private vulnerability reporting** on this repository
(Security → Report a vulnerability). It stays private until it is fixed.

Please **do not open a public issue** for anything that lets a guest reach the
host, or lets one box reach another, outside the grants its spec was given.

This is a single-maintainer project with no service-level commitment. What you
can expect: an acknowledgement, an honest answer about whether it is in scope,
and a fix or a documented limitation. What you should not expect: a bounty, or a
same-day response.

Useful in a report: the box spec (`vmorch show <box>` output, redacted as you
like), what you expected the boundary to be, and what you actually reached.

## What is claimed

These are the properties worth testing. Each was verified from inside a running
box rather than inferred from configuration, and a case where one does not hold
is a vulnerability.

| Claim |
|---|
| A box reaches only what its spec grants. |
| Shared folders are read-only unless the spec says `rw`, and read-only holds against **root in the guest** — `<readonly/>` is enforced host-side by virtiofsd, so remounting `rw` inside the box does not help. |
| `internet = true` grants the **public internet only**. Reaching the LAN, the router, or a NAS needs `lan = true`, which defaults off. |
| A box with `internet = false` reaches no network beyond its own management link. |
| Boxes cannot reach each other, unless deliberately joined to a shared local network. |
| A box cannot reach a host service unless a service grant opened that specific port. |
| All of the above is enforced **host-side** — in libvirt nwfilter rules and the domain definition — so an agent with root inside the box cannot undo any of it. |

The last row is the one that matters most. If you find a way for in-guest root
to change what the box can reach, that is the report to send.

## What is *not* a boundary

This section exists to save you time. These behave the way they do on purpose.

**The agent has root inside its box.** That is the premise of the tool, not a
finding. `sudo = "none"` raises the cost of an unprivileged in-guest compromise;
it is not a boundary and is not claimed as one. The boundary is the VM.

**A `rw` shared folder lets a rooted guest write to that host path.** That is
what granting it means. It is why `ro` is the default, why an `rw` grant is
called out loudly in `vmorch show`, and why the host's own code and credential
directories are refused outright. A rooted agent writing a `.git` hook or a
Makefile into a shared folder that the host later runs is the realistic path
back, and the mitigation is to share narrowly, not to expect the share to
protect you.

**Read-only shares still disclose.** `ro` limits writes, not reading. An `ro`
mount of a directory containing `.env`, `.git-credentials` or key material
exposes all of it. Choose *which* folder first and the mode second.

**Snapshots do not cover shared folders.** virtiofs mounts are the live host
filesystem, so rolling a box back does not undo anything the agent wrote into an
`rw` share. The tool says so in its own output.

**A shared host service is reachable, with its own auth as the only control.**
That is what a service grant is. Ollama in particular has **no authentication**:
the nwfilter rule scoped to that one box's address is the entire access control,
and an agent that can reach the API can pull models until the disk fills, or
delete the ones you have. This is an accepted risk with stated mitigations (bind
to the management bridge address specifically, never `0.0.0.0`; scope the filter
rule to the box's IP, not the subnet; watch host disk). A `/api/generate`-only
proxy is the known fix if it ever bites.

**A box behind a router box reaches the internet regardless of its own
`internet` grant.** If you attach a box to a local network where another box has
the router role, that router forwards for it — that is what the role is for. The
router's own `lan` grant still applies to what leaves it. This is the least
obvious item on this list, so: **`internet = false` means "no NIC of its own to
the outside", not "cannot possibly reach the outside".**

**The console has no password.** Every box gets a VNC graphics device on host
loopback with no authentication, because without it the box has no console and
`virt-manager` cannot show one. Any local user on the host therefore has full
console access to every box, bypassing SSH. This is accepted for the
single-user workstation the tool is built for, and it is a wider grant than the
rest of the tool makes to other local accounts. **On a multi-user host, remove
the `<graphics>` element** from `vmorch/domain.py` — everything except the
console keeps working.

**Image checksums are not signatures.** Downloads are verified against the
distro's published `SHA256SUMS`, fetched over HTTPS from the same host as the
image. That catches a truncated download, a corrupted mirror or a silently
rolled release. It does **not** catch a hostile mirror, which can serve a
matching sums file. Verifying the detached GPG signatures Debian and Ubuntu
publish would close this; it is not implemented, because doing it properly means
shipping and pinning the signing keys rather than fetching those over the same
channel.

**DNS-over-HTTPS is invisible to the audit.** It is HTTPS to port 443 and
indistinguishable from other web traffic without blocking providers outright.
Plain DNS and DNS-over-TLS are locked to the tool's own resolver so the query
log has no holes; DoH is a known gap, and the connection log still shows the flow.

**Anyone who can run `vmorch` can grant anything.** There is no privilege
separation between "operating the tool" and "deciding what a box may reach".
Someone who can run the CLI can share any folder, on any box. The tool assumes
the person running it owns the host.

## Supported versions

There are no releases yet. Fixes land on `master`, and that is the only branch
that gets them.

## Scope

In scope: anything in **What is claimed** failing to hold. Also in scope: a
default that is more permissive than documented, or output that describes a
grant differently from what was actually applied — this project treats "the tool
said it did something it did not do" as a security problem in its own right, and
has fixed several.

Out of scope: everything under **What is not a boundary**; vulnerabilities in
libvirt, QEMU or the guest OS (report those upstream); and anything requiring
prior root on the host.
