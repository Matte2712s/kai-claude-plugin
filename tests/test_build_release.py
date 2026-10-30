"""Tests for scripts/build_release.py (stdlib unittest)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_release  # noqa: E402

MIT_TEXT = "MIT License\n\nCopyright (c) 2026 Test\n\nPermission is hereby granted.\n"
REPO_URL = "https://github.com/example/kai-claude-plugin"
MANIFEST = ".claude-plugin/plugin.json"
ATTR = 0o644 << 16
BASE_NAMES = [MANIFEST, ".mcp.json", "CHANGELOG.md", "LICENSE", "README.md"]

# Smallest zip that verify_zip accepts
GOOD = [(MANIFEST, b"{}"), (".mcp.json", b"{}"), ("README.md", b"x")]


def _force_rmtree(path: Path) -> None:
    def handler(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=handler)


def write(root: Path, rel: str, content: str | bytes = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    return path


def write_json(root: Path, rel: str, data) -> Path:
    return write(root, rel, json.dumps(data, indent=2) + "\n")


def build_valid_tree(root: Path) -> None:
    write_json(root, ".claude-plugin/marketplace.json", {
        "name": "kai",
        "owner": {"name": "Test"},
        "plugins": [{
            "name": "kai", "source": "./plugins/kai", "version": "0.1.0", "license": "MIT",
        }],
    })
    write_json(root, "plugins/kai/.claude-plugin/plugin.json", {
        "name": "kai", "version": "0.1.0", "license": "MIT",
        "homepage": REPO_URL, "repository": REPO_URL,
    })
    write_json(root, "plugins/kai/.mcp.json", {
        "mcpServers": {"kai": {"type": "http", "url": "https://kai.example.org/mcp"}},
    })
    write(root, "plugins/kai/README.md", "# KAI\n")
    write(root, "plugins/kai/CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\n- First version.\n")
    write(root, "plugins/kai/LICENSE", MIT_TEXT)
    write(root, "LICENSE", MIT_TEXT)
    write(root, "README.md", "# repo\n")


def make_zip(path: Path, entries, compress: int = zipfile.ZIP_DEFLATED) -> Path:
    # entries: (name, data) or (name, data, external_attr)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w") as archive:
            for name, data, *rest in entries:
                info = zipfile.ZipInfo("placeholder", (2020, 1, 1, 0, 0, 0))
                # Set after init: the constructor rewrites os.sep
                info.filename = name
                info.compress_type = compress
                info.external_attr = rest[0] if rest else ATTR
                archive.writestr(info, data)
    return path


class BuildCase(unittest.TestCase):
    def tmp(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="kai-build-"))
        self.addCleanup(_force_rmtree, directory)
        return directory

    def tree(self) -> Path:
        root = self.tmp()
        build_valid_tree(root)
        return root

    def out(self) -> Path:
        # Never inside the tree: check_tree would scan it
        return self.tmp() / "dist"

    def assertProblem(self, problems: list[str], *needles: str) -> None:
        self.assertTrue(
            any(all(n in p for n in needles) for p in problems),
            f"no problem with {needles!r} in {problems!r}",
        )

    def built(self, root: Path | None = None, out: Path | None = None, **kwargs) -> Path:
        problems, result = build_release.build(root or self.tree(), out or self.out(), **kwargs)
        self.assertEqual(problems, [])
        self.assertIsNotNone(result)
        return result[0]

    def verify(self, entries, **kwargs) -> list[str]:
        return build_release.verify_zip(make_zip(self.tmp() / "t.zip", entries, **kwargs))


class BuildTests(BuildCase):
    def test_happy_path(self):
        out = self.out()
        problems, result = build_release.build(self.tree(), out)
        self.assertEqual(problems, [])
        path, count, size = result
        self.assertEqual(path, out / "kai-0.1.0.zip")
        self.assertEqual(count, len(BASE_NAMES))
        self.assertEqual(size, path.stat().st_size)
        self.assertEqual(build_release.verify_zip(path), [])
        self.assertEqual([p.name for p in out.iterdir()], ["kai-0.1.0.zip"])

    def test_manifest_is_at_the_zip_root_and_dotfiles_are_in(self):
        root = self.tree()
        write(root, "scripts/helper.py", "print(1)\n")
        write(root, "contract/mcp_tools.json", "{}\n")
        with zipfile.ZipFile(self.built(root)) as archive:
            names = archive.namelist()
            self.assertEqual(names, BASE_NAMES)
            self.assertEqual(archive.read("README.md"), b"# KAI\n")
            self.assertEqual(json.loads(archive.read(MANIFEST))["name"], "kai")
        self.assertFalse([n for n in names if n.startswith("plugins/")])

    def test_nested_files_are_sorted_by_full_name(self):
        root = self.tree()
        write(root, "plugins/kai/skills/x/SKILL.md", "---\nname: x\ndescription: y\n---\n")
        write(root, "plugins/kai/skills/x/references/a.md", "text\n")
        write(root, "plugins/kai/skills/a/SKILL.md", "---\nname: a\ndescription: y\n---\n")
        with zipfile.ZipFile(self.built(root)) as archive:
            names = archive.namelist()
        self.assertEqual(names, sorted(names))
        for name in ("skills/a/SKILL.md", "skills/x/SKILL.md", "skills/x/references/a.md"):
            self.assertIn(name, names)

    def test_entry_metadata_is_fixed(self):
        with zipfile.ZipFile(self.built()) as archive:
            for info in archive.infolist():
                with self.subTest(name=info.filename):
                    self.assertEqual(info.date_time, (2020, 1, 1, 0, 0, 0))
                    self.assertEqual(info.external_attr, 0o644 << 16)
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                    self.assertEqual(info.create_system, 3)
                    self.assertEqual(info.extra, b"")
                    self.assertFalse(info.filename.endswith("/"))
                    self.assertFalse(info.is_dir())

    def test_two_builds_are_byte_identical(self):
        root = self.tree()
        write(root, "plugins/kai/skills/x/SKILL.md", "---\nname: x\ndescription: y\n---\n")
        first = self.built(root).read_bytes()
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (1_000_000_000, 1_000_000_000))
        second = self.built(root).read_bytes()
        self.assertEqual(first, second)

    def test_output_directory_is_created_and_an_existing_zip_is_replaced(self):
        out = self.tmp() / "a" / "b"
        self.assertFalse(out.exists())
        root = self.tree()
        path = self.built(root, out)
        self.assertEqual(path, out / "kai-0.1.0.zip")
        path.write_bytes(b"stale")
        self.assertEqual(self.built(root, out), path)
        self.assertEqual(build_release.verify_zip(path), [])
        self.assertEqual([p.name for p in out.iterdir()], ["kai-0.1.0.zip"])

    def test_the_real_repo_builds(self):
        path = self.built(REPO_ROOT)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            version = json.loads(archive.read(MANIFEST))["version"]
        self.assertEqual(path.name, f"kai-{version}.zip")
        self.assertEqual([n for n in names if n.endswith("plugin.json")], [MANIFEST])
        self.assertFalse([n for n in names if n.lower().startswith("bin")])

    def test_git_ignored_untracked_files_are_left_out_but_tracked_ones_are_not(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        root = self.tree()
        write(root, ".gitignore", "plugins/kai/scratch.md\n")
        write(root, "plugins/kai/scratch.md", "local notes\n")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env, check=False)

        if git("init", "-q").returncode != 0:
            self.skipTest("git init failed")
        with zipfile.ZipFile(self.built(root)) as archive:
            self.assertNotIn("scratch.md", archive.namelist())
            self.assertEqual(archive.namelist(), BASE_NAMES)
        self.assertEqual(git("add", "-f", "plugins/kai/scratch.md").returncode, 0)
        with zipfile.ZipFile(self.built(root)) as archive:
            self.assertIn("scratch.md", archive.namelist())

    def test_the_output_directory_cannot_be_inside_the_plugin(self):
        root = self.tree()
        out = root / "plugins" / "kai" / "dist"
        problems, result = build_release.build(root, out)
        self.assertIsNone(result)
        self.assertProblem(problems, "--out", "inside plugins/kai")
        self.assertFalse(out.exists())

    def test_a_failed_write_leaves_no_file(self):
        out = self.out()

        def boom(entries, target):
            Path(target).write_bytes(b"partial")
            raise OSError("disk full")

        with mock.patch.object(build_release, "_write_zip", boom):
            problems, result = build_release.build(self.tree(), out)
        self.assertIsNone(result)
        self.assertProblem(problems, "kai-0.1.0.zip", "cannot write the zip")
        self.assertEqual(list(out.iterdir()), [])

    def test_a_failed_verification_deletes_the_zip(self):
        out = self.out()
        with mock.patch.object(build_release, "verify_zip", lambda path: ["x: bad"]):
            problems, result = build_release.build(self.tree(), out)
        self.assertEqual(problems, ["x: bad"])
        self.assertIsNone(result)
        self.assertEqual(list(out.iterdir()), [])

    def test_the_built_zip_goes_through_verify_zip(self):
        seen = []
        real = build_release.verify_zip

        def spy(path):
            seen.append(Path(path).name)
            return real(path)

        with mock.patch.object(build_release, "verify_zip", spy):
            self.built()
        self.assertEqual(seen, ["kai-0.1.0.zip"])


class TagTests(BuildCase):
    def test_matching_tag(self):
        self.assertEqual(self.built(tag="v0.1.0").name, "kai-0.1.0.zip")

    def test_mismatching_tags_are_refused_and_build_nothing(self):
        for tag in ("v0.1.1", "0.1.0", "v0.1.0x", "v0.1.0-rc1", "V0.1.0", " v0.1.0", "v0.1", ""):
            with self.subTest(tag=tag):
                out = self.out()
                problems, result = build_release.build(self.tree(), out, tag=tag)
                self.assertIsNone(result)
                self.assertProblem(problems, "--tag", repr(tag), "expected 'v0.1.0'")
                self.assertFalse(out.exists())

    def test_the_tag_follows_the_manifest_version(self):
        root = self.tree()
        write_json(root, "plugins/kai/.claude-plugin/plugin.json", {
            "name": "kai", "version": "0.2.0", "license": "MIT",
            "homepage": REPO_URL, "repository": REPO_URL,
        })
        write_json(root, ".claude-plugin/marketplace.json", {
            "name": "kai", "owner": {"name": "Test"},
            "plugins": [{"name": "kai", "source": "./plugins/kai", "version": "0.2.0", "license": "MIT"}],
        })
        write(root, "plugins/kai/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n\n- Second.\n")
        self.assertEqual(self.built(root, tag="v0.2.0").name, "kai-0.2.0.zip")
        problems, _ = build_release.build(root, self.out(), tag="v0.1.0")
        self.assertProblem(problems, "--tag", "expected 'v0.2.0'")


class TreeChecksBlockTests(BuildCase):
    def test_check_tree_problems_block_the_build(self):
        secret = "kai_at_" + "A1" * 10
        cases = {
            "bin directory": lambda r: write(r, "plugins/kai/bin/tool.md", "x"),
            "possible credential": lambda r: write(r, "plugins/kai/notes.md", f"token: {secret}\n"),
            "executable content": lambda r: write(r, "plugins/kai/hooks.json", "{}"),
            "differs": lambda r: write(r, "plugins/kai/LICENSE", MIT_TEXT + "extra\n"),
        }
        for needle, mutate in cases.items():
            with self.subTest(needle=needle):
                root, out = self.tree(), self.out()
                mutate(root)
                problems, result = build_release.build(root, out)
                self.assertIsNone(result)
                self.assertProblem(problems, needle)
                self.assertFalse(out.exists())

    def test_a_secret_is_never_echoed(self):
        secret = "kai_at_" + "B2" * 10
        root = self.tree()
        write(root, "plugins/kai/notes.md", f"token: {secret}\n")
        problems, _ = build_release.build(root, self.out())
        self.assertNotIn(secret, "\n".join(problems))

    @unittest.skipUnless(os.name == "posix", "executable bits are POSIX only")
    def test_executable_bit_blocks_the_build(self):
        root, out = self.tree(), self.out()
        os.chmod(root / "plugins/kai/README.md", 0o755)
        problems, result = build_release.build(root, out)
        self.assertIsNone(result)
        self.assertProblem(problems, "README.md", "executable bit")
        self.assertFalse(out.exists())


class VerifyZipTests(BuildCase):
    def test_a_minimal_zip_passes(self):
        self.assertEqual(self.verify(GOOD), [])

    def test_two_manifests(self):
        problems = self.verify(GOOD + [("skills/x/plugin.json", b"{}")])
        self.assertProblem(problems, "exactly one plugin.json", "found 2")

    def test_no_manifest(self):
        problems = self.verify([e for e in GOOD if e[0] != MANIFEST])
        self.assertProblem(problems, "exactly one plugin.json", "found 0")

    def test_manifest_in_the_wrong_place(self):
        for name in ("plugin.json", "kai/.claude-plugin/plugin.json", "plugins/kai/.claude-plugin/plugin.json"):
            with self.subTest(name=name):
                entries = [(name, b"{}") if n == MANIFEST else (n, d) for n, d in GOOD]
                self.assertProblem(self.verify(entries), name, f"must be {MANIFEST}")

    def test_manifest_name_is_matched_case_insensitively(self):
        self.assertProblem(self.verify(GOOD + [("skills/Plugin.JSON", b"{}")]), "found 2")

    def test_bin_at_the_top(self):
        for name in ("bin/tool.md", "BIN/tool.md", "Bin/"):
            with self.subTest(name=name):
                self.assertProblem(self.verify(GOOD + [(name, b"")]), name, "bin directory")

    def test_bin_deeper_or_longer_is_fine(self):
        self.assertEqual(self.verify(GOOD + [("skills/bin/x.md", b"x"), ("binary/x.md", b"x")]), [])

    def test_unsafe_paths(self):
        cases = {
            "../evil": "'..' segment",
            "skills/../../evil": "'..' segment",
            "/etc/passwd": "absolute path",
            "C:/Windows/x": "absolute path",
        }
        for name, needle in cases.items():
            with self.subTest(name=name):
                self.assertProblem(self.verify(GOOD + [(name, b"x")]), name, needle)

    def test_backslash_in_a_name(self):
        path = make_zip(self.tmp() / "t.zip", GOOD + [("skills\\x.md", b"x")])
        # On Windows zipfile rewrites the separator while reading: read as on POSIX
        with mock.patch.object(os, "sep", "/"):
            problems = build_release.verify_zip(path)
        self.assertProblem(problems, "skills\\x.md", "backslash")

    def test_duplicate_names(self):
        problems = self.verify(GOOD + [("README.md", b"again")])
        self.assertProblem(problems, "README.md", "duplicate")

    def test_executable_bit(self):
        problems = self.verify(GOOD + [("run.md", b"x", 0o755 << 16)])
        self.assertProblem(problems, "run.md", "executable bit")
        problems = self.verify(GOOD + [("run.md", b"x", 0o641 << 16)])
        self.assertProblem(problems, "run.md", "executable bit")

    def test_a_directory_entry_may_carry_the_executable_bit(self):
        self.assertEqual(self.verify(GOOD + [("skills/", b"", (stat.S_IFDIR | 0o755) << 16)]), [])

    def test_symlink_entry(self):
        problems = self.verify(GOOD + [("link.md", b"README.md", (stat.S_IFLNK | 0o644) << 16)])
        self.assertProblem(problems, "link.md", "symlink")

    def test_required_files(self):
        self.assertProblem(self.verify([e for e in GOOD if e[0] != ".mcp.json"]), ".mcp.json", "missing")
        self.assertProblem(self.verify([e for e in GOOD if e[0] != "README.md"]), "README.md", "missing")

    def test_size_cap(self):
        # GOOD holds 5 bytes
        with mock.patch.object(build_release, "MAX_UNCOMPRESSED_BYTES", 20):
            self.assertEqual(self.verify(GOOD + [("a.md", b"a" * 14)]), [])
            self.assertProblem(self.verify(GOOD + [("a.md", b"a" * 15)]), "uncompressed size", "not under 20")

    def test_the_cap_is_ten_megabytes(self):
        self.assertEqual(build_release.MAX_UNCOMPRESSED_BYTES, 10 * 1024 * 1024)

    def test_corrupt_data_fails_the_crc_check(self):
        path = make_zip(self.tmp() / "t.zip", GOOD + [("a.md", b"hello world")], compress=zipfile.ZIP_STORED)
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"hello world"), 1)
        path.write_bytes(raw.replace(b"hello world", b"hellO world"))
        self.assertProblem(build_release.verify_zip(path), "a.md", "CRC")

    def test_not_a_zip_and_missing_file(self):
        junk = self.tmp() / "junk.zip"
        junk.write_bytes(b"this is not a zip file")
        self.assertProblem(build_release.verify_zip(junk), "junk.zip", "cannot open the zip")
        self.assertProblem(build_release.verify_zip(self.tmp() / "none.zip"), "none.zip", "cannot open the zip")

    def test_every_problem_is_reported_not_just_the_first(self):
        problems = self.verify([("bin/x", b"x"), ("../y", b"y")])
        self.assertProblem(problems, "bin directory")
        self.assertProblem(problems, "'..' segment")
        self.assertProblem(problems, "exactly one plugin.json")
        self.assertProblem(problems, ".mcp.json", "missing")


class NotesTests(BuildCase):
    def notes_for(self, changelog: str | None, **kwargs) -> tuple[list[str], Path, Path]:
        root, out = self.tree(), self.out()
        if changelog is not None:
            write(root, "plugins/kai/CHANGELOG.md", changelog)
        notes = self.tmp() / "sub" / "notes.md"
        problems, _ = build_release.build(root, out, notes=notes, **kwargs)
        return problems, notes, out

    def test_section_body_is_written(self):
        problems, notes, _ = self.notes_for(
            "# Changelog\n\nintro\n\n## 0.1.0\n\n- First version.\n\n### Added\n\n- x\n\n"
            "## 0.0.9\n\n- Old.\n"
        )
        self.assertEqual(problems, [])
        self.assertEqual(notes.read_text(encoding="utf-8"), "- First version.\n\n### Added\n\n- x\n")

    def test_bracket_and_dated_headings(self):
        for heading in ("## [0.1.0]", "## 0.1.0 - 2026-10-29", "## [0.1.0] - 2026-10-29"):
            with self.subTest(heading=heading):
                problems, notes, _ = self.notes_for(f"# Changelog\n\n{heading}\n\n- Body.\n")
                self.assertEqual(problems, [])
                self.assertEqual(notes.read_text(encoding="utf-8"), "- Body.\n")

    def test_the_next_heading_stops_the_body(self):
        _, notes, _ = self.notes_for("## 0.1.0\n- a\n## 0.0.9\n- b\n")
        self.assertEqual(notes.read_text(encoding="utf-8"), "- a\n")

    def test_the_last_section_runs_to_the_end(self):
        _, notes, _ = self.notes_for("# Changelog\n\n## 0.2.0\n\n- New.\n\n## 0.1.0\n\n- First.\n- Second.\n\n\n")
        self.assertEqual(notes.read_text(encoding="utf-8"), "- First.\n- Second.\n")

    def test_crlf_changelog(self):
        _, notes, _ = self.notes_for("# Changelog\r\n\r\n## 0.1.0\r\n\r\n- One.\r\n\r\n## 0.0.9\r\n- Old.\r\n")
        self.assertEqual(notes.read_bytes(), b"- One.\n")

    def test_empty_section_is_a_problem_and_builds_nothing(self):
        for changelog in ("## 0.1.0\n\n## 0.0.9\n\n- Old.\n", "## 0.1.0\n   \n\t\n", "## 0.1.0"):
            with self.subTest(changelog=changelog):
                problems, notes, out = self.notes_for(changelog)
                self.assertProblem(problems, "CHANGELOG.md", "'## 0.1.0' section is empty")
                self.assertFalse(notes.exists())
                self.assertFalse(out.exists())

    def test_missing_section_is_a_problem_and_builds_nothing(self):
        problems, notes, out = self.notes_for("# Changelog\n\n## 0.0.9\n\n- Old.\n")
        self.assertProblem(problems, "CHANGELOG.md", "## 0.1.0")
        self.assertFalse(notes.exists())
        self.assertFalse(out.exists())

    def test_release_notes_directly(self):
        root = self.tree()
        write(root, "plugins/kai/CHANGELOG.md", "## 0.1.01\n\n- Other.\n\n## 0.1.0\n\n- Mine.\n")
        problems: list[str] = []
        self.assertEqual(build_release.release_notes(root, "0.1.0", problems), "- Mine.")
        self.assertEqual(problems, [])
        self.assertIsNone(build_release.release_notes(root, "0.2.0", problems))
        self.assertProblem(problems, "no '## 0.2.0' section")
        (root / "plugins/kai/CHANGELOG.md").unlink()
        problems = []
        self.assertIsNone(build_release.release_notes(root, "0.1.0", problems))
        self.assertProblem(problems, "CHANGELOG.md", "missing")

    def test_a_tag_mismatch_writes_no_notes(self):
        problems, notes, out = self.notes_for(None, tag="v9.9.9")
        self.assertProblem(problems, "--tag")
        self.assertFalse(notes.exists())
        self.assertFalse(out.exists())

    def test_notes_inside_the_plugin_are_refused(self):
        root = self.tree()
        notes = root / "plugins" / "kai" / "notes.md"
        problems, result = build_release.build(root, self.out(), notes=notes)
        self.assertIsNone(result)
        self.assertProblem(problems, "--notes", "inside plugins/kai")
        self.assertFalse(notes.exists())

    def test_unwritable_notes_remove_the_zip(self):
        root, out = self.tree(), self.out()
        notes = self.tmp() / "notes.md"
        notes.mkdir()
        problems, result = build_release.build(root, out, notes=notes)
        self.assertIsNone(result)
        self.assertProblem(problems, "cannot write the notes")
        self.assertEqual(list(out.iterdir()), [])


class CliTests(BuildCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build_release.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_success_exits_zero_and_prints_one_line(self):
        root, out = self.tree(), self.out()
        notes = self.tmp() / "notes.md"
        code, stdout, stderr = self.run_main([
            "--root", str(root), "--out", str(out), "--tag", "v0.1.0", "--notes", str(notes),
        ])
        self.assertEqual((code, stderr), (0, ""))
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^build_release: wrote .*kai-0\.1\.0\.zip \(5 files, \d+ bytes\)$")
        self.assertTrue((out / "kai-0.1.0.zip").is_file())
        self.assertEqual(notes.read_text(encoding="utf-8"), "- First version.\n")

    def test_the_size_in_the_line_is_the_file_size(self):
        root, out = self.tree(), self.out()
        _, stdout, _ = self.run_main(["--root", str(root), "--out", str(out)])
        size = int(re.search(r"(\d+) bytes", stdout).group(1))
        self.assertEqual(size, (out / "kai-0.1.0.zip").stat().st_size)

    def test_problems_exit_one_and_are_printed(self):
        root, out = self.tree(), self.out()
        code, stdout, stderr = self.run_main(["--root", str(root), "--out", str(out), "--tag", "v9.9.9"])
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("--tag: 'v9.9.9' does not match the plugin.json version (expected 'v0.1.0')", stderr)
        self.assertIn("1 problem(s)", stderr)
        self.assertFalse(out.exists())

    def test_tree_problems_exit_one(self):
        root = self.tree()
        write(root, "plugins/kai/bin/x.md", "x")
        code, _, stderr = self.run_main(["--root", str(root), "--out", str(self.out())])
        self.assertEqual(code, 1)
        self.assertIn("plugins/kai/bin", stderr)

    def test_bad_arguments_exit_two(self):
        self.assertEqual(self.run_main(["--bogus"])[0], 2)
        self.assertEqual(self.run_main([])[0], 2)
        self.assertEqual(self.run_main(["--root", str(self.tree())])[0], 2)
        self.assertEqual(self.run_main(["--out"])[0], 2)
        missing = Path(tempfile.gettempdir()) / "kai-build-does-not-exist-xyz"
        self.assertEqual(self.run_main(["--root", str(missing), "--out", str(self.out())])[0], 2)

    def test_non_ascii_problem_text_is_printed_safely(self):
        root = self.tree()
        write(root, "plugins/kai/bin/caff\u00e8.md", "x")
        code, _, stderr = self.run_main(["--root", str(root), "--out", str(self.out())])
        self.assertEqual(code, 1)
        stderr.encode("ascii")

    def test_the_cli_runs_as_a_script(self):
        root, out = self.tree(), self.out()
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "build_release.py"),
             "--root", str(root), "--out", str(out), "--tag", "v0.1.0"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("build_release: wrote", result.stdout)
        mismatch = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "build_release.py"),
             "--root", str(root), "--out", str(out), "--tag", "v9.9.9"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn("does not match", mismatch.stderr)


if __name__ == "__main__":
    unittest.main()
