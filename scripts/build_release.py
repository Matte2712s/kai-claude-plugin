#!/usr/bin/env python3
"""Build the release zip of the kai plugin (stdlib only).

Usage: python scripts/build_release.py [--root PATH] --out DIR [--tag vX.Y.Z] [--notes FILE]
Exit codes: 0 built and verified, 1 problems found, 2 bad arguments.

Every problem is printed as "path: message". Steps, in order:

  1. scripts/check_tree.py runs over ROOT; any problem stops the build
  2. the version comes from plugins/kai/.claude-plugin/plugin.json; with --tag
     the tag must equal "v" + version (how a release job ties tag to manifest)
  3. DIR/kai-<version>.zip is written from the CONTENTS of plugins/kai: the
     manifest lands at .claude-plugin/plugin.json, with no top-level folder.
     The file set is the one check_tree scans: the top-level .git and
     git-ignored untracked files are skipped
  4. the zip is re-opened and checked by verify_zip; a failure deletes it
  5. with --notes FILE, the body of the "## <version>" section of
     plugins/kai/CHANGELOG.md is written there (never empty)

DIR and FILE must not sit inside plugins/kai, or the next build would pack them.

The output is deterministic: sorted entry names, "/" separators, fixed
timestamp, fixed mode 0644, Unix creator, deflate, no directory entries, no
extra fields. The same tree gives the same bytes (for one zlib build). The zip
is written under a temporary name and renamed, so a failed write leaves no
partial file.

verify_zip works on any zip and reports:

  - not exactly one plugin.json, or one that is not .claude-plugin/plugin.json
  - an entry whose first path segment is bin (any case)
  - an absolute path, a ".." segment, a backslash, a duplicate name
  - a symlink or, for a file, an executable bit in the Unix mode
  - total uncompressed size of 10 MB or more
  - a failed ZipFile.testzip() (CRC, truncated or unreadable data)
  - .mcp.json or README.md missing at the root

On a Windows host zipfile turns a backslash in a name into "/" while reading,
so the backslash rule only fires on POSIX hosts. The zips this script builds
cannot hold one: they come from posix paths of files that exist on disk.
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import stat
import sys
import zipfile
import zlib
from pathlib import Path

import check_tree

REPO_ROOT = check_tree.REPO_ROOT
PLUGIN = check_tree.PLUGIN
MANIFEST = ".claude-plugin/plugin.json"
REQUIRED_FILES = (".mcp.json", "README.md")
ZIP_DATE = (2020, 1, 1, 0, 0, 0)
FILE_ATTR = 0o644 << 16
CREATOR_UNIX = 3
MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024

_emit = check_tree._emit


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _inside(path: Path, base: Path) -> bool:
    return Path(path).resolve().is_relative_to(Path(base).resolve())


def _read_version(root: Path, problems: list[str]) -> str | None:
    path = root / "plugins" / PLUGIN / ".claude-plugin" / "plugin.json"
    data = check_tree._load_json(root, path, problems)
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and check_tree.SEMVER.fullmatch(version):
        return version
    if data is not None:
        problems.append(f"{check_tree._rel(root, path)}: version must be semver X.Y.Z")
    return None


def release_notes(root: Path, version: str, problems: list[str]) -> str | None:
    path = root / "plugins" / PLUGIN / "CHANGELOG.md"
    rel = check_tree._rel(root, path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        problems.append(f"{rel}: file is missing")
        return None
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{rel}: cannot read file ({type(exc).__name__})")
        return None
    # Same heading shape as check_tree: "## 0.1.0", "## [0.1.0] - date"
    heading = re.compile(r"##[ \t]+\[?" + re.escape(version) + r"\]?(?:[ \t].*)?")
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if heading.fullmatch(line.rstrip())), None)
    if start is None:
        problems.append(f"{rel}: no '## {version}' section")
        return None
    body: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"##[ \t]", line):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        problems.append(f"{rel}: the '## {version}' section is empty")
        return None
    return notes


def _entries(root: Path, base: Path, problems: list[str]) -> list[tuple[str, Path]]:
    links, _dirs, files, errors = check_tree._walk(root, check_tree._git_ignored(root))
    problems.extend(errors)
    for link in links:
        if link.is_relative_to(base):
            problems.append(f"{check_tree._rel(root, link)}: symlink or junction is not allowed")
    entries = [(p.relative_to(base).as_posix(), p) for p in files if p.is_relative_to(base)]
    return sorted(entries)


def _write_zip(entries: list[tuple[str, Path]], target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in entries:
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = CREATOR_UNIX
            info.external_attr = FILE_ATTR
            archive.writestr(info, path.read_bytes())


def _check_entries(infos: list[zipfile.ZipInfo], label: str, problems: list[str]) -> int:
    total = 0
    manifests: list[str] = []
    for name, count in collections.Counter(i.filename for i in infos).items():
        if count > 1:
            problems.append(f"{name}: duplicate entry name")
    for info in infos:
        name = info.filename
        total += info.file_size
        norm = name.replace("\\", "/")
        parts = norm.split("/")
        mode = info.external_attr >> 16
        if "\\" in name:
            problems.append(f"{name}: backslash in the entry name")
        if norm.startswith("/") or re.match(r"[A-Za-z]:", norm):
            problems.append(f"{name}: absolute path")
        if ".." in parts:
            problems.append(f"{name}: '..' segment in the path")
        if parts[0].lower() == "bin":
            problems.append(f"{name}: a bin directory at the top is refused by claude.ai and Cowork")
        if stat.S_ISLNK(mode):
            problems.append(f"{name}: symlink entry")
        elif mode & 0o111 and not info.is_dir():
            problems.append(f"{name}: executable bit is set")
        if parts[-1].lower() == "plugin.json":
            manifests.append(name)
    if len(manifests) != 1:
        found = ", ".join(manifests) or "none"
        problems.append(f"{label}: expected exactly one plugin.json, found {len(manifests)} ({found})")
    elif manifests[0] != MANIFEST:
        problems.append(f"{manifests[0]}: the manifest must be {MANIFEST} at the zip root")
    names = {i.filename for i in infos}
    for required in REQUIRED_FILES:
        if required not in names:
            problems.append(f"{label}: {required} is missing at the zip root")
    return total


def _check_crc(archive: zipfile.ZipFile, label: str, problems: list[str]) -> None:
    try:
        bad = archive.testzip()
    except (OSError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error) as exc:
        problems.append(f"{label}: entry data cannot be read ({type(exc).__name__})")
        return
    if bad is not None:
        problems.append(f"{bad}: failed the CRC check")


def verify_zip(path: Path) -> list[str]:
    path = Path(path)
    label = path.name
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{label}: cannot open the zip ({type(exc).__name__})"]
    problems: list[str] = []
    with archive:
        total = _check_entries(archive.infolist(), label, problems)
        if total >= MAX_UNCOMPRESSED_BYTES:
            problems.append(
                f"{label}: uncompressed size {total} bytes is not under {MAX_UNCOMPRESSED_BYTES}"
            )
        else:
            # Past the cap the data is not decompressed at all
            _check_crc(archive, label, problems)
    return problems


def build(
    root: Path, out: Path, tag: str | None = None, notes: Path | None = None
) -> tuple[list[str], tuple[Path, int, int] | None]:
    """Return (problems, (zip path, file count, zip bytes)); the second is None on failure."""
    root, out = Path(root), Path(out)
    problems = check_tree.check(root)
    if problems:
        return problems, None
    base = root / "plugins" / PLUGIN
    version = _read_version(root, problems)
    if version is None:
        return problems, None
    if tag is not None and tag != f"v{version}":
        problems.append(f"--tag: {tag!r} does not match the plugin.json version (expected 'v{version}')")
    notes_text = release_notes(root, version, problems) if notes is not None else None
    for flag, path in (("--out", out), ("--notes", notes)):
        if path is not None and _inside(path, base):
            problems.append(f"{flag}: {path} is inside plugins/{PLUGIN} and would end up in the zip")
    entries = _entries(root, base, problems)
    if problems:
        return problems, None

    target = out / f"kai-{version}.zip"
    partial = target.with_name(target.name + ".tmp")
    try:
        out.mkdir(parents=True, exist_ok=True)
        _write_zip(entries, partial)
        os.replace(partial, target)
    except OSError as exc:
        return [f"{target}: cannot write the zip ({type(exc).__name__})"], None
    finally:
        _unlink(partial)

    problems = verify_zip(target)
    if problems:
        _unlink(target)
        return problems, None
    if notes is not None and notes_text is not None:
        try:
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text(notes_text + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            _unlink(target)
            return [f"{notes}: cannot write the notes ({type(exc).__name__})"], None
    return [], (target, len(entries), target.stat().st_size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the release zip of the kai plugin.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this repo)")
    parser.add_argument("--out", type=Path, required=True, help="directory for kai-<version>.zip (created if missing)")
    parser.add_argument("--tag", help="release tag; must equal v<version> of plugin.json")
    parser.add_argument("--notes", type=Path, help="write the CHANGELOG section of this version to FILE")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.root.is_dir():
        _emit(f"build_release: not a directory: {args.root}", sys.stderr)
        return 2
    problems, result = build(args.root, args.out, args.tag, args.notes)
    for problem in problems:
        _emit(problem, sys.stderr)
    if problems or result is None:
        _emit(f"build_release: {len(problems)} problem(s)", sys.stderr)
        return 1
    path, count, size = result
    _emit(f"build_release: wrote {path} ({count} files, {size} bytes)", sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
