"""Extraction batching contracts and recovery using a local synthetic model only."""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_ontology.config import Settings, configuration_sources, load_policy
from code_ontology.models import Extraction
from code_ontology.parsing import parse_repository
from code_ontology.pipeline import Pipeline
from code_ontology.prepare import prepare
from code_ontology.repository import snapshot
from code_ontology.util import digest, read_json, write_json
from tests.fake_api import fake_server, fixture_response


class ExtractBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="extract-batch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.root / "empty.env"
        self.env.write_text("", encoding="utf-8")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "code.cpp").write_text("\n".join(f"int device{i}(int x) {{ return x + {i}; }}" for i in range(4)) + "\n", encoding="utf-8")
        self.policy = load_policy(env_file=str(self.env))
        self.policy.update(parser_backend="tree-sitter", views=["ast"], profiles=["general"])
        self.dest = snapshot(self.repo, self.root / "out", self.policy)
        parse_repository(self.dest, Settings(), self.policy, ["ast"])
        prepare(self.dest, "ast", self.policy)

    def pipeline(self, url, cap, name="batch"):
        p = Pipeline(self.dest, "ast", "general", name, Settings(base_url=url, model="fixture", concurrency=1, retries=0, context_tokens=65536),
                     {**self.policy, "extract_batch_packs": cap})
        self.addCleanup(p.close)
        evidence = sorted(p.evidence)
        schema = {"classes": [dict(id="device", name="device", definition="fixture", evidence_ids=evidence)],
                  "relations": [dict(id="r", name="links", definition="fixture", domain=["device"], range=["device"], evidence_ids=evidence)],
                  "properties": [dict(id="p", name="value", definition="fixture", domain=["device"], datatype="integer", evidence_ids=evidence)]}
        write_json(p.output / "schema/schema.json", schema)
        fixed = p.fixed_schema()
        write_json(p.output / "schema/schema.json", fixed.model_dump())
        return p, fixed

    def test_config_precedence_positive_validation_and_default(self):
        self.assertEqual(self.policy["extract_batch_packs"], 1)
        self.env.write_text("CO_EXTRACT_BATCH_PACKS=4\n", encoding="utf-8")
        yaml = self.root / "policy.yaml"
        yaml.write_text("extract_batch_packs: 2\n", encoding="utf-8")
        self.assertEqual(load_policy(str(yaml), str(self.env))["extract_batch_packs"], 4)
        with patch.dict(os.environ, {"CO_EXTRACT_BATCH_PACKS": "8"}):
            self.assertEqual(load_policy(str(yaml), str(self.env))["extract_batch_packs"], 8)
        self.assertEqual(configuration_sources(str(self.env))["policy"]["extract_batch_packs"], str(self.env))
        for value in ("0", "-1", "not-a-number"):
            self.env.write_text(f"CO_EXTRACT_BATCH_PACKS={value}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_policy(env_file=str(self.env))
        self.env.write_text("CO_EXTRACT_BATCH_PACKS=\n", encoding="utf-8")
        self.assertEqual(load_policy(str(yaml), str(self.env))["extract_batch_packs"], 2)
        self.env.write_text("CO_EXTRACT_BATCH_PACKS=8\n", encoding="utf-8")
        self.assertEqual(load_policy(str(yaml), str(self.env), frozen=True)["extract_batch_packs"], 2)

    def test_batch_change_does_not_invalidate_local_discovery(self):
        with fake_server() as (url, state):
            p, _ = self.pipeline(url, 1)
            p.discover_local()
            key = read_json(p.output / "schema/local-manifest.json")["key"]
            before = state["requests"]
            p.policy["extract_batch_packs"] = 4
            p.discover_local()
            self.assertEqual(state["requests"], before)
            self.assertEqual(read_json(p.output / "schema/local-manifest.json")["key"], key)

    def test_count_cap_full_coverage_and_cached_rerun(self):
        with fake_server() as (url, state):
            p, schema = self.pipeline(url, 3)
            self.assertEqual(len(p.packs), 4)
            self.assertTrue(p.extract(schema, finalize=False))
            selection = read_json(p.output / "extraction/selection.json")
            self.assertEqual([len(x["packs"]) for x in selection], [3, 1])
            self.assertEqual([pid for x in selection for pid in x["packs"]], [x["pack_id"] for x in p.packs])
            self.assertEqual(state["stage_counts"]["extract"], 2)
            manifest = read_json(p.output / "extraction/manifest.json")
            self.assertEqual((manifest["batch_packs"], manifest["total_packs"]), (3, 4))
            self.assertTrue(p.extract(schema, finalize=False))
            self.assertEqual(state["stage_counts"]["extract"], 2)
            p.finalize_saved()
            self.assertEqual(read_json(p.output / "metrics.json")["status"], "success")

    def test_budget_reduces_batch_without_pruning_schema(self):
        with fake_server() as (url, state):
            p, schema = self.pipeline(url, 4)
            real_fits = p.llm.fits
            def fits(messages):
                payload = json.loads(messages[1]["content"])
                return len(payload.get("packs", [])) <= 2 and real_fits(messages)
            with patch.object(p.llm, "fits", side_effect=fits):
                self.assertTrue(p.extract(schema, finalize=False))
            selection = read_json(p.output / "extraction/selection.json")
            self.assertEqual([len(x["packs"]) for x in selection], [2, 2])
            self.assertTrue(all(not x["omitted_schema_ids"] for x in selection))
            self.assertEqual(state["stage_counts"]["extract"], 2)

    def test_default_preserves_single_pack_task_keys_and_fingerprint(self):
        with fake_server() as (url, state):
            p, schema = self.pipeline(url, 1)
            key = p.extraction_fingerprint(schema)
            p.policy.pop("extract_batch_packs")
            self.assertEqual(key, p.extraction_fingerprint(schema))
            self.assertTrue(p.extract(schema, finalize=False))
            selection = read_json(p.output / "extraction/selection.json")
            self.assertEqual(len(selection), 4)
            self.assertEqual(selection[0]["task_id"], digest([p.packs[0]["pack_id"], digest(schema.model_dump())]))
            p.policy["extract_batch_packs"] = 4
            self.assertNotEqual(key, p.extraction_fingerprint(schema))
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                p.finalize_saved()

    def test_truncation_splits_packs_and_resumes_only_failed_child(self):
        with fake_server() as (url, state):
            p, schema = self.pipeline(url, 4)
            counts = Counter()
            def response(payload):
                counts.update(pack["pack_id"] for pack in payload["packs"])
                if len(payload["packs"]) > 1:
                    state["truncate_next"] = True
                value = fixture_response(payload)
                value["relations"] = [dict(subject="e1", relation_id="r", object="e1", basis="direct",
                                            evidence_ids=value["entities"][0]["evidence_ids"])]
                value["attributes"] = [dict(subject="e1", property_id="p", value=1, basis="direct",
                                             evidence_ids=value["entities"][0]["evidence_ids"])]
                return value
            state["response_factory"] = response
            real_call = p.llm.call
            fail_id = p.packs[-1]["pack_id"]
            def fail_last(system, payload, *args, **kwargs):
                if len(payload["packs"]) == 1 and payload["packs"][0]["pack_id"] == fail_id:
                    raise ValueError("temporary failure")
                return real_call(system, payload, *args, **kwargs)
            with patch.object(p.llm, "call", side_effect=fail_last):
                self.assertFalse(p.extract(schema, finalize=False))
            self.assertEqual(read_json(p.output / "extraction/manifest.json")["state"], "partial")
            before = state["requests"]
            successful_counts = counts.copy()
            p.close()
            resumed, schema = self.pipeline(url, 4)
            self.assertTrue(resumed.extract(schema, finalize=False))
            self.assertEqual(state["requests"], before + 1)
            for pack in p.packs[:-1]:
                self.assertEqual(counts[pack["pack_id"]], successful_counts[pack["pack_id"]])
            manifest = read_json(resumed.output / "extraction/manifest.json")
            result = Extraction.model_validate(read_json(resumed.output / "extraction" / f"{manifest['tasks'][0]}.json"))
            self.assertEqual(len(result.entities), 4)
            ids = {row.id for row in result.entities}
            self.assertEqual(len(ids), 4)
            self.assertTrue(all(row.subject in ids and row.object in ids for row in result.relations))
            self.assertTrue(all(row.subject in ids for row in result.attributes))
            self.assertEqual({eid for row in result.entities for eid in row.evidence_ids}, set(resumed.evidence))
            resumed.finalize_saved()

    def test_child_validator_rejects_sibling_evidence(self):
        with fake_server() as (url, _):
            p, _ = self.pipeline(url, 4)
            parent = p.extraction_validator(p.packs)
            child = p.extraction_validator(p.packs[:1], parent)
            result = Extraction.model_validate(dict(entities=[dict(id="e1", name="fixture", type_id="device",
                scope="test", basis="direct", evidence_ids=p.packs[-1]["evidence_ids"])], relations=[], attributes=[], rules=[], candidates=[]))
            parent(result)
            with self.assertRaisesRegex(ValueError, "outside this task"):
                child(result)


if __name__ == "__main__":
    unittest.main()
