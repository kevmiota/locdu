# locdu

ncdu for lines of code. Scans a directory, counts the lines of every text file and lets
you drill into the tree interactively, largest first. Pure Python standard library, no
dependencies; Linux and macOS (needs curses).

```
locdu [DIR] [-x GLOB]... [-m lines|code|files] [-p [-d N] [-n N]] [--ncdu FILE]
```

## Install

With [pipx](https://pipx.pypa.io) (Ubuntu: `sudo apt install pipx && pipx ensurepath`):

```sh
pipx install locdu                                   # from PyPI
pipx install git+https://github.com/kevmiota/locdu   # latest main
```

`uv tool install locdu` works too.

As a Debian/Ubuntu package, from the
[releases page](https://github.com/kevmiota/locdu/releases):

```sh
sudo apt install ./locdu_0.1.0_all.deb
```

Without pip, from a clone (needs only `python3`):

```sh
./install.sh               # copies to ~/.local/share/locdu, launcher in ~/.local/bin
./install.sh --uninstall
```

## What gets counted

- In a git repo: the files git knows about, tracked and untracked, minus `.gitignore`,
  including nested repos and submodules. A plain folder (or one git ignores) is walked,
  skipping `node_modules`, `dist`, `build`, `.venv`, `vendor` and similar; every git repo
  found on the way is listed by git again, so a folder full of repos honours each
  repo's `.gitignore`. `--no-git` walks everything.
- Skipped by default: lockfiles, `*.min.js`, `*.min.css`, `*.map`
  (`--no-default-excludes` counts them). Add your own with `-x`, repeatable:
  a name or glob matches anywhere (`-x docs -x '*.md'`), a path matches relative
  to DIR (`-x src/generated`).
- Binary files (a NUL byte in the first 8 KB), text files over 50 MB and symlinks
  are skipped.
- `lines` = all lines, `code` = non-blank lines. Comments count as code; use
  `scc` or `tokei` if you need comments separated.

While scanning, a status line shows the phase, the file count and the current path.

## Keys

| Key | |
|---|---|
| arrows, `hjkl`, enter, backspace | navigate |
| `m` | metric: lines, code lines, files |
| `s` / `n` | sort by count / name, again to reverse |
| `t` | file types in the current directory |
| `e` | open the file in `$EDITOR`, recounted afterwards |
| `d` / `u` | hide from the view / undo; nothing on disk is touched |
| `r` | rescan |
| `?` / `q` | help / quit |

## Without the UI

```sh
locdu -p -d 2 -n 10           # tree, two levels, top 10 per directory
locdu | less                  # prints automatically when stdout is not a terminal
locdu --ncdu - | ncdu -f -    # hand the counts to ncdu (shown as sizes)
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```
