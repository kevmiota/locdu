"""Interactive ncdu-style browser."""
from __future__ import annotations

import curses
import locale
import os
import shlex
import subprocess
from typing import Callable

from . import __version__
from .output import fmt, printable
from .scan import METRICS, Node, ScanResult, by_file_type, count_lines

METRIC_LABEL = {"lines": "lines", "code": "code lines", "files": "files"}

HELP = [
    "up/down, j/k       move",
    "right, enter, l    open directory",
    "left, h, backspace parent directory",
    "pgup/pgdn, g/G     page up/down, top, bottom",
    "",
    "m                  metric: lines -> code lines -> files",
    "s / n              sort by count / by name (again: reverse)",
    "t                  file types in this directory",
    "e                  open the selected file in $EDITOR, recount after",
    "d / u              hide the selected item / undo (view only)",
    "r                  rescan from disk",
    "q                  quit",
    "",
    "lines = all lines, code = non-blank lines (comments count as code).",
    "Binary files are skipped. Nothing on disk is ever changed.",
]


def put(win, y: int, x: int, text: str, attr: int = 0) -> int:
    """Write text clipped to the window; returns the x after it."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return x
    text = printable(text)[: w - x]
    try:
        win.addstr(y, x, text, attr)
    except (curses.error, UnicodeEncodeError):
        # curses raises after writing the bottom-right cell; the text is drawn anyway.
        pass
    return x + len(text)


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


class App:
    def __init__(self, scr, result: ScanResult, rescan: Callable[[], ScanResult], metric: str):
        self.scr = scr
        self.rescan = rescan
        self.metric = metric
        self.sort_by = "count"
        self.desc = True
        self.hidden: list[Node] = []
        self.msg = ""
        self.load(result)
        self.setup_colors()
        scr.keypad(True)

    def load(self, result: ScanResult, path: str = "") -> None:
        self.result = result
        self.root = result.root
        node = self.root.find(path) or self.root
        while not node.is_dir:
            node = node.parent
        self.cur = node
        self.sel = 0
        self.top = 0

    def setup_colors(self) -> None:
        self.c_bar = 0
        self.c_dir = curses.A_BOLD
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if os.environ.get("NO_COLOR") or not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, bg)
        curses.init_pair(2, curses.COLOR_CYAN, bg)
        self.c_bar = curses.color_pair(1)
        self.c_dir = curses.color_pair(2) | curses.A_BOLD

    # --- data -------------------------------------------------------------

    def items(self) -> list[Node]:
        kids = sorted(self.cur.children.values(), key=lambda n: n.name.lower())
        if self.sort_by == "count":
            kids.sort(key=lambda n: n.value(self.metric), reverse=self.desc)
        elif self.desc:
            kids.reverse()
        return kids

    def display_path(self, node: Node) -> str:
        rel = node.relpath()
        full = os.path.join(self.root.name, rel) if rel else self.root.name
        home = os.path.expanduser("~")
        if full == home or full.startswith(home + os.sep):
            full = "~" + full[len(home):]
        return full

    def sort_label(self) -> str:
        arrow = "desc" if self.desc else "asc"
        return f"{self.sort_by} {arrow}"

    # --- drawing ----------------------------------------------------------

    def list_height(self) -> int:
        return max(1, self.scr.getmaxyx()[0] - 4)

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 6 or w < 40:
            put(scr, 0, 0, "terminal too small")
            scr.refresh()
            return

        items = self.items()
        m = self.metric
        list_h = h - 4
        self.sel = max(0, min(self.sel, len(items) - 1))
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + list_h:
            self.top = self.sel - list_h + 1
        self.top = max(0, min(self.top, max(0, len(items) - list_h)))

        put(scr, 0, 0, f" locdu {__version__} ~ arrows to navigate, ? for help, q to quit".ljust(w), curses.A_REVERSE)
        put(scr, 1, 0, f"--- {self.display_path(self.cur)} ".ljust(w, "-"))

        total = self.cur.value(m)
        biggest = max((n.value(m) for n in items), default=0)
        count_w = max([len(fmt(n.value(m))) for n in items] + [5])
        bar_w = 20 if w >= 100 else 10
        for row, node in enumerate(items[self.top : self.top + list_h]):
            y = 2 + row
            selected = self.top + row == self.sel
            base = curses.A_REVERSE if selected else 0
            value = node.value(m)
            pct = 100 * value / total if total else 0.0
            filled = round(bar_w * value / biggest) if biggest else 0
            x = put(scr, y, 0, f" {fmt(value):>{count_w}} {pct:5.1f}% [", base)
            x = put(scr, y, x, "#" * filled, base | (0 if selected else self.c_bar))
            x = put(scr, y, x, " " * (bar_w - filled) + "] ", base)
            name = ("/" + node.name) if node.is_dir else (" " + node.name)
            attr = base | (curses.A_BOLD if selected and node.is_dir else self.c_dir if node.is_dir else 0)
            put(scr, y, x, clip(name, w - x).ljust(w - x), attr)
        if not items:
            put(scr, 2, 2, "(empty)", curses.A_DIM)

        if items:
            node = items[self.sel]
            detail = (
                f" {node.relpath()}{'/' if node.is_dir else ''}: {fmt(node.lines)} lines, "
                f"{fmt(node.code)} code, {fmt(node.lines - node.code)} blank"
            )
            if node.is_dir:
                detail += f", {fmt(node.files)} files"
            put(scr, h - 2, 0, clip(detail, w), curses.A_DIM)

        if self.msg:
            status = f" {self.msg}"
        else:
            status = f" Total: {fmt(total)} {METRIC_LABEL[m]}"
            if m != "files":
                status += f" in {fmt(self.cur.files)} files"
            status += f" | sort: {self.sort_label()}"
            if self.hidden:
                status += f" | hidden: {len(self.hidden)}"
            if self.result.skipped:
                status += f" | {fmt(self.result.skipped)} binary/large skipped"
        put(scr, h - 1, 0, clip(status, w).ljust(w), curses.A_REVERSE)
        scr.refresh()

    def modal(self, title: str, lines: list[str]) -> None:
        """Scrollable box over the current view; any key other than scrolling closes it."""
        top = 0
        while True:
            self.draw()
            h, w = self.scr.getmaxyx()
            box_w = min(w - 2, max(len(title) + 6, max(map(len, lines), default=0) + 4))
            box_h = min(h - 2, len(lines) + 2)
            if box_w < 10 or box_h < 3:
                return
            win = curses.newwin(box_h, box_w, (h - box_h) // 2, (w - box_w) // 2)
            win.keypad(True)
            view = box_h - 2
            top = max(0, min(top, len(lines) - view))
            win.erase()
            win.box()
            put(win, 0, 2, f" {clip(title, box_w - 6)} ", curses.A_BOLD)
            for i, line in enumerate(lines[top : top + view]):
                put(win, 1 + i, 2, clip(line, box_w - 4))
            hint = " up/down to scroll, any other key closes " if len(lines) > view else " any key closes "
            put(win, box_h - 1, max(1, box_w - len(hint) - 2), clip(hint, box_w - 2))
            win.refresh()
            key = win.getch()
            if key in (curses.KEY_UP, ord("k")):
                top -= 1
            elif key in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif key == curses.KEY_PPAGE:
                top -= view
            elif key == curses.KEY_NPAGE:
                top += view
            elif key != curses.KEY_RESIZE:
                return

    # --- actions ----------------------------------------------------------

    def go_up(self) -> None:
        if self.cur.parent is None:
            return
        child, self.cur = self.cur, self.cur.parent
        items = self.items()
        self.sel = items.index(child) if child in items else 0

    def open_selected(self, items: list[Node]) -> None:
        if items and items[self.sel].is_dir:
            self.cur = items[self.sel]
            self.sel = 0
            self.top = 0

    def hide(self, items: list[Node]) -> None:
        if not items:
            return
        node = items[self.sel]
        node.detach()
        self.hidden.append(node)
        self.msg = f"hid {node.name} from the view, u to undo (files are untouched)"

    def unhide(self) -> None:
        if not self.hidden:
            self.msg = "nothing hidden"
            return
        node = self.hidden.pop()
        node.reattach()
        self.msg = f"restored {node.relpath()}"

    def edit(self, items: list[Node]) -> None:
        if not items or items[self.sel].is_dir:
            self.msg = "e opens files, not directories"
            return
        node = items[self.sel]
        path = os.path.join(self.root.name, node.relpath())
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call([*shlex.split(editor), path])
        except OSError as e:
            self.msg = f"could not start {editor}: {e.strerror}"
        finally:
            curses.reset_prog_mode()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.scr.clear()
        counts = count_lines(path)
        if counts is not None:
            node.set_counts(*counts)

    def file_types(self) -> None:
        idx = 1 + METRICS.index(self.metric)
        rows = sorted(by_file_type(self.cur), key=lambda r: (-r[idx], r[0]))
        total = self.cur.value(self.metric)
        count_w = max([len(fmt(r[idx])) for r in rows] + [5])
        lines = [
            f"{fmt(r[idx]):>{count_w}} {100 * r[idx] / total if total else 0:5.1f}%  {r[0]}  ({fmt(r[3])} files)"
            for r in rows
        ]
        title = f"File types in {self.display_path(self.cur)} by {METRIC_LABEL[self.metric]}"
        self.modal(title, lines or ["(no files)"])

    def do_rescan(self) -> None:
        path = self.cur.relpath()
        hidden = [n.relpath() for n in self.hidden]
        h, w = self.scr.getmaxyx()
        put(self.scr, h - 1, 0, " rescanning...".ljust(w), curses.A_REVERSE)
        self.scr.refresh()
        self.load(self.rescan(), path)
        self.hidden = []
        for rel in hidden:
            node = self.root.find(rel)
            if node is not None and node.parent is not None:
                node.detach()
                self.hidden.append(node)
        self.msg = f"rescanned: {fmt(self.root.files)} files"

    def loop(self) -> None:
        while True:
            self.draw()
            key = self.scr.getch()
            self.msg = ""
            items = self.items()
            if key == ord("q"):
                return
            elif key in (curses.KEY_UP, ord("k")):
                self.sel -= 1
            elif key in (curses.KEY_DOWN, ord("j")):
                self.sel += 1
            elif key == curses.KEY_PPAGE:
                self.sel -= self.list_height()
            elif key == curses.KEY_NPAGE:
                self.sel += self.list_height()
            elif key in (curses.KEY_HOME, ord("g")):
                self.sel = 0
            elif key in (curses.KEY_END, ord("G")):
                self.sel = len(items) - 1
            elif key in (curses.KEY_RIGHT, curses.KEY_ENTER, ord("l"), 10, 13):
                self.open_selected(items)
            elif key in (curses.KEY_LEFT, curses.KEY_BACKSPACE, ord("h"), 127, 8):
                self.go_up()
            elif key == ord("m"):
                self.metric = METRICS[(METRICS.index(self.metric) + 1) % len(METRICS)]
                self.msg = f"metric: {METRIC_LABEL[self.metric]}"
            elif key in (ord("s"), ord("n")):
                by = "count" if key == ord("s") else "name"
                self.desc = not self.desc if self.sort_by == by else by == "count"
                self.sort_by = by
            elif key == ord("d"):
                self.hide(items)
            elif key == ord("u"):
                self.unhide()
            elif key == ord("e"):
                self.edit(items)
            elif key == ord("t"):
                self.file_types()
            elif key == ord("r"):
                self.do_rescan()
            elif key == ord("?"):
                self.modal("locdu help", HELP)


def run(result: ScanResult, rescan: Callable[[], ScanResult], metric: str = "lines") -> None:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda scr: App(scr, result, rescan, metric).loop())
