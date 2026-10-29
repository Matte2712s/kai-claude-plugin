"""Tests for scripts/check_tree.py (stdlib unittest)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_tree  # noqa: E402

MIT_TEXT = "MIT License\n\nCopyright (c) 2026 Test\n\nPermission is hereby granted.\n"
REPO_URL = "https://github.com/example/kai-claude-plugin"

# Built at run time so this file holds no string that the credential scan matches
SECRET_SAMPLES = {
    "kai-access-token": "kai_at_" + "A1" * 10,
    "kai-refresh-token": "kai_rt_" + "b2" * 10,
    "sk-api-key": "sk-" + "c3" * 12,
    "github-token": "ghp_" + "D4" * 12,
    "github-fine-grained-token": "github_pat_" + "e5" * 12,
    "aws-access-key": "AKIA" + "Z9" * 8,
    "slack-token": "xoxb-" + "1234567890",
    "pem-private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "bearer-literal-token": "Authorization: Bearer " + "f6" * 12,
}


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


def edit_json(root: Path, rel: str, fn) -> None:
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    fn(data)
    write_json(root, rel, data)


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


class TreeCase(unittest.TestCase):
    def tree(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="kai-tree-"))
        self.addCleanup(_force_rmtree, root)
        build_valid_tree(root)
        return root

    def assertProblem(self, problems: list[str], *needles: str) -> None:
        self.assertTrue(
            any(all(n in p for n in needles) for p in problems),
            f"no problem with {needles!r} in {problems!r}",
        )

    def assertClean(self, root: Path) -> None:
        self.assertEqual(check_tree.check(root), [])


class CleanTreeTests(TreeCase):
    def test_valid_tree_passes(self):
        self.assertClean(self.tree())

    def test_the_real_repo_passes(self):
        self.assertEqual(check_tree.check(REPO_ROOT), [])

    def test_markdown_json_and_text_are_fine_in_the_plugin(self):
        root = self.tree()
        write(root, "plugins/kai/skills/x/SKILL.md", "---\nname: x\ndescription: y\n---\n")
        write(root, "plugins/kai/skills/x/references/a.md", "text\n")
        write(root, "plugins/kai/notes.txt", "text\n")
        write_json(root, "plugins/kai/data.json", {"a": 1})
        self.assertClean(root)


class BinDirectoryTests(TreeCase):
    def test_bin_directory_is_refused(self):
        root = self.tree()
        write(root, "plugins/kai/bin/tool.md", "x")
        self.assertProblem(check_tree.check(root), "plugins/kai/bin", "bin directory")

    def test_bin_directory_of_another_plugin_is_refused(self):
        root = self.tree()
        write(root, "plugins/other/bin/tool.md", "x")
        self.assertProblem(check_tree.check(root), "plugins/other/bin")

    def test_nested_bin_directory_is_not_a_top_level_one(self):
        root = self.tree()
        write(root, "plugins/kai/skills/x/bin/readme.md", "x")
        self.assertFalse([p for p in check_tree.check(root) if "bin directory" in p])


class LinkTests(TreeCase):
    def test_entries_the_walker_calls_links_are_reported_and_not_entered(self):
        # Covers the plumbing on hosts where real links cannot be created
        root = self.tree()
        write(root, "plugins/kai/fake/inner.md", "x")
        real = check_tree._is_link
        with mock.patch.object(
            check_tree, "_is_link", lambda entry: entry.name == "fake" or real(entry)
        ):
            problems = check_tree.check(root)
        self.assertEqual(problems, ["plugins/kai/fake: symlink or junction is not allowed"])

    def test_symlinks_are_refused(self):
        root = self.tree()
        target = write(root, "target.md", "x")
        link = root / "plugins" / "kai" / "link.md"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"symlinks not permitted on this host: {exc}")
        self.assertProblem(check_tree.check(root), "plugins/kai/link.md", "symlink")

    def test_directory_symlinks_are_refused_and_not_followed(self):
        root = self.tree()
        target = root / "real"
        write(target, "inner.md", "x")
        link = root / "plugins" / "kai" / "dirlink"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"symlinks not permitted on this host: {exc}")
        problems = check_tree.check(root)
        self.assertProblem(problems, "plugins/kai/dirlink", "symlink")
        self.assertFalse([p for p in problems if "dirlink/" in p])

    @unittest.skipUnless(os.name == "nt", "junctions exist on Windows only")
    def test_junctions_are_refused(self):
        root = self.tree()
        target = root / "real"
        write(target, "inner.md", "x")
        link = root / "plugins" / "kai" / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if result.returncode != 0:
            self.skipTest("could not create a junction on this host")
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        problems = check_tree.check(root)
        self.assertProblem(problems, "plugins/kai/junction", "symlink or junction")
        self.assertFalse([p for p in problems if "junction/" in p])


class CredentialTests(TreeCase):
    def test_forbidden_file_names(self):
        for name in (".env", ".env.local", "server.pem", "tls.key", "id_rsa", "id_rsa.pub", "store.p12", "UP.PEM"):
            with self.subTest(name=name):
                root = self.tree()
                write(root, name, "harmless\n")
                self.assertProblem(check_tree.check(root), name, "credential or key file")

    def test_similar_file_names_are_fine(self):
        root = self.tree()
        for name in ("key.md", "monkey.md", "environment.md", "id.md", "pem.md"):
            write(root, name, "harmless\n")
        self.assertClean(root)

    def test_every_secret_rule_reports_rule_and_line_but_never_the_secret(self):
        for rule, secret in SECRET_SAMPLES.items():
            with self.subTest(rule=rule):
                root = self.tree()
                write(root, "docs/notes.md", f"first line\ntoken: {secret}\nlast line\n")
                problems = check_tree.check(root)
                self.assertEqual(problems, [f"docs/notes.md:2: possible credential ({rule})"])
                self.assertNotIn(secret, "\n".join(problems))
                self.assertNotIn(secret[-12:], "\n".join(problems))

    def test_secret_in_the_json_manifest_is_found_too(self):
        root = self.tree()
        secret = SECRET_SAMPLES["kai-access-token"]
        edit_json(root, "plugins/kai/.mcp.json", lambda d: d["mcpServers"]["kai"].update(token=secret))
        problems = check_tree.check(root)
        self.assertProblem(problems, "plugins/kai/.mcp.json", "kai-access-token")
        self.assertNotIn(secret, "\n".join(problems))

    def test_placeholders_and_prose_are_not_secrets(self):
        root = self.tree()
        write(root, "docs/ok.md", (
            "Authorization: Bearer ${EVAL_KAI_TOKEN}\n"
            "Bearer $TOKEN\n"
            "a bearer of good news\n"
            "task-management-integration-with-everything\n"
            "kai_at_ is the prefix of an access token\n"
            "sk-short\n"
        ))
        self.assertClean(root)

    def test_binary_files_are_not_scanned(self):
        root = self.tree()
        write(root, "icon.png", b"\x89PNG\0\0" + SECRET_SAMPLES["kai-access-token"].encode())
        self.assertClean(root)

    def test_text_file_over_one_megabyte_is_reported(self):
        root = self.tree()
        write(root, "big.txt", "a" * (1024 * 1024 + 10))
        self.assertProblem(check_tree.check(root), "big.txt", "over 1 MB")

    def test_git_directory_is_skipped(self):
        root = self.tree()
        write(root, ".git/config", "token = " + SECRET_SAMPLES["kai-access-token"] + "\n")
        write(root, ".git/id_rsa", "x")
        self.assertClean(root)

    def test_git_ignored_untracked_files_are_skipped_but_tracked_ones_are_not(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        root = self.tree()
        write(root, ".gitignore", ".env\nignored/\n")
        write(root, ".env", "token=" + SECRET_SAMPLES["kai-access-token"] + "\n")
        write(root, "ignored/leak.key", "x")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env, check=False)

        if git("init", "-q").returncode != 0:
            self.skipTest("git init failed")
        self.assertClean(root)
        self.assertEqual(git("add", "-f", ".env").returncode, 0)
        self.assertProblem(check_tree.check(root), ".env", "credential")


class NoExecutableContentTests(TreeCase):
    def test_hooks_and_scripts_directories(self):
        for rel in ("plugins/kai/hooks/note.md", "plugins/kai/scripts/note.md", "plugins/kai/skills/x/scripts/note.md"):
            with self.subTest(rel=rel):
                root = self.tree()
                write(root, rel, "x")
                self.assertProblem(check_tree.check(root), "directory", "executable content")

    def test_hooks_json_and_lsp_json(self):
        for rel in ("plugins/kai/hooks.json", "plugins/kai/.lsp.json", "plugins/kai/x/hooks.json"):
            with self.subTest(rel=rel):
                root = self.tree()
                write_json(root, rel, {})
                self.assertProblem(check_tree.check(root), rel, "executable content")

    def test_script_and_binary_extensions(self):
        for suffix in (".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".bat", ".cmd", ".exe", ".dll", ".so", ".SH"):
            with self.subTest(suffix=suffix):
                root = self.tree()
                write(root, f"plugins/kai/tool{suffix}", "x")
                self.assertProblem(check_tree.check(root), f"tool{suffix}", "executable content")

    def test_executable_files_outside_the_plugin_are_fine(self):
        root = self.tree()
        write(root, "scripts/tool.py", "print(1)\n")
        write(root, "scripts/run.sh", "echo\n")
        self.assertClean(root)

    def test_shebang_without_extension(self):
        root = self.tree()
        write(root, "plugins/kai/helper", "#!/bin/sh\necho\n")
        self.assertProblem(check_tree.check(root), "plugins/kai/helper", "shebang")

    @unittest.skipUnless(os.name == "posix", "executable bits are POSIX only")
    def test_executable_bit(self):
        root = self.tree()
        path = write(root, "plugins/kai/notes.md", "x")
        os.chmod(path, 0o755)
        self.assertProblem(check_tree.check(root), "plugins/kai/notes.md", "executable bit")

    def test_inline_hooks_in_a_manifest(self):
        for key in ("hooks", "lspServers", "mcpServers"):
            with self.subTest(key=key):
                root = self.tree()
                edit_json(root, "plugins/kai/.claude-plugin/plugin.json", lambda d, k=key: d.update({k: {}}))
                self.assertProblem(check_tree.check(root), "plugin.json", key, "inline")
        root = self.tree()
        edit_json(root, ".claude-plugin/marketplace.json", lambda d: d["plugins"][0].update(hooks={}))
        self.assertProblem(check_tree.check(root), "marketplace.json", "hooks", "inline")


class McpJsonTests(TreeCase):
    def mcp(self, fn) -> list[str]:
        root = self.tree()
        edit_json(root, "plugins/kai/.mcp.json", fn)
        return check_tree.check(root)

    def test_extra_keys_are_refused(self):
        for key in ("headers", "headersHelper", "env", "command", "args"):
            with self.subTest(key=key):
                problems = self.mcp(lambda d, k=key: d["mcpServers"]["kai"].update({k: "x"}))
                self.assertProblem(problems, ".mcp.json", f"'{key}'")

    def test_url_rules(self):
        cases = {
            "http://kai.example.org/mcp": "https://",
            "https://kai.example.org/other": "/mcp",
            "https://kai.example.org/mcp/": "/mcp",
            "": "https://",
            "https://user:pw@kai.example.org/mcp": "credentials",
            "https://kai.example.org/a?x=/mcp": "query",
        }
        for url, needle in cases.items():
            with self.subTest(url=url):
                problems = self.mcp(lambda d, u=url: d["mcpServers"]["kai"].update(url=u))
                self.assertProblem(problems, ".mcp.json", needle)

    def test_missing_url_and_wrong_type(self):
        self.assertProblem(self.mcp(lambda d: d["mcpServers"]["kai"].pop("url")), "url must start with https://")
        self.assertProblem(self.mcp(lambda d: d["mcpServers"]["kai"].update(type="sse")), "type must be 'http'")
        self.assertProblem(self.mcp(lambda d: d["mcpServers"]["kai"].pop("type")), "type must be 'http'")

    def test_wrong_server_name(self):
        def rename(d):
            d["mcpServers"] = {"other": d["mcpServers"]["kai"]}

        self.assertProblem(self.mcp(rename), "exactly one server named 'kai'", "'other'")

    def test_second_server(self):
        def add(d):
            d["mcpServers"]["extra"] = {"type": "http", "url": "https://x.example.org/mcp"}

        self.assertProblem(self.mcp(add), "exactly one server named 'kai'", "'extra'")

    def test_no_servers_and_missing_wrapper(self):
        self.assertProblem(self.mcp(lambda d: d.update(mcpServers={})), "exactly one server")
        root = self.tree()
        write_json(root, "plugins/kai/.mcp.json", {"kai": {"type": "http", "url": "https://x.org/mcp"}})
        self.assertProblem(check_tree.check(root), ".mcp.json", "'mcpServers' object")

    def test_invalid_json_reports_the_line(self):
        root = self.tree()
        write(root, "plugins/kai/.mcp.json", '{\n  "mcpServers": \n  oops\n}\n')
        self.assertProblem(check_tree.check(root), "plugins/kai/.mcp.json:3", "invalid JSON")

    def test_missing_file(self):
        root = self.tree()
        (root / "plugins/kai/.mcp.json").unlink()
        self.assertProblem(check_tree.check(root), "plugins/kai/.mcp.json", "missing")


class ConsistencyTests(TreeCase):
    PLUGIN = "plugins/kai/.claude-plugin/plugin.json"
    MARKET = ".claude-plugin/marketplace.json"

    def plugin(self, fn) -> list[str]:
        root = self.tree()
        edit_json(root, self.PLUGIN, fn)
        return check_tree.check(root)

    def test_plugin_name(self):
        self.assertProblem(self.plugin(lambda d: d.update(name="other")), "plugin.json", "name must be 'kai'")

    def test_version_must_be_semver(self):
        for version in ("1.0", "v1.0.0", "1.0.0-rc1", "01.0.0", "", 3, None):
            with self.subTest(version=version):
                self.assertProblem(self.plugin(lambda d, v=version: d.update(version=v)), "semver")

    def test_version_must_match_the_marketplace(self):
        problems = self.plugin(lambda d: d.update(version="0.2.0"))
        self.assertProblem(problems, "marketplace.json", "must equal", "0.2.0")

    def test_marketplace_entry_problems(self):
        root = self.tree()
        edit_json(root, self.MARKET, lambda d: d["plugins"][0].pop("version"))
        self.assertProblem(check_tree.check(root), "marketplace.json", "must equal")
        root = self.tree()
        edit_json(root, self.MARKET, lambda d: d["plugins"][0].update(name="other"))
        self.assertProblem(check_tree.check(root), "marketplace.json", "no plugin entry named 'kai'")
        root = self.tree()
        edit_json(root, self.MARKET, lambda d: d.update(plugins="x"))
        self.assertProblem(check_tree.check(root), "marketplace.json", "no plugin entry")

    def test_license_in_both_manifests(self):
        self.assertProblem(self.plugin(lambda d: d.update(license="GPL-3.0")), "plugin.json", "license")
        self.assertProblem(self.plugin(lambda d: d.pop("license")), "plugin.json", "license")
        root = self.tree()
        edit_json(root, self.MARKET, lambda d: d["plugins"][0].update(license="Apache-2.0"))
        self.assertProblem(check_tree.check(root), "marketplace.json", "license")

    def test_license_files(self):
        root = self.tree()
        (root / "LICENSE").unlink()
        self.assertProblem(check_tree.check(root), "LICENSE", "missing")
        root = self.tree()
        (root / "plugins/kai/LICENSE").unlink()
        self.assertProblem(check_tree.check(root), "plugins/kai/LICENSE", "missing")
        root = self.tree()
        write(root, "plugins/kai/LICENSE", MIT_TEXT + "extra\n")
        self.assertProblem(check_tree.check(root), "plugins/kai/LICENSE", "differs")

    def test_license_line_endings_do_not_count(self):
        root = self.tree()
        write(root, "plugins/kai/LICENSE", MIT_TEXT.replace("\n", "\r\n").encode())
        self.assertClean(root)

    def test_changelog_needs_the_current_version(self):
        root = self.tree()
        write(root, "plugins/kai/CHANGELOG.md", "# Changelog\n\n## 0.0.9\n\n- Old.\n")
        self.assertProblem(check_tree.check(root), "CHANGELOG.md", "## 0.1.0")
        root = self.tree()
        write(root, "plugins/kai/CHANGELOG.md", "# Changelog\n\n## 0.1.01\n")
        self.assertProblem(check_tree.check(root), "CHANGELOG.md", "## 0.1.0")
        root = self.tree()
        (root / "plugins/kai/CHANGELOG.md").unlink()
        self.assertProblem(check_tree.check(root), "CHANGELOG.md", "missing")

    def test_changelog_heading_variants(self):
        for heading in ("## 0.1.0", "## 0.1.0 - 2026-10-29", "## [0.1.0]", "## [0.1.0] - 2026-10-29"):
            with self.subTest(heading=heading):
                root = self.tree()
                write(root, "plugins/kai/CHANGELOG.md", f"# Changelog\n\n{heading}\n\n- x\n")
                self.assertClean(root)

    def test_homepage_and_repository(self):
        self.assertProblem(self.plugin(lambda d: d.update(homepage="https://example.org")), "homepage and repository")
        self.assertProblem(self.plugin(lambda d: d.pop("repository")), "homepage and repository")
        self.assertProblem(self.plugin(lambda d: d.pop("homepage")), "homepage and repository")

    def test_missing_manifests(self):
        root = self.tree()
        (root / self.PLUGIN).unlink()
        self.assertProblem(check_tree.check(root), "plugin.json", "missing")
        root = self.tree()
        (root / self.MARKET).unlink()
        self.assertProblem(check_tree.check(root), "marketplace.json", "missing")


class CliTests(TreeCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_tree.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_clean_exits_zero(self):
        code, out, err = self.run_main(["--root", str(self.tree())])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ok", out)

    def test_problems_exit_one_and_are_printed(self):
        root = self.tree()
        write(root, "plugins/kai/bin/x.md", "x")
        code, _, err = self.run_main(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("plugins/kai/bin", err)
        self.assertIn("problem(s)", err)

    def test_bad_arguments_exit_two(self):
        self.assertEqual(self.run_main(["--bogus"])[0], 2)
        missing = Path(tempfile.gettempdir()) / "kai-tree-does-not-exist-xyz"
        self.assertEqual(self.run_main(["--root", str(missing)])[0], 2)

    def test_the_cli_runs_as_a_script(self):
        root = self.tree()
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_tree.py"), "--root", str(root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
