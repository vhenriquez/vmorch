# Security

vmorch puts a boundary between something with root inside a VM and the host it
runs on. This file says where that boundary is, what sits outside it by design,
and how to report a case where it does not hold.

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

These are the properties the tool claims. If one does not hold, that is a
vulnerability and worth reporting.

| Claim |
|---|
| A box reaches only what its spec grants. |
| Shared folders are read-only unless the spec says `rw`, and read-only holds against **root in the guest** — `<readonly/>` is enforced host-side by virtiofsd, so remounting `rw` inside the box does not help. |
| `internet = true` grants the **public internet only**. Reaching the LAN, the router, or a NAS needs `lan = true`, which defaults off. |
| A box with `internet = false` reaches no network beyond its own management link. |
| Boxes cannot reach each other, unless you join them to a shared local network. |
| A box cannot reach a host service unless a service grant opened that specific port. |
| All of the above is enforced **host-side** — in libvirt nwfilter rules and the domain definition — so an agent with root inside the box cannot undo any of it. |

The last row matters most: if you find a way for root inside a box to change
what that box can reach, that is the report worth sending.

## What is *not* a boundary

These behave the way they do by design. Reading this first will save you time.

**Whatever runs in a box has root inside it.** That is the premise of the tool.
`sudo = "none"` makes an unprivileged compromise inside the guest harder to
escalate, but it is not a boundary and is not claimed as one. The boundary is
the VM.

**A writable shared folder lets a rooted guest write to that host path.** That
is what granting it means. Share narrowly: a guest that can write a `.git` hook
or a Makefile into a folder you later run something in has a route back to your
host. This is why read-only is the default, why writable grants are highlighted
in `vmorch show`, and why the host's own code and credential directories cannot
be shared at all.

**Read-only shares still disclose.** Read-only limits writes, not reads. A
read-only share of a directory containing `.env`, `.git-credentials` or key
material exposes all of it. Choose *which* folder first, and the mode second.

**Snapshots do not cover shared folders.** A shared folder is the live host
filesystem, not part of the box's disk, so rolling a box back does not undo
anything written into a writable share.

**A shared host service is reachable, and its own authentication is the only
control behind the hole.** Some services have none — Ollama, for example, will
let anything that can reach its API pull models until your disk fills, or delete
the ones you have. vmorch scopes the opening to one box's address; what happens
inside that opening is up to the service.

**A box behind a router box reaches whatever the router reaches, regardless of
its own `internet` grant.** If you attach a box to a local network where another
box holds the router role, that router forwards for it. `internet = false` means
"no network interface of its own to the outside", not "cannot possibly reach the
outside".

**The console has no password.** Every box gets a VNC device on host loopback
with no authentication, so any local user on your host has full console access
to every box, bypassing SSH. This is fine on a single-user workstation, which is
what vmorch is built for. **On a multi-user host, remove the `<graphics>`
element** from `vmorch/domain.py`; everything except the console keeps working.

**Image checksums are not signatures.** Downloads are verified against the
distribution's published `SHA256SUMS`, fetched over HTTPS from the same server
as the image. That catches a truncated or corrupted download and a silently
rolled release. It does not catch a hostile mirror, which can serve a matching
checksum file. Verifying the detached GPG signatures Debian and Ubuntu publish
would close this and is not implemented.

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

In scope: anything under **What is claimed** failing to hold. Also in scope: a
default that is more permissive than documented, and any case where the tool
reports a grant differently from what it actually applied — a tool that says it
did something it did not do is a security problem in its own right.

Out of scope: everything under **What is not a boundary**; vulnerabilities in
libvirt, QEMU or the guest operating system (report those upstream); and
anything that requires root on the host to begin with.
