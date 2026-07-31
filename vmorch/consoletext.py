"""Make raw guest console output fit to display.

A serial console log is a recording of a terminal session, not a text file. It
carries the escape sequences systemd uses to colour its boot messages, carriage
returns from progress redraws, and stray NULs from the serial line. Printed
verbatim into a curses pager that all shows up as literal `^[[0;32m` noise:

    [^[[0;32m  OK  ^[[0m] Finished ^[[0;1;39msnapd.seeded.service^[[0m - ...

Stripping the escapes throws away real signal though -- the colour is how you
spot a failed unit at a glance while scrolling a thousand lines. So the
sequences come out and the *meaning* is reconstructed from the text markers,
which is more robust anyway: it works the same whether the guest emitted colour
or not.
"""

from __future__ import annotations

import re

# CSI: ESC [ params intermediates final. Covers SGR colour, cursor moves,
# erases -- everything systemd and the kernel emit.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# OSC: ESC ] ... BEL or ST. Terminal title sets, mostly.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
# Two- and three-character escapes: charset selection, keypad modes.
_ESC_SHORT = re.compile(r"\x1b[()#][0-9A-Za-z]|\x1b[=>NOMc78]")

#: Boot-message classes worth colouring. Ordered: the first match wins, so
#: failures outrank the ordinary OK lines they may sit next to.
FAIL = "fail"
WARN = "warn"
OK = "ok"
PLAIN = "plain"

_FAIL_MARKS = ("[FAILED]", "[ FAILED ]", "Failed to start", "Failed to mount",
               "Kernel panic", "Call Trace:", "segfault", "Out of memory")
_WARN_MARKS = ("[DEPEND]", "[ SKIP ]", "[WARNING]", "Warning:", "Dependency failed",
               "timed out", "Timed out")
_OK_MARKS = ("[  OK  ]", "[ OK ]", "Reached target", "Finished ", "Started ")


def strip_escapes(text: str) -> str:
    """Remove terminal control sequences, keeping the visible characters."""
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC_SHORT.sub("", text)
    return text


def _flatten_carriage_returns(line: str) -> str:
    """Show what a terminal would finally have displayed on this line.

    Progress output redraws in place with \\r. Keeping every segment would
    triple the apparent length of the log with text that was never on screen at
    once; keeping the last is what the user actually saw.
    """
    line = line.rstrip("\r")
    if "\r" not in line:
        return line
    segments = [s for s in line.split("\r") if s]
    return segments[-1] if segments else ""


def sanitize(text: str, tab_width: int = 4) -> str:
    """Turn a raw console recording into displayable lines."""
    text = strip_escapes(text.replace("\x00", ""))
    out = []
    for raw in text.split("\n"):
        line = _flatten_carriage_returns(raw)
        line = line.replace("\t", " " * tab_width)
        # Drop any remaining C0 controls; they render as garbage in curses.
        line = "".join(ch for ch in line if ch >= " " or ch == " ")
        out.append(line.rstrip())
    return "\n".join(out)


def classify(line: str) -> str:
    """Which highlight class a boot line belongs to.

    Reads the text markers rather than the escape codes that were stripped, so
    it behaves the same on a guest that never emitted colour at all.
    """
    for mark in _FAIL_MARKS:
        if mark in line:
            return FAIL
    for mark in _WARN_MARKS:
        if mark in line:
            return WARN
    for mark in _OK_MARKS:
        if mark in line:
            return OK
    return PLAIN


def summary(text: str) -> str:
    """One-line count of anything notable, for the status bar."""
    fails = warns = 0
    for line in text.split("\n"):
        kind = classify(line)
        fails += kind == FAIL
        warns += kind == WARN
    if not fails and not warns:
        return "no failures in console"
    parts = []
    if fails:
        parts.append(f"{fails} failed")
    if warns:
        parts.append(f"{warns} warning{'s' if warns != 1 else ''}")
    return ", ".join(parts)
