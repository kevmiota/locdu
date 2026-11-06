"""Command line entry point."""
from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
from typing import Callable

from . import __version__
from .output import progress_text, render_text, throttle, to_ncdu
from .scan import DEFAULT_EXCLUDES, METRICS, Progress, ScanResult, scan

EPILOG = """\
examples:
  locdu                          browse the current directory
  locdu ~/work/client/repo -x docs -x '*.md'
  locdu -p -d 2 -n 10            print the top 10 per directory, two levels deep
  locdu --ncdu - | ncdu -f -     hand the counts to ncdu

In a git repo, the file list comes from git (tracked + untracked, minus .gitignore);
repos inside a plain folder are listed by git too.
Lockfiles, *.min.js, *.min.css and *.map are skipped unless --no-default-excludes is set.
"""


def scan_on_terminal(do_scan: Callable[[Progress | None], ScanResult]) -> ScanResult:
    """Run do_scan with an ncdu-style status line on stderr, if stderr is a terminal."""
    if not sys.stderr.isatty():
        return do_scan(None)
    spinner = itertools.cycle("|/-\\")

    def show(phase: str, done: int, total: int | None, path: str) -> None:
        width = shutil.get_terminal_size().columns - 1
        line = f"{next(spinner)} {progress_text(phase, done, total, path, width - 2)}"
        sys.stderr.write("\r" + line.ljust(width))
        sys.stderr.flush()

    try:
        return do_scan(throttle(show))
    finally:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="locdu",
        description="ncdu for lines of code: see where the lines in a codebase are.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dir", nargs="?", default=".", help="directory to scan (default: .)")
    ap.add_argument("-x", "--exclude", action="append", default=[], metavar="GLOB",
                    help="skip files/dirs matching GLOB (a name anywhere, or a path relative to DIR); repeatable")
    ap.add_argument("--no-default-excludes", action="store_true",
                    help="also count lockfiles, *.min.js, *.min.css and *.map")
    ap.add_argument("--no-git", action="store_true",
                    help="walk the directory instead of asking git for the file list")
    ap.add_argument("-m", "--metric", choices=METRICS, default="lines",
                    help="lines (all), code (non-blank) or files (default: lines)")
    ap.add_argument("-p", "--print", dest="print_tree", action="store_true",
                    help="print a tree instead of the interactive view (default when output is not a terminal)")
    ap.add_argument("-d", "--depth", type=int, default=1, metavar="N", help="with --print: levels to show (default: 1)")
    ap.add_argument("-n", "--top", type=int, default=None, metavar="N", help="with --print: at most N entries per directory")
    ap.add_argument("--ncdu", metavar="FILE", help="write ncdu's JSON format to FILE (- for stdout), for `ncdu -f`")
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    patterns = [p.rstrip("/") for p in args.exclude if p.rstrip("/")]
    if not args.no_default_excludes:
        patterns += DEFAULT_EXCLUDES
    interactive = not (args.print_tree or args.ncdu) and sys.stdin.isatty() and sys.stdout.isatty()

    def do_scan(progress: Progress | None) -> ScanResult:
        return scan(args.dir, patterns, use_git=not args.no_git, progress=progress)

    try:
        result = scan_on_terminal(do_scan)
        if args.ncdu:
            data = to_ncdu(result.root, args.metric)
            if args.ncdu == "-":
                json.dump(data, sys.stdout)
            else:
                with open(args.ncdu, "w", encoding="utf-8") as f:
                    json.dump(data, f)
        elif interactive:
            from .tui import run

            run(result, do_scan, args.metric)
        else:
            print(render_text(result.root, args.metric, max(1, args.depth), args.top))
    except NotADirectoryError as e:
        print(f"locdu: not a directory: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0
