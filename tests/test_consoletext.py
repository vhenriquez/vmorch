"""Console text sanitising.

A serial console log is a terminal recording, not a text file. Rendered
verbatim into the curses pager, systemd's colour codes showed up as literal
`^[[0;32m` noise. These cases lock in the cleanup, and the reconstruction of
meaning from text markers rather than the escapes that were removed.

Run: python3 tests/test_consoletext.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmorch import consoletext as ct  # noqa: E402

ESC = "\x1b"

CASES = [
    # (label, raw, expected_clean, expected_class)
    (
        "the reported line",
        f"[{ESC}[0;32m  OK  {ESC}[0m] Finished {ESC}[0;1;39msnapd.seeded.service"
        f"{ESC}[0m - Wait until snapd is fully seeded",
        "[  OK  ] Finished snapd.seeded.service - Wait until snapd is fully seeded",
        ct.OK,
    ),
    (
        "failed unit stays identifiable after the colour is gone",
        f"[{ESC}[0;1;31mFAILED{ESC}[0m] Failed to start {ESC}[0;1;39mssh.service{ESC}[0m",
        "[FAILED] Failed to start ssh.service",
        ct.FAIL,
    ),
    (
        "carriage-return redraw keeps only what was on screen",
        "Starting foo...\rStarting foo... done",
        "Starting foo... done",
        ct.PLAIN,
    ),
    (
        "NULs from the serial line are dropped",
        "hello\x00\x00 world",
        "hello world",
        ct.PLAIN,
    ),
    (
        "cursor moves and erases go too",
        f"{ESC}[2J{ESC}[1;1Hboot{ESC}[K",
        "boot",
        ct.PLAIN,
    ),
    (
        "charset selection escapes",
        f"{ESC}(Bplain text",
        "plain text",
        ct.PLAIN,
    ),
    (
        "a guest that never emitted colour classifies the same",
        "[FAILED] Failed to start thing.service",
        "[FAILED] Failed to start thing.service",
        ct.FAIL,
    ),
    (
        "failure outranks an OK marker on the same line",
        "[  OK  ] Started x; [FAILED] Failed to start y",
        "[  OK  ] Started x; [FAILED] Failed to start y",
        ct.FAIL,
    ),
    (
        "kernel panic is a failure",
        "Kernel panic - not syncing: Attempted to kill init!",
        "Kernel panic - not syncing: Attempted to kill init!",
        ct.FAIL,
    ),
]


def main() -> int:
    failures = 0
    for label, raw, expected, kind in CASES:
        clean = ct.sanitize(raw)
        got_kind = ct.classify(clean)
        ok = clean == expected and got_kind == kind
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures += 1
            print(f"        expected {expected!r} [{kind}]")
            print(f"        got      {clean!r} [{got_kind}]")

    # No escape character may survive, whatever the input.
    noisy = "".join(f"{ESC}[{n}m x" for n in range(0, 110))
    if ESC in ct.sanitize(noisy):
        print("  FAIL an escape survived sanitising")
        failures += 1
    else:
        print("  ok   no escape survives sanitising")

    text = "\n".join(r for _, r, _, _ in CASES)
    print(f"\n  summary line: {ct.summary(ct.sanitize(text))}")
    print("FAILED" if failures else "console text handling is correct")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
