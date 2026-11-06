import contextlib
import fcntl
import io
import json
import os
import pty
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

from locdu.cli import main  # noqa: E402
from locdu.output import fmt, printable, render_text, to_ncdu  # noqa: E402
import locdu.scan  # noqa: E402
from locdu.scan import DEFAULT_EXCLUDES, by_file_type, count_lines, exclude_matcher, scan  # noqa: E402


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def make_project(self):
        write(self.tmp, "src/a.ts", b"one\n\ntwo\n")  # 3 lines, 2 code
        write(self.tmp, "src/gen/g.ts", b"x\ny")  # no trailing newline: 2 lines
        write(self.tmp, "docs/readme.md", b"# hi\n")
        write(self.tmp, "logo.png", b"\x89PNG\0\0\0binary")
        write(self.tmp, "pnpm-lock.yaml", b"a\nb\nc\n")
        write(self.tmp, "node_modules/dep/index.js", b"x\n" * 50)
        os.symlink("src/a.ts", os.path.join(self.tmp, "link.ts"))


class CountLines(Tmp):
    def count(self, content):
        write(self.tmp, "f", content)
        return count_lines(os.path.join(self.tmp, "f"))

    def test_counts_lines_and_non_blank_lines(self):
        self.assertEqual(self.count(b"a\n\n  \nb\n"), (4, 2))

    def test_last_line_without_newline_counts(self):
        self.assertEqual(self.count(b"a\nb"), (2, 2))

    def test_empty_file(self):
        self.assertEqual(self.count(b""), (0, 0))

    def test_crlf_blank_lines_are_blank(self):
        self.assertEqual(self.count(b"a\r\n\r\nb\r\n"), (3, 2))

    def test_binary_is_none(self):
        self.assertIsNone(self.count(b"abc\0def"))

    def test_nul_after_the_first_8_kb_is_still_text(self):
        self.assertEqual(self.count(b"a\n" * 5000 + b"\0"), (5001, 5001))

    def test_file_over_the_size_limit_is_skipped(self):
        self.addCleanup(setattr, locdu.scan, "MAX_BYTES", locdu.scan.MAX_BYTES)
        locdu.scan.MAX_BYTES = 10
        self.assertIsNone(self.count(b"a\n" * 6))
        self.assertEqual(self.count(b"a\n" * 5), (5, 5))

    def test_missing_file_is_none(self):
        self.assertIsNone(count_lines(os.path.join(self.tmp, "nope")))


class Excludes(unittest.TestCase):
    def test_name_matches_anywhere(self):
        self.assertTrue(exclude_matcher(["docs"])("a/docs/x.md"))

    def test_glob_on_file_name(self):
        self.assertTrue(exclude_matcher(["*.md"])("a/b/x.md"))
        self.assertFalse(exclude_matcher(["*.md"])("a/b/x.ts"))

    def test_path_prefix_relative_to_root(self):
        self.assertTrue(exclude_matcher(["src/gen"])("src/gen/g.ts"))
        self.assertFalse(exclude_matcher(["src/gen"])("lib/src/gen/g.ts"))

    def test_no_patterns_excludes_nothing(self):
        self.assertFalse(exclude_matcher([])("a/b.ts"))

    def test_no_partial_name_match(self):
        self.assertFalse(exclude_matcher(["docs"])("mydocs/x.md"))


class Scan(Tmp):
    def test_walk_tree_totals_and_skips(self):
        self.make_project()
        r = scan(self.tmp, DEFAULT_EXCLUDES, use_git=False)
        self.assertEqual((r.root.lines, r.root.code, r.root.files), (6, 5, 3))
        self.assertEqual(r.root.find("src").lines, 5)
        self.assertEqual(r.root.find("src/gen/g.ts").lines, 2)
        self.assertIsNone(r.root.find("node_modules"))
        self.assertIsNone(r.root.find("pnpm-lock.yaml"))
        self.assertIsNone(r.root.find("link.ts"))
        self.assertIsNone(r.root.find("logo.png"))
        self.assertEqual(r.skipped, 1)

    def test_without_default_excludes_lockfile_counts(self):
        self.make_project()
        r = scan(self.tmp, [], use_git=False)
        self.assertEqual(r.root.find("pnpm-lock.yaml").lines, 3)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_lists_untracked_but_not_ignored(self):
        self.make_project()
        write(self.tmp, ".gitignore", b"docs/\nnode_modules/\n")
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        r = scan(self.tmp, DEFAULT_EXCLUDES)
        self.assertIsNone(r.root.find("docs"))
        self.assertIsNone(r.root.find("node_modules"))
        self.assertEqual(r.root.find("src/a.ts").lines, 3)
        self.assertEqual(r.root.find(".gitignore").lines, 2)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_counts_nested_repos(self):
        write(self.tmp, "top.ts", b"y\n")
        write(self.tmp, "libs/inner/x.ts", b"a\nb\nc\n")
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        subprocess.run(["git", "init", "-q", os.path.join(self.tmp, "libs/inner")], check=True)
        r = scan(self.tmp)
        self.assertEqual(r.root.find("libs/inner/x.ts").lines, 3)
        self.assertEqual(r.root.lines, 4)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_uninitialized_submodule_does_not_recurse_forever(self):
        write(self.tmp, "top.ts", b"y\n")
        os.makedirs(os.path.join(self.tmp, "vendor/lib"))
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        subprocess.run(["git", "-C", self.tmp, "update-index", "--add", "--cacheinfo",
                        "160000,1111111111111111111111111111111111111111,vendor/lib"], check=True)
        self.assertEqual(scan(self.tmp).root.lines, 1)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_folder_ignored_by_git_is_walked(self):
        write(self.tmp, ".gitignore", b"dist/\n")
        write(self.tmp, "dist/app.js", b"a\nb\n")
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        r = scan(os.path.join(self.tmp, "dist"))
        self.assertEqual(r.root.find("app.js").lines, 2)

    def test_not_a_directory(self):
        with self.assertRaises(NotADirectoryError):
            scan(os.path.join(self.tmp, "missing"))


