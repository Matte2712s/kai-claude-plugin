#!/usr/bin/env python3
"""Check the vendored tool contract and the skills against it (stdlib only).

Usage: python scripts/check_contract.py [--root PATH] [--contract PATH]
Exit codes: 0 clean, 1 problems found, 2 bad arguments.
--contract is relative to --root (default contract/mcp_tools.json).

Every problem is printed as "path[:line]: message".

Contract file: schema 1, instructions as text, at least one tool; each tool has
a unique name, a description and inputSchema.properties as an object.
Instructions, tool descriptions and property descriptions stay under 4096
characters (Claude Code cuts them there and Claude sees only the text before).

Documents checked (those that exist): plugins/kai/skills/*/SKILL.md,
plugins/kai/skills/*/references/*.md and plugins/kai/commands/*.md.

Conventions skill authors must follow, so the check can see what is named:

  1. Tool names. Write the full name mcp__plugin_kai_kai__<tool> wherever a
     tool is named; <tool> must be in the contract. The wildcard form with an
     asterisk is ignored.
  2. Call notation. In an inline code span or a fenced block, write a call as
     tool(p1, p2=..., p3: type) where tool is a contract tool, with or without
     the full-name prefix. Arguments are split on commas outside brackets and
     quotes; the parameter name is the text before the first "=" or ":" (or the
     whole argument), and it must be a property of that tool's inputSchema. A
     lone "..." argument is ignored. Put the name first on every argument:
     a bare literal such as "text" or an optional marker such as [p] is read as
     a parameter name and fails. Brackets and quotes must balance.
  3. Parameter reference. An inline code span holding exactly tool.param (with
     or without the prefix) names a parameter; param must be a property of tool.
     Do not use that form for fields of a response or for file names.
  4. Frontmatter (SKILL.md only). The first line is "---" and a closing "---"
     follows. The only keys are name (equal to the skill directory name) and
     description (not empty, at most 1536 characters). Supported values: plain,
     single or double quoted on one line, and block scalars ">" and "|" with an
     optional "-" or "+". Not supported, and reported as such: lists, nested
     maps, flow collections, anchors, tags, multi-line plain or quoted values,
     indentation indicators and a plain value that holds ": " or " #" (quote it).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = Path("contract") / "mcp_tools.json"
PLUGIN_DIR = Path("plugins") / "kai"
TOOL_PREFIX = "mcp__plugin_kai_kai__"
MAX_TEXT = 4096
MAX_SKILL_DESCRIPTION = 1536

_IDENT = r"[A-Za-z][A-Za-z0-9_]*"
_FULL_NAME = re.compile(re.escape(TOOL_PREFIX) + r"([A-Za-z0-9_]+)")
_CALL = re.compile(r"(?<![A-Za-z0-9_.])(?:" + re.escape(TOOL_PREFIX) + r")?(" + _IDENT + r")\(")
_PARAM_REF = re.compile(r"(?:" + re.escape(TOOL_PREFIX) + r")?(" + _IDENT + r")\.([A-Za-z_][A-Za-z0-9_]*)")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]+(.*))?$")
_BLOCK_HEADER = re.compile(r"^([|>])([+-]?)[ \t]*(?:#.*)?$")


def _rel(root: Path, path: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


# Contract

def load_contract(root: Path, contract_file: Path):
    """Return (tools, problems); tools maps name to the set of property names, or None if unusable."""
    rel = _rel(root, contract_file)
    problems: list[str] = []
    try:
        raw = contract_file.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None, [f"{rel}: contract file is missing"]
    except UnicodeDecodeError:
        return None, [f"{rel}: not valid UTF-8"]
    except OSError as exc:
        return None, [f"{rel}: cannot read file ({type(exc).__name__})"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"{rel}:{exc.lineno}: invalid JSON ({exc.msg})"]
    if not isinstance(data, dict):
        return None, [f"{rel}: must be a JSON object"]

    schema = data.get("schema")
    if schema != 1 or isinstance(schema, bool):
        problems.append(f"{rel}: 'schema' must be 1")
    instructions = data.get("instructions")
    if not isinstance(instructions, str):
        problems.append(f"{rel}: 'instructions' must be a string")
    elif len(instructions) >= MAX_TEXT:
        problems.append(f"{rel}: 'instructions' has {len(instructions)} characters, limit is under {MAX_TEXT}")

    tools: dict[str, set[str]] = {}
    entries = data.get("tools")
    if not isinstance(entries, list) or not entries:
        problems.append(f"{rel}: 'tools' must be a non-empty list")
        return None, problems
    for index, tool in enumerate(entries):
        where = f"tools[{index}]"
        if not isinstance(tool, dict):
            problems.append(f"{rel}: {where} must be an object")
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"{rel}: {where} has no 'name'")
            continue
        where = f"tool '{name}'"
        if name in tools:
            problems.append(f"{rel}: duplicate tool name '{name}'")
            continue
        description = tool.get("description")
        if not isinstance(description, str) or not description:
            problems.append(f"{rel}: {where} has no 'description'")
        elif len(description) >= MAX_TEXT:
            problems.append(f"{rel}: {where} description has {len(description)} characters, limit is under {MAX_TEXT}")
        schema_obj = tool.get("inputSchema")
        props = schema_obj.get("properties") if isinstance(schema_obj, dict) else None
        if not isinstance(props, dict):
            problems.append(f"{rel}: {where} has no 'inputSchema.properties' object")
            props = {}
        for prop, spec in props.items():
            text = spec.get("description") if isinstance(spec, dict) else None
            if isinstance(text, str) and len(text) >= MAX_TEXT:
                problems.append(
                    f"{rel}: {where} property '{prop}' description has {len(text)} characters, "
                    f"limit is under {MAX_TEXT}"
                )
        tools[name] = set(props)
    return (tools or None), problems


# Documents

def find_documents(root: Path):
    """Return (problems, skills, others): SKILL.md paths and the other markdown files to check."""
    problems: list[str] = []
    skills: list[Path] = []
    others: list[Path] = []
    skills_dir = root / PLUGIN_DIR / "skills"
    if skills_dir.is_dir():
        for directory in sorted(skills_dir.iterdir()):
            if not directory.is_dir():
                continue
            names = {child.name for child in directory.iterdir()}
            if "SKILL.md" in names:
                skills.append(directory / "SKILL.md")
            else:
                hint = " (the name is case sensitive)" if any(n.lower() == "skill.md" for n in names) else ""
                problems.append(f"{_rel(root, directory)}: skill directory has no SKILL.md{hint}")
            references = directory / "references"
            if references.is_dir():
                others.extend(sorted(p for p in references.glob("*.md") if p.is_file()))
    commands = root / PLUGIN_DIR / "commands"
    if commands.is_dir():
        others.extend(sorted(p for p in commands.glob("*.md") if p.is_file()))
    return problems, skills, others


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _code_spans(text: str):
    """Yield (offset, content, fenced) for fenced blocks and single-line inline spans."""
    offset = 0
    fence = None  # (char, length)
    block_start = 0
    block: list[str] = []
    for line in text.split("\n"):
        line_end = offset + len(line) + 1
        if fence is None:
            opened = _FENCE.match(line)
            # A backtick fence line cannot hold another backtick: it is inline code
            if opened and not (opened.group(1)[0] == "`" and "`" in line[opened.end():]):
                fence = (opened.group(1)[0], len(opened.group(1)))
                block_start = line_end
                block = []
            else:
                for match in _INLINE_CODE.finditer(line):
                    yield offset + match.start(2), match.group(2), False
        else:
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= fence[1]:
                yield block_start, "\n".join(block), True
                fence = None
            else:
                block.append(line)
        offset = line_end
    if fence is not None:
        yield block_start, "\n".join(block), True


def _split_args(text: str, start: int):
    """Split the argument list that starts at text[start] (after the open paren); None if not closed."""
    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == "\\" and i + 1 < len(text):
                current.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            if depth == 0:
                if ch != ")":
                    return None
                args.append("".join(current))
                return args
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    return None


def _param_name(arg: str):
    arg = arg.strip()
    if arg in ("", "...", chr(0x2026)):
        return None
    cut = re.search(r"[=:]", arg)
    return (arg[: cut.start()] if cut else arg).strip()


def _known(props: set[str]) -> str:
    return ", ".join(sorted(props)) or "none"


def check_references(rel: str, text: str, tools: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    for match in _FULL_NAME.finditer(text):
        if match.group(1) not in tools:
            problems.append(
                f"{rel}:{_line_of(text, match.start())}: '{match.group(1)}' is not a tool of the "
                f"contract (tools: {', '.join(sorted(tools))})"
            )
    for offset, content, fenced in _code_spans(text):
        for match in _CALL.finditer(content):
            tool = match.group(1)
            if tool not in tools:
                continue
            line = _line_of(text, offset + match.start())
            args = _split_args(content, match.end())
            if args is None:
                problems.append(f"{rel}:{line}: call to '{tool}' is not closed (brackets and quotes must balance)")
                continue
            for arg in args:
                name = _param_name(arg)
                if name is not None and name not in tools[tool]:
                    problems.append(
                        f"{rel}:{line}: '{name}' is not a parameter of '{tool}' "
                        f"(parameters: {_known(tools[tool])})"
                    )
        if not fenced:
            ref = _PARAM_REF.fullmatch(content.strip())
            if ref and ref.group(1) in tools and ref.group(2) not in tools[ref.group(1)]:
                problems.append(
                    f"{rel}:{_line_of(text, offset)}: '{ref.group(2)}' is not a parameter of "
                    f"'{ref.group(1)}' (parameters: {_known(tools[ref.group(1)])})"
                )
    return problems


# Frontmatter

class FrontmatterError(Exception):
    def __init__(self, line: int, message: str):
        super().__init__(message)
        self.line = line
        self.message = message


def split_frontmatter(text: str):
    """Return (body lines, line number of the first body line), or None when there is no frontmatter."""
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() in ("---", "..."):
            return lines[1:index], 2
    raise FrontmatterError(1, "frontmatter is not closed (no closing '---' line)")


def _double_quoted(raw: str, line: int) -> str:
    escapes = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "/": "/"}
    out: list[str] = []
    i = 1
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= len(raw) or raw[i + 1] not in escapes:
                raise FrontmatterError(line, "unsupported escape in a double quoted value")
            out.append(escapes[raw[i + 1]])
            i += 2
            continue
        if ch == '"':
            _only_comment_after(raw[i + 1:], line)
            return "".join(out)
        out.append(ch)
        i += 1
    raise FrontmatterError(line, "multi-line quoted values are not supported: close the quote on the same line")


def _single_quoted(raw: str, line: int) -> str:
    out: list[str] = []
    i = 1
    while i < len(raw):
        ch = raw[i]
        if ch == "'":
            if raw[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            _only_comment_after(raw[i + 1:], line)
            return "".join(out)
        out.append(ch)
        i += 1
    raise FrontmatterError(line, "multi-line quoted values are not supported: close the quote on the same line")


def _only_comment_after(rest: str, line: int) -> None:
    rest = rest.strip()
    if rest and not rest.startswith("#"):
        raise FrontmatterError(line, "unexpected text after a quoted value")


def _plain(raw: str, line: int) -> str:
    cut = raw.find(" #")
    value = (raw if cut < 0 else raw[:cut]).strip()
    if ": " in value or value.endswith(":"):
        raise FrontmatterError(
            line, "plain value holds ': ' (YAML reads a nested map): quote the value or use a block scalar"
        )
    return value


def _block_scalar(lines: list[str], index: int, first_line: int, header: re.Match):
    """Return (value, index after the block). lines[index] is the first line after the key."""
    style, chomp = header.group(1), header.group(2)
    block: list[str] = []
    i = index
    while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
        block.append(lines[i])
        i += 1
    content = [ln for ln in block if ln.strip()]
    if not content:
        return "", i
    indent = min(len(ln) - len(ln.lstrip(" ")) for ln in content)
    if any(ln.startswith("\t") for ln in content):
        raise FrontmatterError(first_line + index, "tabs in block scalar indentation are not supported")
    rows = [ln[indent:] if ln.strip() else "" for ln in block]
    while rows and not rows[-1]:
        rows.pop()
    if style == "|":
        value = "\n".join(rows)
    else:
        paragraphs: list[str] = []
        current: list[str] = []
        for row in rows:
            if row:
                current.append(row.strip())
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        value = "\n".join(paragraphs)
    if chomp != "-":
        value += "\n"
    return value, i


def parse_frontmatter(lines: list[str], first_line: int = 2) -> dict[str, tuple[str, int]]:
    """Parse the minimal subset described in the module docstring: key -> (value, line number)."""
    result: dict[str, tuple[str, int]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        number = first_line + i
        if not line.strip() or line.startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            raise FrontmatterError(
                number, "indented line: nested values and multi-line plain values are not supported"
            )
        if line.startswith("- ") or line == "-":
            raise FrontmatterError(number, "lists are not supported")
        match = _KEY_LINE.match(line)
        if not match:
            raise FrontmatterError(number, "expected 'key: value'")
        key, raw = match.group(1), (match.group(2) or "").strip()
        if key in result:
            raise FrontmatterError(number, f"duplicate key '{key}'")
        i += 1
        if not raw:
            if i < len(lines) and lines[i].strip() and lines[i][0] in " \t":
                raise FrontmatterError(number, f"'{key}' has a nested value, which is not supported")
            result[key] = ("", number)
        elif raw[0] in "|>":
            header = _BLOCK_HEADER.match(raw)
            if not header:
                raise FrontmatterError(number, "unsupported block scalar header (indentation indicators)")
            value, i = _block_scalar(lines, i, first_line, header)
            result[key] = (value, number)
        elif raw[0] == '"':
            result[key] = (_double_quoted(raw, number), number)
        elif raw[0] == "'":
            result[key] = (_single_quoted(raw, number), number)
        elif raw[0] in "[{&*!%@`":
            raise FrontmatterError(
                number, "flow collections, anchors, aliases and tags are not supported: quote the value"
            )
        else:
            result[key] = (_plain(raw, number), number)
    return result


def check_frontmatter(rel: str, text: str, skill_name: str) -> list[str]:
    try:
        parts = split_frontmatter(text)
        if parts is None:
            return [f"{rel}:1: SKILL.md must start with a '---' frontmatter block"]
        fields = parse_frontmatter(*parts)
    except FrontmatterError as exc:
        return [f"{rel}:{exc.line}: {exc.message}"]
    problems: list[str] = []
    for key, (_, number) in fields.items():
        if key not in ("name", "description"):
            problems.append(f"{rel}:{number}: frontmatter key '{key}' is not allowed (only name and description)")
    for key in ("name", "description"):
        if key not in fields:
            problems.append(f"{rel}:1: frontmatter lacks '{key}'")
    if "name" in fields and fields["name"][0].strip() != skill_name:
        problems.append(
            f"{rel}:{fields['name'][1]}: name '{fields['name'][0].strip()}' must equal the "
            f"directory name '{skill_name}'"
        )
    if "description" in fields:
        description, number = fields["description"][0].strip(), fields["description"][1]
        if not description:
            problems.append(f"{rel}:{number}: description is empty")
        elif len(description) > MAX_SKILL_DESCRIPTION:
            problems.append(
                f"{rel}:{number}: description has {len(description)} characters, limit is {MAX_SKILL_DESCRIPTION}"
            )
    return problems


# Entry points

def _read_markdown(root: Path, path: Path, problems: list[str]):
    rel = _rel(root, path)
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        problems.append(f"{rel}: not valid UTF-8")
    except OSError as exc:
        problems.append(f"{rel}: cannot read file ({type(exc).__name__})")
    return None


def check(root: Path, contract_path: Path | None = None) -> list[str]:
    root = Path(root)
    contract_file = root / (contract_path if contract_path is not None else DEFAULT_CONTRACT)
    tools, problems = load_contract(root, contract_file)
    if tools is None:
        return problems
    doc_problems, skills, others = find_documents(root)
    problems.extend(doc_problems)
    for path in skills + others:
        text = _read_markdown(root, path, problems)
        if text is None:
            continue
        rel = _rel(root, path)
        if path in skills:
            problems.extend(check_frontmatter(rel, text, path.parent.name))
        problems.extend(check_references(rel, text, tools))
    return problems


def _emit(text: str, stream) -> None:
    # Paths can hold characters a Windows console cannot print
    print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the tool contract and the skills against it.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this repo)")
    parser.add_argument(
        "--contract", type=Path, default=None, help="contract file, relative to --root (default: contract/mcp_tools.json)"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.root.is_dir():
        _emit(f"check_contract: not a directory: {args.root}", sys.stderr)
        return 2
    problems = check(args.root, args.contract)
    for problem in problems:
        _emit(problem, sys.stderr)
    if problems:
        _emit(f"check_contract: {len(problems)} problem(s)", sys.stderr)
        return 1
    _, skills, others = find_documents(args.root)
    if skills or others:
        _emit(f"check_contract: ok ({len(skills)} skill(s), {len(others)} other document(s) checked)", sys.stdout)
    else:
        _emit("check_contract: ok (contract only: there are no skills to check)", sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
