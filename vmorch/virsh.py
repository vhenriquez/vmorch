"""Thin wrapper around the virsh CLI.

We shell out rather than use libvirt-python: it keeps the dependency surface at
zero, and every action the tool takes is a command you can paste into a terminal
yourself when something misbehaves.

All calls go to qemu:///system, which works without sudo because the owner is in
the `libvirt` group.
"""

from __future__ import annotations

import subprocess

from . import config


class VirshError(RuntimeError):
    """A virsh invocation failed. Carries the command and stderr."""

    def __init__(self, args: list[str], returncode: int, stderr: str):
        self.args_ = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(
            f"virsh {' '.join(args)} failed ({returncode}): {self.stderr}"
        )


def run(*args: str, check: bool = True, stdin: str | None = None) -> str:
    """Run a virsh subcommand and return stdout."""
    cmd = ["virsh", "--connect", config.LIBVIRT_URI, *args]
    proc = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise VirshError(list(args), proc.returncode, proc.stderr)
    return proc.stdout


def domain_exists(name: str) -> bool:
    try:
        run("dominfo", name)
        return True
    except VirshError:
        return False


def domain_state(name: str) -> str:
    """'running', 'shut off', ... or 'absent' if there is no such domain."""
    try:
        return run("domstate", name).strip()
    except VirshError:
        return "absent"


def list_domains() -> list[str]:
    """Every domain libvirt knows about, ours or not."""
    out = run("list", "--all", "--name")
    return [line.strip() for line in out.splitlines() if line.strip()]


def list_our_domains() -> list[str]:
    """Only domains this tool created.

    The owner's authorization is scoped to the vmorch- prefix. Every code path
    that stops, destroys or reconfigures a domain must source its names here.
    """
    return [d for d in list_domains() if d.startswith(config.DOMAIN_PREFIX)]


def domain_uuid(name: str) -> str | None:
    """The UUID libvirt holds for a domain, if it exists."""
    try:
        return run("domuuid", name).strip() or None
    except VirshError:
        return None


def network_exists(name: str) -> bool:
    try:
        run("net-info", name)
        return True
    except VirshError:
        return False


def nwfilter_exists(name: str) -> bool:
    try:
        run("nwfilter-dumpxml", name)
        return True
    except VirshError:
        return False


def attach_device(domain: str, xml: str, live: bool = True,
                  persist: bool = True) -> None:
    """Hot-plug a device. --config makes it survive the next boot too."""
    flags = (["--live"] if live else []) + (["--config"] if persist else [])
    _device_op("attach-device", domain, xml, flags)


def detach_device(domain: str, xml: str, live: bool = True,
                  persist: bool = True) -> None:
    flags = (["--live"] if live else []) + (["--config"] if persist else [])
    _device_op("detach-device", domain, xml, flags)


def _device_op(subcommand: str, domain: str, xml: str, flags: list[str]) -> None:
    import os
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(xml)
        path = fh.name
    try:
        run(subcommand, domain, path, *flags)
    finally:
        os.unlink(path)


def define_network(xml: str) -> None:
    _define_from_xml("net-define", xml)


def define_nwfilter(xml: str) -> None:
    _define_from_xml("nwfilter-define", xml)


def define_domain(xml: str) -> None:
    _define_from_xml("define", xml)


def _define_from_xml(subcommand: str, xml: str) -> None:
    """virsh define-style commands take a file path, not stdin."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
        fh.write(xml)
        path = fh.name
    try:
        run(subcommand, path)
    finally:
        import os

        os.unlink(path)
