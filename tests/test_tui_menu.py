"""Every TUI menu entry must actually do something.

The F9 menu is the discoverable surface: if an entry is listed there, a user
will pick it. An entry with no handler falls through `act_menu` silently and
looks like the app ignored the keypress -- the same silent no-op that once let
`--via vsock` record a service grant while delivering nothing.

Also checks the menu covers the CLI, so a command added to one front end is not
quietly missing from the other.

Run: python3 tests/test_tui_menu.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "vmorch" / "tui" / "app.py").read_text()

#: CLI command -> the menu value that provides it. The names differ where the
#: TUI groups things: one "toggle" entry covers both start and stop.
ALIASES = {
    "rm": "del",
    "snapshot": "snap",
    "start": "toggle",
    "stop": "toggle",
}

#: CLI commands the TUI is not expected to expose, with the reason.
CLI_ONLY = {
    "golden": "long-running build, better watched in a terminal",
    "ls": "the left panel is the listing",
    "show": "the right panel is the detail view",
    "images": "reachable as the 'Image catalogue' entry",
    "snapshots": "listed in the right panel",
    "rollback": "Enter on a snapshot row",
    "unshare": "F8 on a folder row",
    "revoke": "F8 on a service row",
    "logs": "F3",
}


def menu_values() -> list[str]:
    block = re.search(r'ui\.choose\(self\.stdscr, "Menu", \[(.*?)\]\)', SRC, re.S)
    assert block, "could not find the menu definition"
    return re.findall(r'"([a-z]+)"\)', block.group(1))


def handled_values() -> set[str]:
    body = SRC[SRC.index("def act_menu"):]
    return (set(re.findall(r'"([a-z]+)": self\.act_\w+', body))
            | set(re.findall(r'elif choice == "([a-z]+)"', body)))


def cli_commands() -> set[str]:
    cli = (ROOT / "vmorch" / "cli.py").read_text()
    return set(re.findall(r'sub\.add_parser\(\s*"([a-z]+)"', cli))


def main() -> int:
    failures = 0
    values, handled = menu_values(), handled_values()

    orphans = [v for v in values if v not in handled]
    if orphans:
        print(f"  FAIL menu entries with no handler: {orphans}")
        failures += 1
    else:
        print(f"  ok   all {len(values)} menu entries are wired")

    # Recovery actions are the ones people need when they cannot use the CLI
    # comfortably, so they must be present.
    for required in ("reseed", "mount"):
        if required in values:
            print(f"  ok   '{required}' is reachable from the menu")
        else:
            print(f"  FAIL '{required}' missing from the menu")
            failures += 1

    provided = set(values) | {c for c, v in ALIASES.items() if v in values}
    missing = sorted(cli_commands() - provided - set(CLI_ONLY))
    if missing:
        print(f"  FAIL CLI commands absent from the TUI: {missing}")
        print("       add them to the menu, to ALIASES, or to CLI_ONLY")
        failures += 1
    else:
        print("  ok   every CLI command is in the TUI or excused")

    # An alias pointing at an entry that no longer exists is worse than useless.
    stale = sorted(c for c, v in ALIASES.items() if v not in values)
    if stale:
        print(f"  FAIL ALIASES point at menu entries that do not exist: {stale}")
        failures += 1
    else:
        print("  ok   every alias resolves to a real menu entry")

    print("FAILED" if failures else "TUI menu is complete and wired")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
