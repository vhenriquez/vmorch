"""Check that every TUI colour pair is actually readable.

The first version of the TUI used the 8-colour names. Colours 0-15 are remapped
by the terminal theme, and on a light theme "white on blue" came out around
1.5:1 -- text you could barely see. The fix was to move to the fixed 16-255
region of the xterm-256 palette, where the RGB values are known and the contrast
can be computed rather than guessed.

This test is that computation. WCAG AA wants 4.5:1 for body text and AAA wants
7:1; every pair here is expected to clear AAA.

Run: python3 tests/test_contrast.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmorch.tui import ui  # noqa: E402

AAA = 7.0


def xterm_rgb(idx: int) -> tuple[int, int, int]:
    """RGB for an xterm-256 palette index, for the fixed 16-255 region."""
    if 16 <= idx <= 231:
        i = idx - 16
        levels = [0, 95, 135, 175, 215, 255]
        return (levels[i // 36], levels[(i // 6) % 6], levels[i % 6])
    if 232 <= idx <= 255:
        v = 8 + (idx - 232) * 10
        return (v, v, v)
    raise ValueError(f"{idx} is in the theme-dependent 0-15 range")


def luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: int, bg: int) -> float:
    a, b = luminance(xterm_rgb(fg)), luminance(xterm_rgb(bg))
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


NAMES = {
    ui.FIELD: "field", ui.PANEL: "panel text", ui.FRAME: "frames",
    ui.SELECT: "selection (active)", ui.SELECT_DIM: "selection (inactive)",
    ui.KEYBAR_N: "keybar digit", ui.KEYBAR_L: "keybar label",
    ui.TITLE: "title bar", ui.WARN: "warning / writable share",
    ui.OK: "running marker", ui.DIM: "dim text", ui.DIALOG: "dialog text",
    ui.STATUS: "status line", ui.SHADOW: "dialog shadow",
}


# Pairs that never carry text, so a contrast ratio is meaningless for them.
# The drop shadow is deliberately black on black.
NON_TEXT = {ui.SHADOW}


def main() -> int:
    failures = 0
    print(f"{'pair':<26} {'fg':>4} {'bg':>4} {'ratio':>7}")
    for pair, (fg, bg) in sorted(ui._PAIRS_256.items()):
        ratio = contrast(fg, bg)
        if pair in NON_TEXT:
            print(f"{NAMES.get(pair, str(pair)):<26} {fg:>4} {bg:>4} "
                  f"{'—':>8}  (carries no text)")
            continue
        flag = "ok " if ratio >= AAA else "LOW"
        if ratio < AAA:
            failures += 1
        print(f"{NAMES.get(pair, str(pair)):<26} {fg:>4} {bg:>4} "
              f"{ratio:>6.1f}:1  {flag}")

    # The old scheme, for comparison, assuming a theme that maps these to the
    # pale values seen in the reported screenshot.
    print(f"\nAAA threshold is {AAA}:1 for body text.")
    print("FAILED" if failures else "every pair clears WCAG AAA")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
