"""Find files, count their lines and build the directory tree."""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

METRICS = ("lines", "code", "files")

# Only used outside git repos (or with --no-git); inside a repo, .gitignore decides.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".next", ".nuxt", ".turbo", ".cache",
    "dist", "build", "coverage", ".venv", "venv", "__pycache__", "vendor",
}

DEFAULT_EXCLUDES = [
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb",
    "composer.lock", "Cargo.lock", "poetry.lock", "uv.lock", "Gemfile.lock", "go.sum",
    "*.min.js", "*.min.css", "*.map",
]

SNIFF_BYTES = 8192
MAX_BYTES = 50 * 2**20  # bigger text files are data dumps, not code


class Node:
    """A file or directory. Directory counts are the sums of their children."""

    __slots__ = ("name", "parent", "children", "lines", "code", "files")

    def __init__(self, name: str, parent: Node | None = None, is_dir: bool = False):
        self.name = name
        self.parent = parent
        self.children: dict[str, Node] | None = {} if is_dir else None
        self.lines = 0
        self.code = 0
        self.files = 0

    @property
    def is_dir(self) -> bool:
        return self.children is not None

    def value(self, metric: str) -> int:
        return getattr(self, metric)

    def relpath(self) -> str:
        parts = []
        node = self
        while node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    def find(self, relpath: str) -> Node | None:
        node = self
        for part in filter(None, relpath.split("/")):
            if not node.is_dir or part not in node.children:
                return None
            node = node.children[part]
        return node

    def _add_to_ancestors(self, lines: int, code: int, files: int) -> None:
        node = self.parent
        while node is not None:
            node.lines += lines
            node.code += code
            node.files += files
            node = node.parent

    def detach(self) -> None:
        """Take this node out of the tree (view only) and update the totals above it."""
        del self.parent.children[self.name]
        self._add_to_ancestors(-self.lines, -self.code, -self.files)

    def reattach(self) -> None:
        self.parent.children[self.name] = self
        self._add_to_ancestors(self.lines, self.code, self.files)

    def set_counts(self, lines: int, code: int) -> None:
        """Replace a file's counts, e.g. after it was edited."""
        delta_lines, delta_code = lines - self.lines, code - self.code
        self.lines, self.code = lines, code
        self._add_to_ancestors(delta_lines, delta_code, 0)


@dataclass
class ScanResult:
    root: Node
    skipped: int  # binary or larger than MAX_BYTES


def count_lines(path: str) -> tuple[int, int] | None:
    """(lines, non-blank lines) of a text file; None if it is binary, too large or unreadable."""
    try:
        with open(path, "rb") as f:
            data = f.read(SNIFF_BYTES)
            if b"\0" in data or os.fstat(f.fileno()).st_size > MAX_BYTES:
                return None
            data += f.read()
    except OSError:
        return None
    if not data:
        return 0, 0
    lines = data.count(b"\n") + (not data.endswith(b"\n"))
    code = sum(1 for line in data.split(b"\n") if line.strip())
    return lines, code


def exclude_matcher(patterns: Iterable[str]) -> Callable[[str], bool]:
    """Test for relative paths: a pattern matches any single path component, or a path prefix."""
    patterns = list(patterns)
    if not patterns:
        return lambda relpath: False
    regex = re.compile("|".join(fnmatch.translate(p) for p in patterns))

    def is_excluded(relpath: str) -> bool:
        parts = relpath.split("/")
        candidates = parts + ["/".join(parts[:i]) for i in range(2, len(parts) + 1)]
        return any(regex.match(c) for c in candidates)

    return is_excluded


# phase ("listing" or "counting"), files done, total (None while listing), current path
Progress = Callable[[str, int, int | None, str], None]


def list_files(root: str, use_git: bool = True, progress: Progress | None = None) -> list[str]:
    """Candidate file paths relative to root. Git repos inside root are listed by git."""
    found: list[str] = []
    _list_into(found, root, "", use_git, progress)
    return found


def _git_files(root: str) -> list[str]:
    """What git lists in root (directories for submodules and nested repos); [] if not a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    # An uninitialized submodule lists itself as "./".
    return sorted({os.fsdecode(p).rstrip("/") for p in out.split(b"\0")} - {"", "."})


def _list_into(found: list[str], root: str, prefix: str, use_git: bool, progress: Progress | None) -> None:
    entries = _git_files(root) if use_git else []
    if entries:
        for p in entries:
            full = os.path.join(root, p)
            if os.path.isdir(full) and not os.path.islink(full):
                _list_into(found, full, f"{prefix}{p}/", use_git, progress)
            else:
                found.append(prefix + p)
        if progress:
            progress("listing", len(found), None, prefix)
        return

    # Not a repo, or git lists nothing here (e.g. a folder in .gitignore): walk it.
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        here = prefix + ("" if rel == "." else rel.replace(os.sep, "/") + "/")
        if use_git and dirpath != root and os.path.exists(os.path.join(dirpath, ".git")):
            dirnames[:] = []
            _list_into(found, dirpath, here, use_git, progress)
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        found.extend(here + f for f in sorted(filenames))
        if progress:
            progress("listing", len(found), None, here)


def _count_regular(path: str) -> tuple[int, int] | None | bool:
    """False for anything that is not a regular file (symlinks, deleted files, submodules)."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
    except OSError:
        return False
    return count_lines(path)


def _insert(root: Node, relpath: str, lines: int, code: int) -> None:
    *dirs, name = relpath.split("/")
    node = root
    for d in dirs:
        child = node.children.get(d)
        if child is None:
            child = node.children[d] = Node(d, node, is_dir=True)
        node = child
    leaf = node.children[name] = Node(name, node)
    leaf.lines, leaf.code, leaf.files = lines, code, 1
    leaf._add_to_ancestors(lines, code, 1)


def scan(
    root_dir: str,
    excludes: Iterable[str] = (),
    use_git: bool = True,
    progress: Progress | None = None,
) -> ScanResult:
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        raise NotADirectoryError(root_dir)
    is_excluded = exclude_matcher(excludes)
    kept = [p for p in list_files(root_dir, use_git, progress) if not is_excluded(p)]

    root = Node(root_dir, is_dir=True)
    skipped = 0
    workers = min(32, (os.cpu_count() or 1) * 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = pool.map(lambda p: _count_regular(os.path.join(root_dir, p)), kept)
        for i, (relpath, result) in enumerate(zip(kept, counts), 1):
            if result is None:
                skipped += 1
            elif result is not False:
                _insert(root, relpath, *result)
            if progress:
                progress("counting", i, len(kept), relpath)
    return ScanResult(root, skipped)


def file_type(name: str) -> str:
    """Extension in lower case, or the whole name for files without one (Dockerfile, .gitignore)."""
    ext = os.path.splitext(name)[1]
    return ext.lower() if ext else name


def by_file_type(node: Node) -> list[tuple[str, int, int, int]]:
    """(type, lines, code, files) for every file type below node."""
    stats: dict[str, list[int]] = {}
    stack = [node]
    while stack:
        n = stack.pop()
        if n.is_dir:
            stack.extend(n.children.values())
        else:
            s = stats.setdefault(file_type(n.name), [0, 0, 0])
            s[0] += n.lines
            s[1] += n.code
            s[2] += 1
    return [(k, *v) for k, v in stats.items()]
