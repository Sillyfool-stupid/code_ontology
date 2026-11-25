"""Real C/C++ grammars; model integration uses only a local synthetic API."""
import io
import os
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_ontology.cli import main
from code_ontology.config import Settings, load_policy
from code_ontology.parsing import parse_repository, select_backend
from code_ontology.prepare import prepare, evidence_valid
from code_ontology.repository import snapshot
from code_ontology.util import iter_jsonl, read_json
from tests.fake_api import fake_server


class TreeSitterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tree 中文 ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "代码"
        self.repo.mkdir()
        self.policy = load_policy()
        self.policy["parser_backend"] = "tree-sitter"
        (self.repo / "device.cpp").write_text(
            '// 温度\nnamespace plant {\nstruct Device {\n int check(int t);\n'
            ' int run(int t) { if (t > 85) return 1; return 0; }\n};\n'
            'int Device::check(int t) { return run(t); }\n}\n'
            'int (*callback)(int);\nint global_limit = 85;\n', encoding="utf-8")
        (self.repo / "sensor.c").write_text('int sense(void) { return 1; }\n', encoding="utf-8")
        (self.repo / "limits.h").write_text('#define LIMIT 85\nint threshold = LIMIT;\n', encoding="utf-8")
        self.env = self.root / ".env"
        self.env.write_text("", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def parse(self):
        dest = snapshot(self.repo, self.root / "out", self.policy)
        parse_repository(dest, Settings(), self.policy, ["ast"])
        return dest

    def test_real_ast_symbols_provenance_and_cache(self):
        dest = self.parse()
        symbols = list(iter_jsonl(dest / "normalized/symbols.jsonl"))
        names = [s["NAME"] for s in symbols]
        self.assertEqual(names.count("run"), 1)
        self.assertEqual(names.count("check"), 1)
        self.assertIn("Device::check", names)
        self.assertNotIn("callback", names)
        self.assertIn("plant::Device::run", [s["FULL_NAME"] for s in symbols])
        nodes = list(iter_jsonl(dest / "normalized/ast/nodes.jsonl"))
        edges = list(iter_jsonl(dest / "normalized/ast/edges.jsonl"))
        self.assertTrue(edges)
        self.assertEqual({e["label"] for e in edges}, {"AST"})
        self.assertTrue(all(e["src"] in {n["id"] for n in nodes} for e in edges))
        packs = list(iter_jsonl(prepare(dest, "ast", self.policy)))
        self.assertTrue(all(evidence_valid(dest, p) for p in packs))
        self.assertTrue(all(p["evidence_layers"]["capabilities"]["backend"] == "tree-sitter" for p in packs))
        self.assertTrue(all(p["evidence_layers"]["capabilities"]["ast"] for p in packs))
        self.assertTrue(all(not p["evidence_layers"]["capabilities"]["cfg"] for p in packs))
        self.assertTrue(all(not p["evidence_layers"]["capabilities"]["cpg"] for p in packs))
        index = read_json(dest / "prepared/ast/index.json")
        self.assertEqual(index["format"], 2)
        self.assertEqual(sum(index["layer_coverage"].values()), len(packs))
        self.assertIn("limits.h", {r["file"] for p in packs for r in p["source_refs"]})
        self.assertTrue(any("global_limit" in p["code"] for p in packs))
        self.assertTrue(all(any("syntax AST only" in w for w in p["parse_warnings"]) for p in packs))
        before = (dest / "raw/tree-sitter/nodes.jsonl").stat().st_mtime_ns
        parse_repository(dest, Settings(), self.policy, ["ast"])
        self.assertEqual(before, (dest / "raw/tree-sitter/nodes.jsonl").stat().st_mtime_ns)
        self.assertTrue(read_json(dest / "manifest.json")["stages"]["parse"]["cached"])
        with self.assertRaisesRegex(ValueError, "supports"):
            prepare(dest, "cpg", self.policy)
        self.assertFalse((dest / "normalized/cpg").exists())

    def test_auto_selection_and_distinct_snapshot(self):
        with patch("code_ontology.parsing.executable", side_effect=ValueError("missing")):
            self.assertEqual(select_backend({"parser_backend": "auto"}, Settings())["backend"], "tree-sitter")
        with patch("code_ontology.parsing.executable", return_value=Path("joern")):
            self.assertEqual(select_backend({"parser_backend": "auto"}, Settings())["backend"], "joern")
        first = snapshot(self.repo, self.root / "out", self.policy)
        joern = {**self.policy, "parser_backend": "joern"}
        self.assertNotEqual(first, snapshot(self.repo, self.root / "out", joern))
        with patch("code_ontology.parsing.executable", side_effect=ValueError("missing")), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["parse", "--repo", str(self.repo), "--out", str(self.root / "auto"),
                                   "--env-file", str(self.env)]), 0)
            auto_dest = next((self.root / "auto").iterdir())
            self.assertEqual(read_json(auto_dest / "manifest.json")["parser"]["backend"], "tree-sitter")

    def test_syntax_errors_and_missing_dependency_failure(self):
        (self.repo / "broken.cpp").write_text('int broken( { return;\n', encoding="utf-8")
        dest = self.parse()
        report = read_json(dest / "raw/tree-sitter/parse_report.json")
        self.assertGreater(report["syntax_errors"], 0)
        with patch("code_ontology.treesitter.parsers", side_effect=RuntimeError("missing grammar")):
            with self.assertRaisesRegex(RuntimeError, "missing grammar"):
                parse_repository(dest, Settings(), self.policy, ["ast"], force=True)
        self.assertEqual(read_json(dest / "manifest.json")["stages"]["parse"]["state"], "failed")

    def test_cli_ast_end_to_end_and_resume_without_joern(self):
        with fake_server() as (url, transport), patch.dict(os.environ, {
            "OPENAI_BASE_URL": url, "OPENAI_API_KEY": "fixture-only", "OPENAI_MODEL": "fixture-only",
            "LLM_MAX_RETRIES": "0", "JOERN_HOME": ""}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            args = ["experiment", "--repo", str(self.repo), "--out", str(self.root / "out"),
                    "--env-file", str(self.env), "--parser-backend", "tree-sitter", "--experiment", "fixture-ts"]
            self.assertEqual(main(args), 0)
            dest = next((self.root / "out").iterdir())
            summary = read_json(dest / "runs/fixture-ts/comparison.json")
            self.assertEqual(len(summary["runs"]), 2)
            self.assertEqual(summary["missing"], [])
            for profile in ("general", "maintenance"):
                self.assertEqual(main(["validate", "--snapshot", str(dest), "--view", "ast", "--run-dir",
                                       str(dest / "runs/fixture-ts/ast" / profile), "--env-file", str(self.env)]), 0)
            before = transport["requests"]
            self.assertEqual(main(args), 0)
            self.assertEqual(transport["requests"], before)
            self.assertEqual(main(["doctor", "--no-llm", "--parser-backend", "tree-sitter", "--out",
                                   str(self.root / "doctor"), "--env-file", str(self.env)]), 0)


if __name__ == "__main__":
    unittest.main()
