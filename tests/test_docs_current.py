"""The docs must describe what the tool actually does.

A feature that is not documented is not finished. This walks the real surface of
the tool -- every CLI subcommand, every flag, every config key -- and fails if
any of it is missing from the docs.

**This test previously pointed outside the repository**, at a directory on one
machine, so it could only ever pass there and failed on every clone. It was
deleted for that reason. This version reads only files that ship, so it works
anywhere -- which is the whole difference between enforcing a rule and pretending
to.

Its value is not theoretical: the first run of the original found a config field
users could set that nothing read. Dead surface is easier to spot from the docs
than from the code.

Run: python3 tests/test_docs_current.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Every markdown file that ships. Docs are plural on purpose: a flag explained
#: in CONTRIBUTING or SECURITY counts as documented.
DOC_FILES = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
DOC_FILES += sorted((ROOT / "docs").glob("*.md"))

#: Deliberately undocumented, each with a reason. An entry here is a decision on
#: the record; something missing with no entry here is an oversight.
EXEMPT: dict[str, str] = {
    "--help": "argparse builtin",
    "-h": "argparse builtin",
    "--keep-build-box": "debugging aid for image builds, not a user-facing task",
}


def docs_text() -> str:
    return "\n".join(f.read_text() for f in DOC_FILES if f.exists())


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print(f"        {detail}")
    return 0 if ok else 1


def main() -> int:
    missing = [f for f in DOC_FILES if not f.exists()]
    if missing:
        print(f"  FAIL missing doc files: {[str(f) for f in missing]}")
        return 1

    from vmorch import config
    from vmorch.cli import EXAMPLE_CONFIG, build_parser

    text = docs_text()
    failures = 0

    parser = build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "_name_parser_map"))

    undocumented_cmds, undocumented_flags = [], []
    for name, sp in sub._name_parser_map.items():
        if not re.search(rf"vmorch {re.escape(name)}\b", text):
            undocumented_cmds.append(name)
        for action in sp._actions:
            for flag in action.option_strings:
                if flag in EXEMPT or flag in text:
                    continue
                undocumented_flags.append(f"{name} {flag}")

    failures += check(
        "every subcommand appears in the docs", not undocumented_cmds,
        f"missing: {undocumented_cmds}")
    failures += check(
        "every flag appears in the docs", not undocumented_flags,
        f"missing: {undocumented_flags}\n        "
        "Document it, or add it to EXEMPT with a reason.")

    # Config keys, from the starter file the tool itself writes. A key offered
    # there and read nowhere is dead surface; a key read and never offered is
    # undiscoverable.
    offered = set(re.findall(r"^# (\w+)\s*=", EXAMPLE_CONFIG, re.M))
    undocumented_keys = sorted(k for k in offered if k not in text)
    failures += check(
        "every config key in the starter file is documented",
        not undocumented_keys, f"missing: {undocumented_keys}")

    dead = sorted(k for k in offered if f'"{k}"' not in
                  (ROOT / "vmorch" / "config.py").read_text())
    failures += check(
        "every config key offered is actually read", not dead,
        f"offered but never read by config.py: {dead}")

    # The paths users are told to expect.
    for label, path in (("state dir", config.STATE_DIR.name),
                        ("bases", config.BASES_DIR.name),
                        ("boxes", config.BOXES_DIR.name),
                        ("cache", config.DOWNLOAD_CACHE.name)):
        failures += check(f"the {label} directory is named in the docs",
                          path in text, f"{path!r} appears in no doc file")

    print("FAILED" if failures else "docs cover the whole surface")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
