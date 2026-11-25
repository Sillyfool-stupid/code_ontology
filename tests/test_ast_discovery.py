from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from code_ontology import ast_discovery
from code_ontology.config import load_policy, Settings
from code_ontology.discovery import payload as old_payload
from code_ontology.parsing import parse_repository
from code_ontology.pipeline import Pipeline
from code_ontology.prepare import prepare
from code_ontology.repository import snapshot
from code_ontology.stages import SchemaStages, IncompleteStage
from code_ontology.util import canonical, digest, read_json, write_json
from tests.fake_api import fake_server


def pack(identity, code, start=1, file="device.cpp", sha="source"):
    return {"pack_id": identity, "view": "ast", "code": code, "evidence_ids": ["ev_" + identity],
            "source_refs": [{"file": file, "start_line": start,
                             "end_line": start + len(code.splitlines()) - 1,
                             "source_sha256": sha, "snippet_hash": digest(code, 64)}],
            "context": {"nodes": [], "edges": []}, "source_scope": "code_definition"}


def restore(value, shared):
    if isinstance(value, str):
        return value
    if "shared_ref" in value:
        return shared[value["shared_ref"]]
    return "".join(restore(piece, shared) for piece in value["segments"])


class AstDiscoveryTests(unittest.TestCase):
    def test_overlap_sharing_round_trip_and_input_unchanged(self):
        body = "".join(f"  if (reading > {i}) alarm({i});\r\n" for i in range(80))
        packs = [pack("type", "struct Device {\r\n" + body + "};\r\n"), pack("method", body, 2)]
        before = deepcopy(packs)
        encoded = ast_discovery.payload(packs, "maintenance")
        self.assertLess(len(canonical(encoded)), len(canonical(old_payload(packs, "maintenance", True))))
        self.assertEqual(encoded["ast_01_format"], 1)
        for original, row in zip(packs, encoded["packs"]):
            self.assertEqual(restore(row["code"], encoded.get("shared", {})), original["code"])
            self.assertEqual({k: v for k, v in row.items() if k != "code"},
                             {k: v for k, v in original.items() if k != "code"})
        self.assertEqual(packs, before)

    def test_crossing_ranges_and_identical_nodes_are_reversible(self):
        lines = [f"/* {'x' * 120} {i} */\n" for i in range(30)]
        packs = [pack("left", "".join(lines[:20])), pack("right", "".join(lines[10:]), 11),
                 pack("middle", "".join(lines[12:18]), 13)]
        for p in packs:
            p["context"]["nodes"] = [{"id": "node", "CODE": "n" * 400}]
        encoded = ast_discovery.payload(packs, "general")
        for original, row in zip(packs, encoded["packs"]):
            self.assertEqual(restore(row["code"], encoded.get("shared", {})), original["code"])
            node = row["context"]["nodes"][0]
            self.assertEqual(encoded["shared"][node["shared_ref"]] if "shared_ref" in node else node,
                             original["context"]["nodes"][0])

    def test_metadata_sharing_restores_all_fields_without_mutation(self):
        packs = [pack("a", "a();\n"), pack("b", "b();\n", 2)]
        for p in packs:
            p["parse_warnings"] = ["syntax warning " * 100]
            p["evidence_layers"] = {"capabilities": {"note": "AST only " * 100}}
            p["context"]["edges"] = [{"src": str(i), "dst": str(i + 1), "label": "AST"} for i in range(30)]
        original = deepcopy(packs)
        encoded = ast_discovery.payload(packs, "general")
        self.assertEqual(ast_discovery.expand(encoded["packs"], encoded["shared"]), original)
        self.assertEqual(packs, original)
        self.assertLess(len(canonical(encoded)), len(canonical(old_payload(packs, "general", True))))

    def test_different_locations_conflicts_and_fragments_do_not_share_ranges(self):
        large = "value++;\n" * 100
        base = pack("type", "struct A {\n" + large + "};\n")
        variants = [pack("other-file", large, 2, file="other.cpp"),
                    pack("other-sha", large, 2, sha="changed"),
                    pack("conflict", large.replace("value", "other"), 2),
                    {**pack("fragment", large, 2), "fragment_of": "original"},
                    {**pack("bad-range", large, 2), "source_refs": [{**pack("x", large)["source_refs"][0], "snippet_hash": "bad"}]}]
        for variant in variants:
            encoded = ast_discovery.payload([base, variant], "general")
            self.assertTrue(all(not isinstance(p["code"], dict) or "segments" not in p["code"]
                                for p in encoded["packs"]))

    def test_batches_keep_all_packs_respect_budget_and_file_boundary(self):
        packs = [pack("last", "c();\n", 20), pack("first", "a();\n"),
                 pack("middle", "b();\n", 10), pack("other", "a();\n", file="other/other.cpp")]
        class Budget:
            def fits(self, messages):
                return len(json.loads(messages[1]["content"])["packs"]) <= 2
        groups = ast_discovery.batches(packs, 8, "system", Budget(), "general")
        self.assertEqual([[p["pack_id"] for p in g] for g in groups], [["first", "middle"], ["last"], ["other"]])
        report = ast_discovery.input_report(groups, "general")
        self.assertEqual(report["selected_packs"], 4)
        self.assertEqual(report["request_json_bytes"], sum(len(canonical(ast_discovery.payload(g, "general")).encode("utf-8")) for g in groups))

    def test_opt_in_pipeline_cache_merge_and_original_strategy(self):
        with tempfile.TemporaryDirectory() as directory, fake_server() as (url, state), redirect_stdout(io.StringIO()):
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            (repo / "device.cpp").write_text("struct Device {\n int run(int t) {\n if (t > 85) return 1;\n return 0;\n }\n};\n", encoding="utf-8")
            env = root / "empty.env"
            env.write_text("", encoding="utf-8")
            policy = load_policy(env_file=str(env))
            policy.update(parser_backend="tree-sitter", discovery_max_packs=0, discovery_strategy="ast-01", batch_packs=8)
            settings = Settings(base_url=url, model="fixture-only", retries=0, context_tokens=32768)
            dest = snapshot(repo, root / "out", policy)
            parse_repository(dest, settings, policy, ["ast"])
            prepare(dest, "ast", policy)
            p = Pipeline(dest, "ast", "general", "AST-01-test", settings, policy)
            prepared = (dest / "prepared/ast/packs.jsonl").read_bytes()
            state["truncate_next"] = True
            try:
                local = p.discover_local()
            except IncompleteStage:
                self.fail(read_json(p.output / "schema/local-manifest.json")["errors"])
            self.assertEqual(local["strategy"], "ast-01")
            self.assertTrue((p.output / "schema/ast-01-input-report.json").is_file())
            count = state["requests"]
            self.assertGreater(count, 0)
            self.assertGreater(p.stage_stats[-1]["truncated_requests"], 0)
            p.discover_local()
            self.assertEqual(state["requests"], count)
            p.merge_schema()
            self.assertEqual((dest / "prepared/ast/packs.jsonl").read_bytes(), prepared)
            for request in state["payloads"]:
                value = json.loads(request["messages"][1]["content"])
                if value["stage"] == "discover":
                    self.assertEqual(value["ast_01_format"], 1)
            manifest_path = p.output / "schema/local-manifest.json"
            write_json(manifest_path, {**read_json(manifest_path), "ast_01_format": 999})
            runner = SchemaStages(p, "merge")
            try:
                with self.assertRaises(IncompleteStage):
                    runner.verify_local()
            finally:
                runner.close()
            legacy = Pipeline(dest, "ast", "general", "original-test", settings, {**policy, "discovery_strategy": "budget"})
            legacy.discover_local()
            value = json.loads(state["payloads"][-1]["messages"][1]["content"])
            self.assertNotIn("ast_01_format", value)
            self.assertFalse((legacy.output / "schema/ast-01-input-report.json").exists())


if __name__ == "__main__":
    unittest.main()
