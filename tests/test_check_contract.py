"""Tests for scripts/check_contract.py (stdlib unittest)."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_contract  # noqa: E402

CONTRACT = {
    "schema": 1,
    "instructions": "Use the tools.",
    "tools": [
        {
            "name": "cerca",
            "title": "Cerca",
            "description": "Search.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "domanda": {"type": "string", "description": "Question."},
                    "varianti": {"type": "array"},
                    "fonti": {"type": "array"},
                    "modalita": {"type": "string"},
                    "max_risultati": {"type": "integer"},
                },
                "required": ["domanda"],
            },
        },
        {
            "name": "elenca_fonti",
            "description": "List.",
            "inputSchema": {"type": "object", "properties": {"filtro": {"type": "string"}}},
        },
        {
            "name": "recupera_documento",
            "description": "Read.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id_documento": {"type": "string"},
                    "offset": {"type": "integer"},
                    "max_caratteri": {"type": "integer"},
                    "pagina": {"type": "integer"},
                },
            },
        },
    ],
}

SKILL_OK = """---
name: demo
description: Use when the user asks about documents.
---

# Demo

Call `mcp__plugin_kai_kai__elenca_fonti` first, then `cerca(domanda, fonti, varianti=[...])`.
Pass `cerca.fonti` as a list and read with `recupera_documento(id_documento, offset=0, ...)`.
"""


def _force_rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def write(root: Path, rel: str, content: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


class ContractCase(unittest.TestCase):
    def tree(self, contract=CONTRACT) -> Path:
        root = Path(tempfile.mkdtemp(prefix="kai-contract-"))
        self.addCleanup(_force_rmtree, root)
        write(root, "contract/mcp_tools.json", json.dumps(contract, indent=2))
        return root

    def skill(self, root: Path, text: str, name: str = "demo") -> Path:
        return write(root, f"plugins/kai/skills/{name}/SKILL.md", text)

    def problems_for(self, text: str, name: str = "demo") -> list[str]:
        root = self.tree()
        self.skill(root, text, name)
        return check_contract.check(root)

    def assertProblem(self, problems: list[str], *needles: str) -> None:
        self.assertTrue(
            any(all(n in p for n in needles) for p in problems),
            f"no problem with {needles!r} in {problems!r}",
        )

    def mutated(self, fn) -> dict:
        data = copy.deepcopy(CONTRACT)
        fn(data)
        return data


class ContractShapeTests(ContractCase):
    def test_valid_contract_without_skills_passes(self):
        self.assertEqual(check_contract.check(self.tree()), [])

    def test_the_real_repo_passes(self):
        self.assertEqual(check_contract.check(REPO_ROOT), [])

    def test_missing_file(self):
        root = Path(tempfile.mkdtemp(prefix="kai-contract-"))
        self.addCleanup(_force_rmtree, root)
        self.assertProblem(check_contract.check(root), "contract/mcp_tools.json", "missing")

    def test_invalid_json_reports_the_line(self):
        root = self.tree()
        write(root, "contract/mcp_tools.json", '{\n  "schema": 1,\n  oops\n}\n')
        self.assertProblem(check_contract.check(root), "contract/mcp_tools.json:3", "invalid JSON")

    def test_not_an_object(self):
        root = self.tree()
        write(root, "contract/mcp_tools.json", "[]")
        self.assertProblem(check_contract.check(root), "JSON object")

    def test_schema_must_be_one(self):
        for value in (2, "1", True, None):
            with self.subTest(value=value):
                root = self.tree(self.mutated(lambda d, v=value: d.update(schema=v)))
                self.assertProblem(check_contract.check(root), "'schema' must be 1")

    def test_instructions_rules(self):
        root = self.tree(self.mutated(lambda d: d.update(instructions=5)))
        self.assertProblem(check_contract.check(root), "'instructions' must be a string")
        root = self.tree(self.mutated(lambda d: d.update(instructions="x" * 4095)))
        self.assertEqual(check_contract.check(root), [])
        root = self.tree(self.mutated(lambda d: d.update(instructions="x" * 4096)))
        self.assertProblem(check_contract.check(root), "'instructions' has 4096 characters")

    def test_tool_description_limit(self):
        root = self.tree(self.mutated(lambda d: d["tools"][0].update(description="x" * 4095)))
        self.assertEqual(check_contract.check(root), [])
        root = self.tree(self.mutated(lambda d: d["tools"][0].update(description="x" * 4096)))
        self.assertProblem(check_contract.check(root), "tool 'cerca' description has 4096")

    def test_property_description_limit(self):
        def set_len(n):
            return lambda d: d["tools"][0]["inputSchema"]["properties"]["domanda"].update(description="x" * n)

        self.assertEqual(check_contract.check(self.tree(self.mutated(set_len(4095)))), [])
        self.assertProblem(check_contract.check(self.tree(self.mutated(set_len(4096)))), "property 'domanda'")

    def test_tools_must_be_a_non_empty_list(self):
        for value in ([], None, "x"):
            with self.subTest(value=value):
                root = self.tree(self.mutated(lambda d, v=value: d.update(tools=v)))
                self.assertProblem(check_contract.check(root), "'tools' must be a non-empty list")

    def test_tool_fields(self):
        cases = {
            "name": (lambda d: d["tools"][0].pop("name"), "no 'name'"),
            "description": (lambda d: d["tools"][0].pop("description"), "no 'description'"),
            "inputSchema": (lambda d: d["tools"][0].pop("inputSchema"), "inputSchema.properties"),
            "properties": (lambda d: d["tools"][0]["inputSchema"].pop("properties"), "inputSchema.properties"),
            "properties type": (lambda d: d["tools"][0]["inputSchema"].update(properties=[]), "inputSchema.properties"),
            "tool type": (lambda d: d["tools"].__setitem__(0, "x"), "must be an object"),
        }
        for label, (fn, needle) in cases.items():
            with self.subTest(label=label):
                root = self.tree(self.mutated(fn))
                self.assertProblem(check_contract.check(root), needle)

    def test_duplicate_tool_names(self):
        root = self.tree(self.mutated(lambda d: d["tools"].append(copy.deepcopy(d["tools"][0]))))
        self.assertProblem(check_contract.check(root), "duplicate tool name 'cerca'")

    def test_unusable_contract_skips_the_skill_checks(self):
        root = self.tree(self.mutated(lambda d: d.update(tools=[])))
        self.skill(root, "no frontmatter and `cerca(zzz)`\n")
        problems = check_contract.check(root)
        self.assertEqual(len(problems), 1)

    def test_custom_contract_path_is_relative_to_the_root(self):
        root = self.tree()
        write(root, "other.json", json.dumps(self.mutated(lambda d: d.update(schema=9))))
        self.assertEqual(check_contract.check(root), [])
        self.assertProblem(check_contract.check(root, Path("other.json")), "other.json", "'schema' must be 1")

    def test_vendored_real_contract_has_the_three_tools(self):
        data = json.loads((REPO_ROOT / "contract" / "mcp_tools.json").read_text(encoding="utf-8"))
        self.assertEqual({t["name"] for t in data["tools"]}, {"cerca", "elenca_fonti", "recupera_documento"})


class SkillReferenceTests(ContractCase):
    def test_valid_skill_passes(self):
        self.assertEqual(self.problems_for(SKILL_OK), [])

    def test_unknown_full_tool_name_fails(self):
        problems = self.problems_for(SKILL_OK + "\nUse mcp__plugin_kai_kai__inesistente now.\n")
        self.assertProblem(problems, "SKILL.md:", "'inesistente' is not a tool of the contract")

    def test_full_tool_names_of_the_contract_pass(self):
        text = SKILL_OK + "\nmcp__plugin_kai_kai__cerca mcp__plugin_kai_kai__recupera_documento\n"
        self.assertEqual(self.problems_for(text), [])

    def test_wildcard_full_name_is_ignored(self):
        self.assertEqual(self.problems_for(SKILL_OK + "\nallow mcp__plugin_kai_kai__* here\n"), [])

    def test_unknown_call_parameter_fails(self):
        problems = self.problems_for(SKILL_OK + "\nSee `cerca(domanda, fonti, inesistente)`.\n")
        self.assertProblem(problems, "'inesistente' is not a parameter of 'cerca'")

    def test_the_line_number_points_at_the_call(self):
        problems = self.problems_for(SKILL_OK + "\nSee `cerca(inesistente)`.\n")
        line = SKILL_OK.count("\n") + 2
        self.assertProblem(problems, f"SKILL.md:{line}:")

    def test_call_notation_forms(self):
        good = [
            "`cerca(domanda)`",
            "`cerca(...)`",
            "`cerca()`",
            "`cerca(domanda, " + chr(0x2026) + ")`",
            "`cerca(domanda: str, max_risultati: int)`",
            "`cerca(domanda, modalita=\"solo_fonti_indicate\", fonti=[\"a, b\", 'c)'])`",
            "`cerca(domanda, varianti=[\"x\", \"y\"],)`",
            "`cerca(domanda=\"a, inesistente=1\")`",
            "`elenca_fonti(filtro)` and `elenca_fonti()`",
            "`mcp__plugin_kai_kai__cerca(domanda)`",
            "``cerca(domanda)``",
            "`len(inesistente)` and `ricerca(inesistente)` and `x_cerca(inesistente)` and `obj.cerca(inesistente)`",
            "Plain text cerca(inesistente) outside code is not checked.",
        ]
        for text in good:
            with self.subTest(text=text):
                self.assertEqual(self.problems_for(SKILL_OK + "\n" + text + "\n"), [])

    def test_bad_call_notation_forms(self):
        bad = {
            "`cerca(domanda, [fonti])`": "'[fonti]'",
            "`cerca(\"testo\")`": "'\"testo\"'",
            "`cerca(domanda=\"x\", fonti=[1], nope: int)`": "'nope'",
            "`recupera_documento(id_documento, pagina=1, inesistente=2)`": "'inesistente'",
            "`mcp__plugin_kai_kai__cerca(inesistente)`": "'inesistente'",
            "`elenca_fonti(domanda)`": "'domanda' is not a parameter of 'elenca_fonti'",
        }
        for text, needle in bad.items():
            with self.subTest(text=text):
                self.assertProblem(self.problems_for(SKILL_OK + "\n" + text + "\n"), needle)

    def test_unclosed_call_fails(self):
        for text in ("`cerca(domanda, fonti`", "`cerca(domanda, [fonti)`", "`cerca(domanda, \"x)`"):
            with self.subTest(text=text):
                self.assertProblem(self.problems_for(SKILL_OK + "\n" + text + "\n"), "is not closed")

    def test_fenced_block_calls_are_checked_across_lines(self):
        block = "```text\ncerca(\n  domanda=\"x\",\n  inesistente=1,\n)\n```\n"
        problems = self.problems_for(SKILL_OK + "\n" + block)
        self.assertProblem(problems, "'inesistente' is not a parameter of 'cerca'")
        ok = "```\ncerca(\n  domanda=\"x\",\n  fonti=[\"a\"],\n)\n```\n~~~\nelenca_fonti(filtro)\n~~~\n"
        self.assertEqual(self.problems_for(SKILL_OK + "\n" + ok), [])

    def test_text_after_a_fence_is_not_code(self):
        text = SKILL_OK + "\n```\nplain\n```\n\nThen cerca(inesistente) in prose.\n"
        self.assertEqual(self.problems_for(text), [])

    def test_param_reference(self):
        for text in ("`cerca.fonti`", "`elenca_fonti.filtro`", "`mcp__plugin_kai_kai__cerca.domanda`", "`riepilogo.fonti`", "`file.json`"):
            with self.subTest(text=text):
                self.assertEqual(self.problems_for(SKILL_OK + "\n" + text + "\n"), [])
        for text in ("`cerca.inesistente`", "`recupera_documento.fonti`", "`mcp__plugin_kai_kai__cerca.nope`"):
            with self.subTest(text=text):
                self.assertProblem(
                    self.problems_for(SKILL_OK + "\n" + text + "\n"), "is not a parameter of"
                )

    def test_param_reference_only_counts_for_the_whole_span(self):
        self.assertEqual(self.problems_for(SKILL_OK + "\nUse `cerca.fonti and more` here.\n"), [])

    def test_references_and_commands_are_checked_too(self):
        for rel in ("plugins/kai/skills/demo/references/guide.md", "plugins/kai/commands/go.md"):
            with self.subTest(rel=rel):
                root = self.tree()
                self.skill(root, SKILL_OK)
                write(root, rel, "Use `cerca(inesistente)`.\n")
                self.assertProblem(check_contract.check(root), rel, "'inesistente'")
                write(root, rel, "Use `cerca(domanda)` and mcp__plugin_kai_kai__nope.\n")
                self.assertProblem(check_contract.check(root), rel, "'nope' is not a tool")

    def test_references_and_commands_have_no_frontmatter_rules(self):
        root = self.tree()
        self.skill(root, SKILL_OK)
        write(root, "plugins/kai/skills/demo/references/guide.md", "No frontmatter here.\n")
        write(root, "plugins/kai/commands/go.md", "---\nallowed-tools: x\ndescription: y\n---\nBody.\n")
        self.assertEqual(check_contract.check(root), [])

    def test_only_direct_references_are_read(self):
        root = self.tree()
        self.skill(root, SKILL_OK)
        write(root, "plugins/kai/skills/demo/other/guide.md", "Use `cerca(inesistente)`.\n")
        self.assertEqual(check_contract.check(root), [])


class FrontmatterTests(ContractCase):
    def fm(self, body: str, name: str = "demo") -> list[str]:
        return self.problems_for(f"---\n{body}---\n\nBody.\n", name)

    def test_extra_key_fails(self):
        problems = self.fm("name: demo\ndescription: ok\nallowed-tools: x\n")
        self.assertProblem(problems, "SKILL.md:4:", "'allowed-tools' is not allowed")

    def test_missing_keys_fail(self):
        self.assertProblem(self.fm("name: demo\n"), "lacks 'description'")
        self.assertProblem(self.fm("description: ok\n"), "lacks 'name'")

    def test_name_must_match_the_directory(self):
        self.assertProblem(self.fm("name: other\ndescription: ok\n"), "name 'other' must equal the directory name 'demo'")
        self.assertEqual(self.fm("name: other\ndescription: ok\n", name="other"), [])

    def test_description_length(self):
        self.assertEqual(self.fm("name: demo\ndescription: " + "x" * 1536 + "\n"), [])
        self.assertProblem(
            self.fm("name: demo\ndescription: " + "x" * 1537 + "\n"), "description has 1537 characters"
        )

    def test_description_must_not_be_empty(self):
        for body in ("name: demo\ndescription:\n", "name: demo\ndescription: ''\n", "name: demo\ndescription: >\n"):
            with self.subTest(body=body):
                self.assertProblem(self.fm(body), "description is empty")

    def test_block_scalar_description_parses(self):
        body = "name: demo\ndescription: >\n  First line of the text,\n  second line.\n\n  Second paragraph.\n"
        self.assertEqual(self.fm(body), [])
        long_block = "name: demo\ndescription: >-\n" + "".join(f"  {'w' * 99}\n" for _ in range(16))
        self.assertProblem(self.fm(long_block), "description has 1599 characters")

    def test_block_scalar_values(self):
        lines = ["name: demo", "description: |", "  one", "  two", "", "next: x"]
        fields = check_contract.parse_frontmatter(lines)
        self.assertEqual(fields["description"][0], "one\ntwo\n")
        self.assertEqual(fields["next"][0], "x")
        lines = ["description: >-", "  a", "  b", "", "  c"]
        self.assertEqual(check_contract.parse_frontmatter(lines)["description"][0], "a b\nc")
        lines = ["description: |+", "  a"]
        self.assertEqual(check_contract.parse_frontmatter(lines)["description"][0], "a\n")

    def test_quoted_values(self):
        fields = check_contract.parse_frontmatter([
            'a: "double: with colon # and hash"',
            "b: 'single ''quoted'' text'",
            'c: "tab\\tnewline\\n quote\\" back\\\\" # comment',
            "d: plain value # comment",
            "name: demo",
        ])
        self.assertEqual(fields["a"][0], "double: with colon # and hash")
        self.assertEqual(fields["b"][0], "single 'quoted' text")
        self.assertEqual(fields["c"][0], 'tab\tnewline\n quote" back\\')
        self.assertEqual(fields["d"][0], "plain value")
        self.assertEqual(self.fm('name: "demo"\ndescription: "Use when: asked"\n'), [])

    def test_line_numbers_follow_the_file(self):
        fields = check_contract.parse_frontmatter(["# comment", "", "name: demo", "description: x"], first_line=2)
        self.assertEqual((fields["name"][1], fields["description"][1]), (4, 5))

    def test_unsupported_constructs_say_so(self):
        cases = {
            "name: demo\ndescription: Use when: asked\n": "plain value holds ': '",
            "name: demo\ndescription: text:\n": "plain value holds ': '",
            "name: demo\ndescription: [a, b]\n": "not supported",
            "name: demo\ndescription: &anchor x\n": "not supported",
            "name: demo\ndescription: *alias\n": "not supported",
            "name: demo\ndescription: !!str x\n": "not supported",
            "name: demo\ndescription:\n  nested: x\n": "nested value",
            "name: demo\ndescription: first\n  second\n": "indented line",
            "name: demo\n- a\n": "lists are not supported",
            "name: demo\ndescription: >2\n  x\n": "indentation indicators",
            "name: demo\ndescription: \"open\n": "multi-line quoted",
            "name: demo\ndescription: 'open\n": "multi-line quoted",
            'name: demo\ndescription: "a" tail\n': "unexpected text after a quoted value",
            'name: demo\ndescription: "bad \\q escape"\n': "unsupported escape",
            "name: demo\nname: again\n": "duplicate key 'name'",
            "name demo\n": "expected 'key: value'",
            "name:demo\n": "expected 'key: value'",
            "name: demo\ndescription: |\n\tx\n": "tabs",
        }
        for body, needle in cases.items():
            with self.subTest(body=body):
                self.assertProblem(self.fm(body), "SKILL.md:", needle)

    def test_missing_frontmatter(self):
        self.assertProblem(self.problems_for("# Title\n"), "SKILL.md:1:", "frontmatter")
        self.assertProblem(self.problems_for(""), "SKILL.md:1:", "frontmatter")

    def test_unclosed_frontmatter(self):
        self.assertProblem(self.problems_for("---\nname: demo\n"), "not closed")

    def test_byte_order_mark_is_accepted(self):
        root = self.tree()
        path = root / "plugins/kai/skills/demo/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xef\xbb\xbf" + SKILL_OK.encode("utf-8"))
        self.assertEqual(check_contract.check(root), [])

    def test_invalid_utf8(self):
        root = self.tree()
        path = root / "plugins/kai/skills/demo/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"---\nname: demo\ndescription: \xff\xfe\n---\n")
        self.assertProblem(check_contract.check(root), "SKILL.md", "not valid UTF-8")


class SkillDiscoveryTests(ContractCase):
    def test_skill_directory_without_skill_md(self):
        root = self.tree()
        write(root, "plugins/kai/skills/empty/notes.md", "x")
        self.assertProblem(check_contract.check(root), "plugins/kai/skills/empty", "no SKILL.md")

    def test_wrong_case_skill_md_is_reported(self):
        root = self.tree()
        write(root, "plugins/kai/skills/demo/skill.md", SKILL_OK)
        self.assertProblem(check_contract.check(root), "plugins/kai/skills/demo", "no SKILL.md", "case sensitive")

    def test_several_skills_and_stray_files(self):
        root = self.tree()
        self.skill(root, SKILL_OK, "demo")
        self.skill(root, SKILL_OK.replace("name: demo", "name: second"), "second")
        write(root, "plugins/kai/skills/README.md", "Use `cerca(inesistente)` in a stray file.\n")
        self.assertEqual(check_contract.check(root), [])
        self.skill(root, SKILL_OK.replace("name: demo", "name: wrong"), "third")
        self.assertProblem(check_contract.check(root), "skills/third/SKILL.md", "must equal the directory name 'third'")

    def test_no_skills_directory_passes(self):
        root = self.tree()
        self.assertEqual(check_contract.find_documents(root), ([], [], []))


class CliTests(ContractCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_contract.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_skills_is_reported_and_exits_zero(self):
        code, out, err = self.run_main(["--root", str(self.tree())])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("no skills to check", out)

    def test_skills_are_counted(self):
        root = self.tree()
        self.skill(root, SKILL_OK)
        code, out, _ = self.run_main(["--root", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("1 skill(s)", out)

    def test_problems_exit_one_and_are_printed(self):
        root = self.tree()
        self.skill(root, SKILL_OK + "\n`cerca(inesistente)`\n")
        code, _, err = self.run_main(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("SKILL.md:", err)
        self.assertIn("problem(s)", err)

    def test_contract_option(self):
        root = self.tree()
        write(root, "alt.json", json.dumps(self.mutated(lambda d: d.update(schema=2))))
        self.assertEqual(self.run_main(["--root", str(root), "--contract", "alt.json"])[0], 1)
        self.assertEqual(self.run_main(["--root", str(root)])[0], 0)

    def test_bad_arguments_exit_two(self):
        self.assertEqual(self.run_main(["--bogus"])[0], 2)
        missing = Path(tempfile.gettempdir()) / "kai-contract-does-not-exist-xyz"
        self.assertEqual(self.run_main(["--root", str(missing)])[0], 2)

    def test_the_cli_runs_as_a_script(self):
        root = self.tree()
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_contract.py"), "--root", str(root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
