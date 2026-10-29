#!/usr/bin/env python3
"""Static checks on the repository tree (stdlib only).

Usage: python scripts/check_tree.py [--root PATH]
Exit codes: 0 clean, 1 problems found, 2 bad arguments.

Every problem is printed as "path[:line]: message". Rules:

  a. no bin directory at the top of a plugin (claude.ai and Cowork refuse it)
  b. no symlinks and no Windows junctions
  c. no credentials: forbidden file names, plus a content scan of every text
     file (rule name and line only, the matched text is never printed)
  d. plugins/kai carries no executable content: no hooks or scripts directory,
     no hooks.json or .lsp.json, no script or binary extensions, no shebang, no
     executable bit (POSIX), no inline hooks / lspServers / mcpServers in the
     manifests
  e. plugins/kai/.mcp.json is one plain https server named kai with no header,
     env, command or credential in the URL
  f. the manifests agree: name, semver version, license, LICENSE files,
     CHANGELOG entry, homepage and repository

Scope: everything under ROOT except the top-level .git. When ROOT is a git
checkout, files that git ignores and does not track (a local .env, .venv) are
skipped, because they never reach the repository. A clean CI checkout has none.
Text files over 1 MB are reported instead of scanned. A file is binary when its
first 8 KB hold a NUL byte; binary files are not scanned.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "kai"
MAX_SCAN_BYTES = 1024 * 1024
BINARY_PROBE_BYTES = 8192

FORBIDDEN_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12")

# The sk- rule needs a token start, or ordinary words such as "task-..." match
SECRET_RULES = (
    ("kai-access-token", re.compile(r"kai_at_[A-Za-z0-9_-]{16,}")),
    ("kai-refresh-token", re.compile(r"kai_rt_[A-Za-z0-9_-]{16,}")),
    ("sk-api-key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github-fine-grained-token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("pem-private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("bearer-literal-token", re.compile(r"Bearer[ \t]+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
)

CODE_DIR_NAMES = {"hooks", "scripts"}
CODE_FILE_NAMES = {"hooks.json", ".lsp.json"}
CODE_SUFFIXES = {
    ".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts",
    ".ps1", ".bat", ".cmd", ".exe", ".dll", ".so",
}
INLINE_EXEC_KEYS = ("hooks", "lspServers", "mcpServers")
MCP_FORBIDDEN_KEYS = ("headers", "headersHelper", "env", "command", "args")
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def _rel(root: Path, path: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _git_ignored(root: Path) -> set[str]:
    # Untracked and ignored paths, posix, directories with a trailing slash
    if not (root / ".git").exists():
        return set()
    cmd = [
        "git", "-C", str(root), "ls-files",
        "--others", "--ignored", "--exclude-standard", "--directory", "-z",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    if res.returncode != 0:
        return set()
    return {p for p in res.stdout.decode("utf-8", "replace").split("\0") if p}


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(entry.path))


def _walk(root: Path, ignored: set[str]):
    # Own walk: os.walk would descend into junctions
    links: list[Path] = []
    dirs: list[Path] = []
    files: list[Path] = []
    errors: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            errors.append(f"{_rel(root, directory)}: cannot read directory ({type(exc).__name__})")
            return
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            if directory == root and entry.name == ".git":
                continue
            if rel in ignored or rel + "/" in ignored:
                continue
            if _is_link(entry):
                links.append(path)
            elif entry.is_dir(follow_symlinks=False):
                dirs.append(path)
                visit(path)
            else:
                files.append(path)

    visit(root)
    return links, dirs, files, errors


def _load_json(root: Path, path: Path, problems: list[str]):
    rel = _rel(root, path)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        problems.append(f"{rel}: file is missing")
        return None
    except UnicodeDecodeError:
        problems.append(f"{rel}: not valid UTF-8")
        return None
    except OSError as exc:
        problems.append(f"{rel}: cannot read file ({type(exc).__name__})")
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        problems.append(f"{rel}:{exc.lineno}: invalid JSON ({exc.msg})")
        return None


def _check_bin(root: Path, problems: list[str]) -> None:
    plugins = root / "plugins"
    if not plugins.is_dir():
        return
    for plugin in sorted(plugins.iterdir()):
        if not plugin.is_dir():
            continue
        for child in sorted(plugin.iterdir()):
            if child.name.lower() == "bin":
                problems.append(
                    f"{_rel(root, child)}: a bin directory at the top of a plugin is "
                    "refused by claude.ai and Cowork"
                )


def _check_links(root: Path, links: list[Path], problems: list[str]) -> None:
    for path in links:
        problems.append(f"{_rel(root, path)}: symlink or junction is not allowed")


def _read_scannable(root: Path, path: Path, problems: list[str]) -> str | None:
    rel = _rel(root, path)
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_SCAN_BYTES + 1)
    except OSError as exc:
        problems.append(f"{rel}: cannot read file ({type(exc).__name__})")
        return None
    if b"\0" in data[:BINARY_PROBE_BYTES]:
        return None
    if len(data) > MAX_SCAN_BYTES:
        problems.append(f"{rel}: text file over 1 MB, not scanned for credentials")
        return None
    return data.decode("utf-8", "replace")


def _check_credentials(root: Path, files: list[Path], problems: list[str]) -> None:
    for path in files:
        rel = _rel(root, path)
        name = path.name.lower()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in FORBIDDEN_NAMES):
            problems.append(f"{rel}: file name suggests a credential or key file")
        text = _read_scannable(root, path, problems)
        if text is None:
            continue
        for number, line in enumerate(text.split("\n"), 1):
            for rule, pattern in SECRET_RULES:
                if pattern.search(line):
                    problems.append(f"{rel}:{number}: possible credential ({rule})")


def _check_no_code(root: Path, dirs: list[Path], files: list[Path], problems: list[str]) -> None:
    base = root / "plugins" / PLUGIN
    if not base.is_dir():
        return
    for path in dirs:
        if path.is_relative_to(base) and path.name.lower() in CODE_DIR_NAMES:
            problems.append(f"{_rel(root, path)}: {path.name} directory: executable content is not allowed")
    for path in files:
        if not path.is_relative_to(base):
            continue
        rel = _rel(root, path)
        name = path.name.lower()
        if name in CODE_FILE_NAMES:
            problems.append(f"{rel}: {path.name}: executable content is not allowed")
        elif path.suffix.lower() in CODE_SUFFIXES:
            problems.append(f"{rel}: {path.suffix.lower()} file: executable content is not allowed")
        elif _has_shebang(path):
            problems.append(f"{rel}: starts with a shebang: executable content is not allowed")
        elif os.name == "posix" and os.lstat(path).st_mode & 0o111:
            problems.append(f"{rel}: executable bit is set: executable content is not allowed")


def _has_shebang(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _check_mcp_json(root: Path, problems: list[str]) -> None:
    path = root / "plugins" / PLUGIN / ".mcp.json"
    rel = _rel(root, path)
    data = _load_json(root, path, problems)
    if data is None:
        return
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        problems.append(f"{rel}: must be an object with an 'mcpServers' object")
        return
    if list(servers) != [PLUGIN]:
        found = ", ".join(repr(k) for k in servers) or "none"
        problems.append(f"{rel}: mcpServers must hold exactly one server named '{PLUGIN}' (found: {found})")
    server = servers.get(PLUGIN)
    if server is None:
        return
    if not isinstance(server, dict):
        problems.append(f"{rel}: server '{PLUGIN}' must be an object")
        return
    if server.get("type") != "http":
        problems.append(f"{rel}: server type must be 'http'")
    for key in MCP_FORBIDDEN_KEYS:
        if key in server:
            problems.append(f"{rel}: key '{key}' is not allowed: the shipped config carries no secret and runs nothing")
    url = server.get("url")
    if not (isinstance(url, str) and url.startswith("https://") and url.endswith("/mcp")):
        problems.append(f"{rel}: url must start with https:// and end with /mcp")
    elif _url_has_extras(url):
        problems.append(f"{rel}: url must have no credentials, query or fragment")


def _url_has_extras(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        return True


def _check_consistency(root: Path, problems: list[str]) -> None:
    plugin_path = root / "plugins" / PLUGIN / ".claude-plugin" / "plugin.json"
    market_path = root / ".claude-plugin" / "marketplace.json"
    plugin_rel = _rel(root, plugin_path)
    market_rel = _rel(root, market_path)
    plugin = _load_json(root, plugin_path, problems)
    market = _load_json(root, market_path, problems)

    version = None
    if isinstance(plugin, dict):
        if plugin.get("name") != PLUGIN:
            problems.append(f"{plugin_rel}: name must be '{PLUGIN}'")
        version = plugin.get("version")
        if not (isinstance(version, str) and SEMVER.fullmatch(version)):
            problems.append(f"{plugin_rel}: version must be semver X.Y.Z")
            version = None
        if plugin.get("license") != "MIT":
            problems.append(f"{plugin_rel}: license must be 'MIT'")
        homepage, repository = plugin.get("homepage"), plugin.get("repository")
        if not (isinstance(homepage, str) and homepage and homepage == repository):
            problems.append(f"{plugin_rel}: homepage and repository must be the same URL")
        for key in INLINE_EXEC_KEYS:
            if key in plugin:
                problems.append(f"{plugin_rel}: '{key}' declared inline: executable content is not allowed")
    elif plugin is not None:
        problems.append(f"{plugin_rel}: must be a JSON object")

    if isinstance(market, dict):
        entry = _marketplace_entry(market)
        if entry is None:
            problems.append(f"{market_rel}: no plugin entry named '{PLUGIN}'")
        else:
            if version is not None and entry.get("version") != version:
                problems.append(
                    f"{market_rel}: version of the '{PLUGIN}' entry must equal {plugin_rel} version ({version})"
                )
            if entry.get("license") != "MIT":
                problems.append(f"{market_rel}: license of the '{PLUGIN}' entry must be 'MIT'")
            for key in INLINE_EXEC_KEYS:
                if key in entry:
                    problems.append(f"{market_rel}: '{key}' declared inline: executable content is not allowed")
    elif market is not None:
        problems.append(f"{market_rel}: must be a JSON object")

    _check_licenses(root, problems)
    if version is not None:
        _check_changelog(root, version, problems)


def _marketplace_entry(market: dict):
    entries = market.get("plugins")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == PLUGIN:
            return entry
    return None


def _check_licenses(root: Path, problems: list[str]) -> None:
    texts = {}
    for path in (root / "LICENSE", root / "plugins" / PLUGIN / "LICENSE"):
        try:
            texts[path] = path.read_bytes().replace(b"\r\n", b"\n")
        except FileNotFoundError:
            problems.append(f"{_rel(root, path)}: file is missing")
        except OSError as exc:
            problems.append(f"{_rel(root, path)}: cannot read file ({type(exc).__name__})")
    if len(texts) == 2 and len(set(texts.values())) != 1:
        problems.append(f"plugins/{PLUGIN}/LICENSE: text differs from the root LICENSE")


def _check_changelog(root: Path, version: str, problems: list[str]) -> None:
    path = root / "plugins" / PLUGIN / "CHANGELOG.md"
    rel = _rel(root, path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        problems.append(f"{rel}: file is missing")
        return
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{rel}: cannot read file ({type(exc).__name__})")
        return
    heading = re.compile(r"##[ \t]+\[?" + re.escape(version) + r"\]?(?:[ \t].*)?")
    if not any(heading.fullmatch(line.rstrip()) for line in text.split("\n")):
        problems.append(f"{rel}: no '## {version}' entry for the current version")


def check(root: Path) -> list[str]:
    root = Path(root)
    problems: list[str] = []
    links, dirs, files, errors = _walk(root, _git_ignored(root))
    problems.extend(errors)
    _check_bin(root, problems)
    _check_links(root, links, problems)
    _check_credentials(root, files, problems)
    _check_no_code(root, dirs, files, problems)
    _check_mcp_json(root, problems)
    _check_consistency(root, problems)
    return problems


def _emit(text: str, stream) -> None:
    # Paths can hold characters a Windows console cannot print
    print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static checks on the plugin repository tree.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this repo)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.root.is_dir():
        _emit(f"check_tree: not a directory: {args.root}", sys.stderr)
        return 2
    problems = check(args.root)
    for problem in problems:
        _emit(problem, sys.stderr)
    if problems:
        _emit(f"check_tree: {len(problems)} problem(s)", sys.stderr)
        return 1
    _emit("check_tree: ok", sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
