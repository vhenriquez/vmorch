"""Drawing primitives and modal dialogs.

Norton Commander styling: blue field, cyan framed panels, a reverse-video
selection bar, and a function-key strip pinned to the last line.

Everything here is synchronous and main-thread only. curses is not thread safe,
so background work hands results back through a queue and only the main loop
ever touches the screen -- see `run_task`.
"""

from __future__ import annotations

import curses
import queue
import threading
import time

# Colour pair ids
FIELD = 1        # the desktop behind the panels
PANEL = 2        # panel interior
FRAME = 3        # panel borders and titles
SELECT = 4       # selection bar, active panel
SELECT_DIM = 5   # selection bar, inactive panel
KEYBAR_N = 6     # the digit in "1Help"
KEYBAR_L = 7     # the label in "1Help"
TITLE = 8        # top bar
WARN = 9         # writable shares, destructive prompts
OK = 10          # running / healthy
DIM = 11         # secondary text
DIALOG = 12      # dialog interior
STATUS = 13      # the status line
SHADOW = 14      # dialog drop shadow

# --------------------------------------------------------------------------
# Palette
#
# Colours 0-15 are whatever the user's terminal theme decides they are. A theme
# that renders "white" as pale mint and "blue" as light periwinkle turns
# white-on-blue into roughly 1.5:1 -- unreadable, which is exactly what the
# first version of this shipped as.
#
# Colours 16-255 are FIXED RGB in the xterm-256 palette and ignore the theme
# entirely, so every pair below has a contrast ratio that can be computed in
# advance. The numbers in the comments are WCAG contrast against the navy
# background; AAA wants 7:1 for body text and all of these clear it.
# --------------------------------------------------------------------------

C_BG = 17          # #00005f  deep navy, the Commander field
C_BG_DLG = 19      # #0000af  lighter navy so dialogs lift off the panels
C_BG_STATUS = 18   # #000087  a lighter navy: distinct from the field,
                   #          and 15.6:1 against white rather than 7.0:1
C_BLACK = 16       # #000000
C_TEXT = 231       # #ffffff  18.0:1 on navy
C_FRAME = 45       # #00d7ff  10.4:1
C_DIM = 146        # #afafd7   8.5:1 -- dim to the eye, still far above AAA
C_WARN = 226       # #ffff00  16.8:1
C_OK = 82          # #5fff00  13.5:1
C_SEL = 45         # selection bar; black on it is 12.2:1
C_SEL_DIM = 250    # #bcbcbc  grey bar for the UNfocused panel: reads as
                   #          inactive next to cyan, still 11.1:1

_PAIRS_256 = {
    FIELD:      (C_FRAME, C_BG),
    PANEL:      (C_TEXT, C_BG),
    FRAME:      (C_FRAME, C_BG),
    SELECT:     (C_BLACK, C_SEL),
    SELECT_DIM: (C_BLACK, C_SEL_DIM),
    KEYBAR_N:   (C_TEXT, C_BLACK),
    KEYBAR_L:   (C_BLACK, C_SEL),
    TITLE:      (C_BLACK, C_SEL),
    WARN:       (C_WARN, C_BG),
    OK:         (C_OK, C_BG),
    DIM:        (C_DIM, C_BG),
    DIALOG:     (C_TEXT, C_BG_DLG),
    STATUS:     (C_TEXT, C_BG_STATUS),
    SHADOW:     (C_BLACK, C_BLACK),
}

# Fallback for 8-colour terminals. Body text is bold white rather than cyan,
# because cyan-on-blue is the pair that fails hardest on a light theme.
_PAIRS_8 = {
    FIELD:      (curses.COLOR_WHITE, curses.COLOR_BLUE),
    PANEL:      (curses.COLOR_WHITE, curses.COLOR_BLUE),
    FRAME:      (curses.COLOR_CYAN, curses.COLOR_BLUE),
    SELECT:     (curses.COLOR_BLACK, curses.COLOR_CYAN),
    SELECT_DIM: (curses.COLOR_WHITE, curses.COLOR_BLACK),
    KEYBAR_N:   (curses.COLOR_WHITE, curses.COLOR_BLACK),
    KEYBAR_L:   (curses.COLOR_BLACK, curses.COLOR_CYAN),
    TITLE:      (curses.COLOR_BLACK, curses.COLOR_CYAN),
    WARN:       (curses.COLOR_YELLOW, curses.COLOR_BLUE),
    OK:         (curses.COLOR_GREEN, curses.COLOR_BLUE),
    DIM:        (curses.COLOR_WHITE, curses.COLOR_BLUE),
    DIALOG:     (curses.COLOR_WHITE, curses.COLOR_BLUE),
    STATUS:     (curses.COLOR_WHITE, curses.COLOR_BLACK),
    SHADOW:     (curses.COLOR_BLACK, curses.COLOR_BLACK),
}

_using_256 = False


