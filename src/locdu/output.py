"""Non-interactive output: a text tree and ncdu's JSON export format."""
from __future__ import annotations

import os
import time
from typing import Callable

from . import __version__
from .scan import Node


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", "'")


def printable(text: str) -> str:
    """Show bytes of non-UTF-8 file names as \\xNN instead of failing to encode them."""
    return os.fsencode(text).decode("utf-8", "backslashreplace")


def progress_text(phase: str, done: int, total: int | None, path: str, width: int) -> str:
    """One status line of at most width characters; a long path is cut at the front."""
    if total is None:
        head = f"listing files: {fmt(done)} found"
    else:
        pct = 100 * done // total if total else 100
        head = f"counting lines: {fmt(done)}/{fmt(total)} files ({pct}%)"
    path = printable(path)
    room = width - len(head) - 2
    if room < 8:
        return head[:width]
    if len(path) > room:
        path = "..." + path[-(room - 3):]
    return f"{head}  {path}"


def throttle(fn: Callable[..., None], interval: float = 0.1) -> Callable[..., None]:
    """Call fn at most once per interval; the first call waits one interval, so quick scans show nothing."""
    last = time.monotonic()

    def throttled(*args) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= interval:
            last = now
            fn(*args)

    return throttled


def sorted_children(node: Node, metric: str) -> list[Node]:
    return sorted(node.children.values(), key=lambda n: (-n.value(metric), n.name.lower()))


def render_text(root: Node, metric: str = "lines", depth: int = 1, top: int | None = None) -> str:
    total = root.value(metric)
    width = len(fmt(total))
    out = [f"{fmt(total)} {metric} in {fmt(root.files)} files  {root.name}"]

    def walk(node: Node, level: int) -> None:
        kids = sorted_children(node, metric)
        shown = kids[:top] if top else kids
        indent = "  " * level
        for kid in shown:
            value = kid.value(metric)
            pct = 100 * value / total if total else 0.0
            suffix = "/" if kid.is_dir else ""
            out.append(f"{fmt(value):>{width}}  {pct:5.1f}%  {indent}{kid.name}{suffix}")
            if kid.is_dir and level + 1 < depth:
                walk(kid, level + 1)
        if len(kids) > len(shown):
            out.append(f"{'':>{width}}          {indent}... {len(kids) - len(shown)} more")

    walk(root, 0)
    return printable("\n".join(out))


def to_ncdu(root: Node, metric: str = "lines") -> list:
    """ncdu export format; the metric goes into the size fields, so `ncdu -f` shows it as size."""

    def emit(node: Node, name: str) -> list:
        entries: list = [{"name": name}]
        for kid in sorted(node.children.values(), key=lambda n: n.name):
            if kid.is_dir:
                entries.append(emit(kid, kid.name))
            else:
                value = kid.value(metric)
                entries.append({"name": kid.name, "asize": value, "dsize": value})
        return entries

    meta = {"progname": "locdu", "progver": __version__, "timestamp": int(time.time())}
    return [1, 0, meta, emit(root, root.name)]
