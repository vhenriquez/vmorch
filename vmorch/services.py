"""Service sharing across the box boundary.

The two directions are not symmetric, and treating them the same is the easy
mistake:

**box -> host** is nearly free. The box already holds a stable address on the
management network and the host can route to it, so `via: direct` needs no
plumbing at all -- the guest service just has to bind 0.0.0.0 rather than
127.0.0.1. It is recorded in the spec so `vmorch show` can list it.

**host -> box** is the direction that costs something, because guest-initiated
traffic to the host is dropped by default. Every entry is a deliberate hole:

  via: filter   an accept rule ahead of the drop, in a per-box nwfilter.
                Persistent, no relay process, no session to keep alive.
                The default.
  via: vsock    a socat relay over virtio-vsock. No IP involved at all, works
                on a box with zero NICs.
  via: ssh      a RemoteForward carried by a systemd user unit. Never a bare
                ssh config entry: a forward that dies with the terminal is
                useless to a background agent.

A per-box filter exists so grants can differ between boxes. Scoping the accept
to one box means adding a second, less-trusted box does not silently inherit
the first one's access.
"""

from __future__ import annotations

import re

from . import config, virsh
from .spec import BoxSpec


def box_filter_name(name: str) -> str:
    return f"vmorch-box-{name}"


def build_box_filter(spec: BoxSpec) -> str:
    """Per-box filter: the shared base plus this box's service grants.

    Accepts sit at priority 200, ahead of the guest->host drops at 500 in the
    base filter. Priorities are explicit rather than positional -- relying on
    document order across a filterref boundary would be a latent bug.
    """
    rules = []
    for svc in spec.from_host:
        if svc.via != "filter":
            continue
        rules += [
            f"  <!-- grant: {svc.name} -->",
            "  <rule action='accept' direction='out' priority='200'>",
            f"    <tcp dstipaddr='{config.MGMT_GATEWAY}' dstipmask='32'"
            f" dstportstart='{svc.host_port}' dstportend='{svc.host_port}'/>",
            "  </rule>",
        ]

    body = "\n".join(rules) if rules else "  <!-- no service grants -->"
    return f"""<filter name='{box_filter_name(spec.name)}' chain='root'>
  <filterref filter='vmorch-mgmt-filter'/>
{body}
</filter>
"""


def ensure_box_filter(spec: BoxSpec) -> str:
    """Define or update this box's filter. Returns the filter name."""
    xml = build_box_filter(spec)
    name = box_filter_name(spec.name)
    try:
        existing = virsh.run("nwfilter-dumpxml", name)
        match = re.search(r"<uuid>([^<]+)</uuid>", existing)
        if match and "<uuid>" not in xml:
            xml = xml.replace(">", f">\n  <uuid>{match.group(1)}</uuid>", 1)
    except virsh.VirshError:
        pass
    virsh.define_nwfilter(xml)
    return name


def delete_box_filter(name: str) -> None:
    try:
        virsh.run("nwfilter-undefine", box_filter_name(name))
    except virsh.VirshError:
        pass


def guest_relay_script(spec: BoxSpec) -> str:
    """In-guest relays so host services appear on the box's own loopback.

    Tooling overwhelmingly assumes 127.0.0.1. Relaying locally means nothing in
    the box needs OLLAMA_HOST or equivalent set -- the difference between a
    service that works and one that works only if you remember to configure it.

    Uses **systemd-socket-proxyd, not socat**. socat is not in the Ubuntu cloud
    image, and the boxes that most need this are precisely the ones that cannot
    apt-get it: an isolated box has no internet by definition. socket-proxyd
    ships as part of systemd, so it is present in any image that boots at all.

    Socket activation also means the listener exists from boot, before the proxy
    process starts, so nothing races the first connection.
    """
    units = []
    for svc in spec.from_host:
        if svc.via != "filter":
            continue
        unit = f"vmorch-relay-{svc.name}"
        units.append(f"""cat > /etc/systemd/system/{unit}.socket <<'SOCKET'
[Unit]
Description=vmorch relay socket: {svc.name}

[Socket]
ListenStream=127.0.0.1:{svc.guest_port}

[Install]
WantedBy=sockets.target
SOCKET
cat > /etc/systemd/system/{unit}.service <<'UNIT'
[Unit]
Description=vmorch relay: {svc.name} -> host {config.MGMT_GATEWAY}:{svc.host_port}
Requires={unit}.socket
After={unit}.socket

[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd {config.MGMT_GATEWAY}:{svc.host_port}
UNIT
systemctl disable --now {unit}.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now {unit}.socket""")

    if not units:
        return ""
    return "set -e\n" + "\n".join(units) + "\n"