def init_colors() -> None:
    global _using_256
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    _using_256 = curses.COLORS >= 256
    table = _PAIRS_256 if _using_256 else _PAIRS_8
    for pair, (fg, bg) in table.items():
        try:
            curses.init_pair(pair, fg, bg)
        except curses.error:
            # A terminal that claims 256 colours but refuses the pair: fall
            # back rather than crash into a monochrome mess.
            curses.init_pair(pair, *_PAIRS_8[pair])


def attr(pair: int, bold: bool = False) -> int:
    a = curses.color_pair(pair)
    return a | curses.A_BOLD if bold else a


def put(win, y: int, x: int, text: str, a: int = 0) -> None:
    """Write text, clipped to the window. curses raises at the last cell."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        text, x = text[-x:], 0
    space = w - x
    if space <= 0:
        return
    text = text[:space]
    try:
        win.addstr(y, x, text, a)
    except curses.error:
        # Writing the bottom-right cell always raises; the glyph still lands.
        pass


def frame(win, y: int, x: int, h: int, w: int, title: str = "",
          a: int | None = None, double: bool = False) -> None:
    a = attr(FRAME, bold=True) if a is None else a
    tl, tr, bl, br, hz, vt = ("╔", "╗", "╚", "╝", "═", "║") if double else \
                             ("┌", "┐", "└", "┘", "─", "│")
    put(win, y, x, tl + hz * (w - 2) + tr, a)
    for i in range(1, h - 1):
        put(win, y + i, x, vt, a)
        put(win, y + i, x + w - 1, vt, a)
    put(win, y + h - 1, x, bl + hz * (w - 2) + br, a)
    if title:
        label = f" {title} "[: max(0, w - 4)]
        put(win, y, x + (w - len(label)) // 2, label, a)


def fill(win, y: int, x: int, h: int, w: int, a: int) -> None:
    for i in range(h):
        put(win, y + i, x, " " * w, a)


def shadow(win, y: int, x: int, h: int, w: int) -> None:
    """A dropped shadow, so dialogs read as floating above the panels.

    Black on black. Drawing this in DIM painted navy spaces onto a navy field,
    which is to say it drew nothing at all.
    """
    dark = attr(SHADOW)
    for i in range(1, h):
        put(win, y + i, x + w, "  ", dark)
    put(win, y + h, x + 2, " " * w, dark)


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------

def _centred_box(stdscr, h: int, w: int, title: str):
    sh, sw = stdscr.getmaxyx()
    w = min(w, sw - 4)
    h = min(h, sh - 4)
    y, x = (sh - h) // 2, (sw - w) // 2
    shadow(stdscr, y, x, h, w)
    fill(stdscr, y, x, h, w, attr(DIALOG))
    frame(stdscr, y, x, h, w, title, attr(DIALOG, bold=True), double=True)
    return y, x, h, w


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        if not para:
            out.append("")
            continue
        line = ""
        for word in para.split():
            if len(line) + len(word) + 1 > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return out


def message(stdscr, title: str, text: str, kind: int = DIALOG) -> None:
    lines = _wrap(text, 60)
    h, w = len(lines) + 4, max(len(l) for l in lines + [title]) + 6
    y, x, h, w = _centred_box(stdscr, h, w, title)
    for i, line in enumerate(lines[: h - 4]):
        put(stdscr, y + 1 + i, x + 2, line,
            attr(WARN, bold=True) if kind == WARN else attr(DIALOG))
    hint = "[ OK ]"
    put(stdscr, y + h - 2, x + (w - len(hint)) // 2, hint, attr(SELECT))
    stdscr.refresh()
    while stdscr.getch() not in (10, 13, 27, ord(" ")):
        pass


def error(stdscr, text: str) -> None:
    message(stdscr, "Error", text, kind=WARN)


def confirm(stdscr, title: str, text: str, danger: bool = False) -> bool:
    """Yes/No. Defaults to No when the action is destructive."""
    lines = _wrap(text, 58)
    h, w = len(lines) + 5, max(len(l) for l in lines + [title]) + 6
    y, x, h, w = _centred_box(stdscr, h, w, title)
    for i, line in enumerate(lines):
        put(stdscr, y + 1 + i, x + 2, line,
            attr(WARN, bold=True) if danger else attr(DIALOG))

    choice = not danger          # destructive prompts start on "No"
    while True:
        for i, (label, val) in enumerate((("  Yes  ", True), ("  No  ", False))):
            a = attr(SELECT, bold=True) if choice == val else attr(DIALOG)
            put(stdscr, y + h - 2, x + w // 2 - 9 + i * 10, label, a)
        stdscr.refresh()
        k = stdscr.getch()
        if k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("\t")):
            choice = not choice
        elif k in (ord("y"), ord("Y")):
            return True
        elif k in (ord("n"), ord("N"), 27):
            return False
        elif k in (10, 13):
            return choice


def prompt(stdscr, title: str, label: str, default: str = "") -> str | None:
    """Single-line input. Returns None if cancelled."""
    w = max(len(title), len(label), 44) + 6
    y, x, h, w = _centred_box(stdscr, 7, w, title)
    put(stdscr, y + 1, x + 2, label, attr(DIALOG))
    put(stdscr, y + h - 2, x + 2, "Enter accept   Esc cancel", attr(DIM))

    buf, pos = list(default), len(default)
    field_w = w - 6
    curses.curs_set(1)
    try:
        while True:
            view = "".join(buf)
            start = max(0, pos - field_w + 1)
            put(stdscr, y + 3, x + 2, " " * field_w, attr(SELECT))
            put(stdscr, y + 3, x + 2, view[start:start + field_w], attr(SELECT))
            stdscr.move(y + 3, x + 2 + min(pos - start, field_w - 1))
            stdscr.refresh()

            k = stdscr.getch()
            if k == 27:
                return None
            if k in (10, 13):
                return "".join(buf).strip()
            if k in (curses.KEY_BACKSPACE, 127, 8):
                if pos:
                    del buf[pos - 1]
                    pos -= 1
            elif k == curses.KEY_DC:
                if pos < len(buf):
                    del buf[pos]
            elif k == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif k == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif k == curses.KEY_HOME:
                pos = 0
            elif k == curses.KEY_END:
                pos = len(buf)
            elif 32 <= k < 127:
                buf.insert(pos, chr(k))
                pos += 1
    finally:
        curses.curs_set(0)


def choose(stdscr, title: str, options: list[tuple[str, object]],
           note: str = "") -> object | None:
    """Pick one of a list. Returns the chosen value, or None if cancelled."""
    labels = [o[0] for o in options]
    body = _wrap(note, 54) if note else []
    w = max([len(l) for l in labels + body + [title]]) + 8
    h = len(labels) + len(body) + 4
    y, x, h, w = _centred_box(stdscr, h, w, title)

    for i, line in enumerate(body):
        put(stdscr, y + 1 + i, x + 2, line, attr(WARN, bold=True))
    top = y + 1 + len(body)

    idx = 0
    while True:
        for i, label in enumerate(labels):
            a = attr(SELECT, bold=True) if i == idx else attr(DIALOG)
            put(stdscr, top + i, x + 2, f" {label.ljust(w - 6)}", a)
        put(stdscr, y + h - 2, x + 2, "Enter select   Esc cancel", attr(DIM))
        stdscr.refresh()
        k = stdscr.getch()
        if k == 27:
            return None
        if k in (10, 13):
            return options[idx][1]
        if k in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(labels)
        elif k in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(labels)


def pager(stdscr, title: str, text: str) -> None:
    """Scrollable read-only view, for console logs and long output."""
    sh, sw = stdscr.getmaxyx()
    h, w = sh - 4, sw - 6
    y, x, h, w = _centred_box(stdscr, h, w, title)
    lines = text.replace("\t", "    ").split("\n")
    view_h = h - 3
    top = max(0, len(lines) - view_h)      # open at the end, like tail

    while True:
        for i in range(view_h):
            put(stdscr, y + 1 + i, x + 2, " " * (w - 4), attr(DIALOG))
            if top + i < len(lines):
                put(stdscr, y + 1 + i, x + 2, lines[top + i][: w - 4],
                    attr(DIALOG))
        pos = f" {top + 1}-{min(top + view_h, len(lines))} of {len(lines)} "
        put(stdscr, y + h - 1, x + w - len(pos) - 3, pos, attr(FRAME, bold=True))
        put(stdscr, y + h - 2, x + 2,
            "↑↓ PgUp PgDn Home End   Esc close", attr(DIM))
        stdscr.refresh()

        k = stdscr.getch()
        if k in (27, ord("q"), 10, 13):
            return
        if k in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            top = min(max(0, len(lines) - view_h), top + 1)
        elif k == curses.KEY_PPAGE:
            top = max(0, top - view_h)
        elif k == curses.KEY_NPAGE:
            top = min(max(0, len(lines) - view_h), top + view_h)
        elif k == curses.KEY_HOME:
            top = 0
        elif k == curses.KEY_END:
            top = max(0, len(lines) - view_h)


SPINNER = "|/-\\"


def run_task(stdscr, title: str, fn, note: str = ""):
    """Run a slow operation without freezing the UI.

    Box creation and `apply` take tens of seconds. The work happens on a worker
    thread and only this loop draws, because curses is not thread safe. Returns
    (ok, result_or_exception).
    """
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            result.put((True, fn()))
        except BaseException as exc:              # noqa: BLE001
            result.put((False, exc))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    lines = _wrap(note, 46) if note else []
    w = max([len(title)] + [len(l) for l in lines] + [40]) + 8
    h = 5 + len(lines)
    y, x, h, w = _centred_box(stdscr, h, w, title)
    for i, line in enumerate(lines):
        put(stdscr, y + 1 + i, x + 3, line, attr(DIALOG))

    stdscr.nodelay(True)
    tick = 0
    try:
        while thread.is_alive():
            msg = f" {SPINNER[tick % len(SPINNER)]}  working... "
            put(stdscr, y + h - 3, x + 3, msg.ljust(w - 6), attr(DIALOG, bold=True))
            stdscr.refresh()
            time.sleep(0.1)
            tick += 1
            stdscr.getch()          # drain keys so they do not queue up
    finally:
        stdscr.nodelay(False)

    thread.join()
    return result.get()
