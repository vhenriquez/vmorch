"""Every path that starts a domain must define the filters first.

A box started without a filter definition immediately before it is **not
filtered**. Measured 2026-08-05: with `lan = false` it reached the LAN router and
the host's LAN address with 0% loss for as long as it was left alone. The filter
was defined, correct, and bound to the port the whole time -- libvirt had simply
not put the rules in place.

Four filter variants were tested to find the cause (as shipped, without
`clean-traffic`, and `CTRL_IP_LEARNING` pinned to `none` and to `dhcp`); all four
behaved identically, so the content is not the issue. What matters is only that
a define happens between the previous state and the start.

That makes this a *sequencing* property, and sequencing is exactly what rots
silently: a new start path added later would inherit the hole with nothing to
say so. Hence a test that reads the call order rather than the behaviour -- the
behaviour needs a real box and three minutes, which is why the hole survived
this long.

Run: python3 tests/test_filter_arming.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "vmorch" / "boxes.py").read_text()


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def starts_and_arms(fn: ast.FunctionDef) -> tuple[int, int]:
    """(line of `virsh.run("start", ...)`, line of the arm_filters before it).

    Returns (0, 0) when the function does not start a domain.
    """
    start_line = arm_line = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "run" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "start":
                start_line = max(start_line, node.lineno)
        if isinstance(f, ast.Attribute) and f.attr == "arm_filters":
            arm_line = max(arm_line, node.lineno)
    return start_line, arm_line


def main() -> int:
    failures = 0
    tree = ast.parse(SRC)
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}

    starters = {name: starts_and_arms(fn) for name, fn in functions.items()}
    starters = {n: v for n, v in starters.items() if v[0]}

    failures += check("boxes.py has start paths to check", bool(starters),
                      "the AST walk found no virsh.run('start', ...) at all, "
                      "so this test is checking nothing")

    # Every one of them, by name, so a new path shows up here rather than
    # quietly joining the list.
    expected = {"create", "apply", "start", "reseed"}
    failures += check("the known start paths are all present",
                      expected <= set(starters),
                      f"missing: {sorted(expected - set(starters))}")

    for name, (start_line, arm_line) in sorted(starters.items()):
        failures += check(
            f"{name}() arms the filters before starting",
            arm_line and arm_line < start_line,
            "no network.arm_filters() call before virsh.run('start', ...) "
            f"(arm at line {arm_line or '-'}, start at line {start_line})")

    # Ordering is the whole point: arming *after* the start was tried first and
    # does nothing, because by then the unfiltered port already exists.
    for name, (start_line, arm_line) in sorted(starters.items()):
        if arm_line:
            failures += check(f"{name}() does not arm after the start",
                              arm_line < start_line,
                              "arming after virsh start is a no-op; the port "
                              "is already up unfiltered")

    # And the function has to exist, with ensure_filters behind it.
    from vmorch import network
    failures += check("network.arm_filters exists",
                      callable(getattr(network, "arm_filters", None)))
    failures += check("network.ensure_filters exists",
                      callable(getattr(network, "ensure_filters", None)))

    print("FAILED" if failures else "every start path arms the filters first")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
