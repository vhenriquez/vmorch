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
    # `vmorch net` grew subcommands. They are nested under one parser in the CLI and
    # under one submenu in the TUI, so they are checked by their full names.
    "net ls": "netls",
    "net create": "netcreate",
    "net rm": "netrm",
    "net attach": "netattach",
    "net detach": "netdetach",
}

#: CLI commands the TUI is not expected to expose, with the reason.
CLI_ONLY = {
    "ls": "the left panel is the listing",
    "show": "the right panel is the detail view",
    "images": "reachable as the 'Image catalogue' entry",
    "snapshots": "listed in the right panel",
    "unshare": "F8 on a folder row",
    "revoke": "F8 on a service row",
    "logs": "F3",
}


def menu_values() -> list[str]:
    """Every leaf action in the menu tree, submenu links excluded.

    Read from the MENUS structure rather than scraped out of the source. The
    old version regex-matched one `ui.choose(...)` call, so grouping the menu
    into submenus would have made it silently match nothing and pass.
    """
    from vmorch.tui.app import MENUS
    return [e.value for _, entries in MENUS.values() for e in entries
            if e.value and not e.value.startswith("menu:")]


def handled_values() -> set[str]:
    body = SRC[SRC.index("def _run_menu_action"):]
    return (set(re.findall(r'"([a-z]+)": self\.act_\w+', body))
            | set(re.findall(r'elif choice == "([a-z]+)"', body)))


#: CLI option -> the TUI field that provides it, where the names differ.
#: `--no-start` is inverted in the TUI ("start now"), because a form of
#: positive toggles reads better than a negative one.
FLAG_FIELDS = {"--no-start": "start"}


def report(label: str, missing: set[str]) -> int:
    if missing:
        print(f"  FAIL {label}: {', '.join(sorted(missing))}")
        return 1
    print(f"  ok   {label}")
    return 0


def cli_new_flags() -> set[str]:
    """Options on `vmorch new`. Every one must be settable from the TUI."""
    import re as _re
    cli = (ROOT / "vmorch" / "cli.py").read_text()
    block = _re.search(r"new = sub\.add_parser.*?new\.set_defaults", cli, _re.S)
    return set(_re.findall(r'add_argument\("(--[a-z-]+)"', block.group(0)))


def cli_commands() -> set[str]:
    """Every subcommand, nested ones included.

    `netsub.add_parser("create")` also ends in `sub.add_parser`, so a naive
    pattern reported `create` as a top-level command and demanded a menu entry
    called that. Nested ones are reported as "net create", which is what a user
    types and what ALIASES maps.
    """
    cli = (ROOT / "vmorch" / "cli.py").read_text()
    top = set(re.findall(r'(?<![A-Za-z_])sub\.add_parser\(\s*"([a-z]+)"', cli))
    nested = {f"net {c}" for c in
              re.findall(r'netsub\.add_parser\(\s*"([a-z]+)"', cli)}
    return top | nested


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

    # --- the menu tree itself -------------------------------------------
    from vmorch.tui.app import MENUS

    # A submenu link pointing at a name that does not exist is a dead entry
    # that raises KeyError the moment it is picked.
    targets = {e.value.split(":", 1)[1]
               for _, entries in MENUS.values() for e in entries
               if e.value.startswith("menu:")}
    failures += report("every submenu link resolves", targets - set(MENUS))

    # A submenu nobody links to cannot be opened, so its actions are lost.
    failures += report("every submenu is reachable from a menu",
                       set(MENUS) - targets - {"main"})

    # Accelerators are the whole point of the grouping -- two entries sharing
    # a letter means one of them can never be reached by keystroke.
    for name, (_, entries) in MENUS.items():
        keys = [e.key for e in entries if e.key]
        dupes = {k for k in keys if keys.count(k) > 1}
        failures += report(f"'{name}' has no duplicate accelerators", dupes)

        # j and k move the cursor in `choose`, so an accelerator using them
        # would be swallowed by navigation and look like a dead key.
        failures += report(f"'{name}' avoids the navigation keys",
                           {k for k in keys if k in ("j", "k")})

        # Every action needs a letter, or the grouping costs keystrokes
        # instead of saving them.
        failures += report(f"'{name}' gives every entry an accelerator",
                           {e.label for e in entries
                            if e.value and not e.key})

        # Headers are labels only; an entry is one or the other.
        failures += report(f"'{name}' keeps headers valueless",
                           {e.label for e in entries if e.header and e.value})

    # --- the keybar -------------------------------------------------------
    #
    # The bottom strip is the scarcest space in the interface, so what sits
    # there is a real decision. These check the strip stays honest rather than
    # drifting out of step with the keys the loop actually binds.
    from vmorch.tui.app import KEYBAR
    labels = {num: label for num, label in KEYBAR}
    failures += report("the keybar has ten slots",
                       set() if len(KEYBAR) == 10 else {str(len(KEYBAR))})

    # Norton Commander meanings that are free to keep, so they are kept.
    for num, expected in (("1", "Help"), ("3", "View"), ("4", "Edit"),
                          ("7", "New"), ("8", "Del"), ("9", "Menu"),
                          ("10", "Quit")):
        failures += report(f"F{num} keeps its Commander meaning ({expected})",
                           set() if labels.get(num) == expected
                           else {f"F{num} is {labels.get(num)!r}"})

    # Every labelled key must actually be bound in the event loop, or the strip
    # advertises something that does nothing.
    loop = SRC[SRC.index("def loop("):]
    for num in labels:
        token = "KEY_DC" if num == "8" else f"KEY_F{num}"
        failures += report(f"F{num} ({labels[num]}) is bound in the loop",
                           set() if f"KEY_F{num}" in loop else {token})

    # Options, not just commands. --nested shipped CLI-only because nothing
    # checked this, which is the whole reason the check exists.
    tui = (ROOT / "vmorch" / "tui" / "app.py").read_text()
    missing_flags = set()
    for flag in cli_new_flags():
        key = FLAG_FIELDS.get(flag, flag.lstrip("-").replace("-", "_"))
        if f'"key": "{key}"' not in tui:
            missing_flags.add(flag)
    failures += report("every `vmorch new` option is settable in the TUI",
                       missing_flags)

    # And each must carry a description, so the TUI explains rather than lists.
    undescribed = set()
    for flag in cli_new_flags():
        key = FLAG_FIELDS.get(flag, flag.lstrip("-").replace("-", "_"))
        if f'"{key}":' not in tui.split("RISKY_WHEN_ON")[0]:
            undescribed.add(flag)
    failures += report("every option has a description in OPTION_HELP",
                       undescribed)

    print("FAILED" if failures else "TUI menu is complete and wired")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
