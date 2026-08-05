"""The docs must describe what the tool actually does.

A rule that says "update the docs when you change functionality" is a promise,
and promises rot. This is the same rule as a check: it walks the real surface of
the tool -- every CLI subcommand and flag, every config key, every catalogue
field -- and fails if any of it is missing from the documentation.

Adding a feature therefore breaks the build until it is written up. That is the
point.

The docs live outside this repo, so the path is configurable:

    VMORCH_DOCS=/path/to/docs python3 tests/test_docs_current.py

Run: python3 tests/test_docs_current.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOCS = Path(os.environ.get(
    "VMORCH_DOCS",
    Path.home() / "Documents/projects"
    "/08-vm-orchestrator-launch-operate-kill/docs",
))

#: Things deliberately undocumented, each with a reason. An entry here is a
#: decision; an entry missing from the docs with no entry here is an oversight.
EXEMPT: dict[str, str] = {
    "--help": "argparse builtin",
    "-h": "argparse builtin",
}


def docs_text() -> str:
    if not DOCS.exists():
        return ""
    return "\n".join(p.read_text() for p in sorted(DOCS.rglob("*.md")))


def cli_surface() -> tuple[set[str], set[str]]:
    """Subcommands and long flags, read from the parser definition.

    Nested subcommands come back as "net create", which is what a user types and
    what the docs must show. Matching on the bare word instead reported `create`
    as a top-level command and then looked for "vm create" in the docs.
    """
    src = (ROOT / "vmorch" / "cli.py").read_text()
    commands = set(re.findall(
        r'(?<![A-Za-z_])sub\.add_parser\(\s*"([a-z-]+)"', src))
    commands |= {f"net {c}" for c in
                 re.findall(r'netsub\.add_parser\(\s*"([a-z-]+)"', src)}
    flags = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))
    return commands, flags


def config_keys() -> set[str]:
    src = (ROOT / "vmorch" / "config.py").read_text()
    return set(re.findall(r'_(?:path|value)\(\s*"([a-z_]+)"', src))


def catalogue_fields() -> set[str]:
    """Fields a user can write in images.toml."""
    from vmorch.images import CatalogueEntry
    return {f for f in CatalogueEntry.__dataclass_fields__ if f != "key"}


def report(label: str, missing: set[str]) -> int:
    if missing:
        print(f"  FAIL {label}: undocumented -> {', '.join(sorted(missing))}")
        return 1
    print(f"  ok   {label}")
    return 0


def main() -> int:
    text = docs_text()
    if not text:
        print(f"  FAIL docs not found at {DOCS}")
        print("       set VMORCH_DOCS if they live elsewhere")
        return 1

    failures = 0
    commands, flags = cli_surface()

    failures += report(
        "every CLI command is documented",
        {c for c in commands if f"vm {c}" not in text and c not in EXEMPT},
    )
    failures += report(
        "every CLI flag is documented",
        {f for f in flags if f not in text and f not in EXEMPT},
    )
    failures += report(
        "every config key is documented",
        {k for k in config_keys() if k not in text},
    )
    failures += report(
        "every images.toml field is documented",
        {f for f in catalogue_fields() if f not in text},
    )

    # The four-document structure is the agreed shape; a stray top-level doc
    # usually means something was written where nobody will look for it.
    expected = {"user-guide.md", "agent-sandbox-use-case.md", "build-log.md",
                "host-capability-check.md"}
    stray = {p.name for p in DOCS.glob("*.md")} - expected
    failures += report("no undeclared top-level docs", stray)

    for name in ("getting-started.md", "how-to.md", "reference.md",
                 "troubleshooting.md"):
        if not (DOCS / "guide" / name).exists():
            print(f"  FAIL missing guide/{name}")
            failures += 1
    if not failures:
        print("  ok   the four guides are present")

    print("FAILED" if failures else "docs match the tool")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