class TreeEdits(Tmp):
    def setUp(self):
        super().setUp()
        self.make_project()
        self.root = scan(self.tmp, DEFAULT_EXCLUDES, use_git=False).root

    def test_detach_updates_totals(self):
        src = self.root.find("src")
        src.find("gen").detach()
        self.assertEqual((src.lines, self.root.lines, self.root.files), (3, 4, 2))
        self.assertIsNone(src.find("gen"))

    def test_reattach_restores(self):
        gen = self.root.find("src/gen")
        gen.detach()
        gen.reattach()
        self.assertEqual((self.root.lines, self.root.files), (6, 3))

    def test_set_counts_propagates(self):
        self.root.find("src/a.ts").set_counts(10, 8)
        self.assertEqual((self.root.find("src").lines, self.root.lines, self.root.code), (12, 13, 11))

    def test_file_types(self):
        rows = {r[0]: r[1:] for r in by_file_type(self.root)}
        self.assertEqual(rows, {".ts": (5, 4, 2), ".md": (1, 1, 1)})


class Output(Tmp):
    def setUp(self):
        super().setUp()
        self.make_project()
        self.root = scan(self.tmp, DEFAULT_EXCLUDES, use_git=False).root

    def test_fmt(self):
        self.assertEqual(fmt(1234567), "1'234'567")

    def test_render_text_depth_and_top(self):
        text = render_text(self.root, depth=2, top=1)
        lines = text.splitlines()
        self.assertIn("6 lines in 3 files", lines[0])
        self.assertRegex(lines[1], r"5\s+83\.3%\s+src/$")
        self.assertRegex(lines[2], r"3\s+50\.0%\s+  a\.ts$")
        self.assertIn("... 1 more", lines[3])
        self.assertIn("... 1 more", lines[4])

    def test_non_utf8_file_name_is_escaped(self):
        try:
            open(os.path.join(os.fsencode(self.tmp), b"caf\xe9.txt"), "wb").close()
        except OSError:
            self.skipTest("file system rejects non-UTF-8 names")
        text = render_text(scan(self.tmp, use_git=False).root)
        self.assertIn("caf\\xe9.txt", text)
        text.encode("utf-8")  # strict: would raise on a raw surrogate

    def test_printable_keeps_valid_unicode(self):
        self.assertEqual(printable("Grüezi ✓"), "Grüezi ✓")

    def test_ncdu_format(self):
        data = to_ncdu(self.root)
        self.assertEqual(data[:2], [1, 0])
        tree = data[3]
        self.assertEqual(tree[0]["name"], self.tmp)
        names = {e[0]["name"] if isinstance(e, list) else e["name"] for e in tree[1:]}
        self.assertEqual(names, {"src", "docs"})


class Cli(Tmp):
    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_prints_when_not_a_terminal(self):
        self.make_project()
        code, out = self.run_main(self.tmp, "--no-git", "-x", "docs")
        self.assertEqual(code, 0)
        self.assertIn("5 lines in 2 files", out)

    def test_code_metric(self):
        self.make_project()
        code, out = self.run_main(self.tmp, "--no-git", "-m", "code")
        self.assertIn("5 code in 3 files", out)

    def test_ncdu_to_file(self):
        self.make_project()
        target = os.path.join(self.tmp, "out.json")
        code, _ = self.run_main(self.tmp, "--no-git", "--ncdu", target)
        self.assertEqual(code, 0)
        with open(target) as f:
            self.assertEqual(json.load(f)[2]["progname"], "locdu")

    def test_missing_dir(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = self.run_main(os.path.join(self.tmp, "missing"))
        self.assertEqual(code, 2)


class Tui(Tmp):
    """Drives the real curses UI through a pseudo terminal."""

    def test_keys_do_not_crash(self):
        self.make_project()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", EDITOR="true", PYTHONPATH=SRC)
        env.pop("VISUAL", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "locdu", self.tmp, "--no-git"],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
        )
        os.close(slave)
        output = bytearray()

        def drain():
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                output.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        keys = ["j", "k", "\r", "e", "j", "d", "u", "h", "m", "m", "m", "s", "n", "n",
                "t", "x", "?", "j", "x", "r", "\x1b[6~", "G", "g", "l", "\x7f", "q"]
        for key in keys:
            time.sleep(0.15)
            os.write(master, key.encode())
        try:
            code = proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        reader.join(timeout=2)
        os.close(master)
        text = output.decode(errors="replace")
        self.assertNotIn("Traceback", text)
        self.assertEqual(code, 0)
        self.assertIn("locdu", text)


if __name__ == "__main__":
    unittest.main()
