"""Merge identity, lineage and structural compatibility, independent of model wording."""
from copy import deepcopy
import unittest

from code_ontology.models import Extraction, Ontology
from code_ontology.schema_merge import (MergeResponse, canonical_merge, coalesce, compose,
                                       final_mapping, initial_candidates, merge_payload, validate_merge, retain_missing_sources,
                                       retain_source_metadata)


def schema():
    return {"classes": [
        {"id": "device", "name": "Device", "definition": "Equipment", "evidence_ids": ["ev1"]},
        {"id": "task", "name": "Task", "definition": "Maintenance task", "evidence_ids": ["ev1"]}],
        "relations": [{"id": "hasTask", "name": "hasTask", "definition": "Equipment owns task", "domain": ["device"],
                       "range": ["task"], "evidence_ids": ["ev1"]}],
        "properties": [{"id": "duration", "name": "duration", "definition": "Task duration", "domain": ["task"],
                        "datatype": "number", "unit": "h", "evidence_ids": ["ev1"]}]}


def renamed_response(payload):
    output = deepcopy(payload["candidates"][0])
    mapping = []
    for category in ("classes", "relations", "properties"):
        for row in output[category]:
            row["name"] = "Renamed" + row["name"]
            mapping.append({"source_id": row["id"], "target_id": row["id"], "action": "preserve", "reason": "Equivalent concept, new label"})
    return MergeResponse.model_validate({"ontology": output, "source_mapping": mapping})


class SchemaMergeTests(unittest.TestCase):
    def test_rename_and_canonicalization_preserve_all_categories(self):
        payload, _ = merge_payload([schema()], "general")
        response = canonical_merge(renamed_response(payload), payload, "general")
        target_ids = {row.id for category in (response.ontology.classes, response.ontology.relations, response.ontology.properties) for row in category}
        self.assertEqual({row.target_id for row in response.source_mapping}, target_ids)
        self.assertIn("hasTask", response.ontology.relations[0].aliases)
        self.assertIn("duration", response.ontology.properties[0].aliases)
        self.assertEqual(response.ontology.relations[0].evidence_ids, ["ev1"])

    def test_complete_mapping_is_mandatory_not_name_matching(self):
        payload, _ = merge_payload([schema()], "general")
        response = renamed_response(payload)
        response.source_mapping.pop()
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_merge(response, payload)
        response = renamed_response(payload)
        response.source_mapping.append(response.source_mapping[0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_merge(response, payload)
        response = renamed_response(payload)
        response.source_mapping[0].target_id = "missing"
        with self.assertRaisesRegex(ValueError, "same category"):
            validate_merge(response, payload)

    def test_direction_datatype_and_unit_are_checked(self):
        payload, _ = merge_payload([schema()], "general")
        for change in ("direction", "datatype", "unit"):
            response = renamed_response(payload)
            if change == "direction":
                relation = response.ontology.relations[0]
                relation.domain, relation.range = relation.range, relation.domain
            elif change == "datatype":
                response.ontology.properties[0].datatype = "string"
            else:
                response.ontology.properties[0].unit = "s"
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_merge(response, payload)

    def test_same_name_different_meanings_have_distinct_ids(self):
        raw = schema()
        raw["properties"].append({**raw["properties"][0], "id": "duration2", "definition": "Equipment lifetime",
                                  "domain": ["device"]})
        payload, _ = merge_payload([raw], "general")
        result = canonical_merge(renamed_response(payload), payload, "general")
        self.assertEqual(len(result.ontology.properties), 2)
        self.assertEqual(len({p.id for p in result.ontology.properties}), 2)

    def test_integer_may_widen_to_number_but_number_must_not_narrow(self):
        raw = schema()
        raw["properties"][0]["datatype"] = "integer"
        payload, _ = merge_payload([raw], "general")
        response = renamed_response(payload)
        response.ontology.properties[0].datatype = "number"
        validate_merge(response, payload)
        payload, _ = merge_payload([schema()], "general")
        response = renamed_response(payload)
        response.ontology.properties[0].datatype = "integer"
        with self.assertRaisesRegex(ValueError, "narrows"):
            validate_merge(response, payload)

    def test_multi_round_lineage_and_dedup_retain_original_sources(self):
        candidates, registry = initial_candidates([schema()])
        for _ in range(3):
            payload, sources = merge_payload([c["schema"] for c in candidates], "general")
            result = canonical_merge(renamed_response(payload), payload, "general")
            candidate = compose(candidates, result, sources)
            candidates = coalesce([candidate, deepcopy(candidate)])
        audit = final_mapping(candidates[0], registry)
        self.assertEqual(len(audit), 4)
        self.assertTrue(all(r["state"] == "mapped" for r in audit))
        self.assertTrue(all(r["target_id"] in candidates[0]["lineage"] for r in audit))

    def test_unresolved_sources_stay_pending_across_rounds(self):
        candidates, registry = initial_candidates([schema()])
        payload, sources = merge_payload([c["schema"] for c in candidates], "general")
        result = renamed_response(payload)
        result.ontology.properties = []
        mapping = result.source_mapping[-1]
        mapping.target_id, mapping.action, mapping.reason = None, "unresolved", "Conflicting definition"
        candidate = compose(candidates, canonical_merge(result, payload, "general", compatibility=False), sources)
        next_payload, next_sources = merge_payload([candidate["schema"]], "general")
        candidate = compose([candidate], canonical_merge(renamed_response(next_payload), next_payload, "general"), next_sources)
        audit = final_mapping(candidate, registry)
        self.assertEqual(sum(r["state"] == "unresolved" for r in audit), 1)

    def test_input_ids_do_not_collide_between_schemas(self):
        second = schema()
        second["classes"][0]["definition"] = "Different equipment role"
        payload, index = merge_payload([schema(), second], "general")
        self.assertEqual(len(index), 8)
        for candidate in payload["candidates"]:
            Ontology.model_validate(candidate)

    def test_missing_mapping_is_retained_for_review_without_name_guessing(self):
        payload, _ = merge_payload([schema()], "general")
        response = renamed_response(payload)
        response.ontology.properties = []
        missing = response.source_mapping.pop().source_id
        retain_missing_sources(response, payload)
        validate_merge(response, payload, compatibility=False)
        mapping = next(m for m in response.source_mapping if m.source_id == missing)
        self.assertEqual(mapping.action, "unresolved")
        self.assertIsNone(mapping.target_id)

    def test_multiple_tasks_are_multiple_binary_assertions(self):
        result = Extraction.model_validate({"entities": [
            {"id": eid, "name": eid, "type_id": typ, "scope": "example", "basis": "direct", "evidence_ids": ["ev1"]}
            for eid, typ in (("pump", "device"), ("taskA", "task"), ("taskB", "task"))],
            "relations": [{"subject": "pump", "object": tid, "relation_id": "hasTask", "basis": "direct", "evidence_ids": ["ev1"]}
                          for tid in ("taskA", "taskB")], "attributes": [], "rules": []})
        self.assertEqual(len(result.relations), 2)

    def test_review_can_correct_wrong_source_hypothesis_with_traceable_explanation(self):
        from code_ontology.schema_review import ReviewIssue, assess_alignment
        payload, _ = merge_payload([schema()], "general")
        response = renamed_response(payload)
        response.ontology.properties[0].datatype = "integer"
        sid = response.source_mapping[-1].source_id
        tid = response.source_mapping[-1].target_id
        issue = ReviewIssue(source_ids=[sid], target_ids=[tid], severity="warning", status="fixed",
                            description="Source declaration uses int; previous local number metadata was imprecise")
        accepted, pending, decisions = assess_alignment(response, payload, {}, [issue])
        self.assertEqual(len(accepted), 1)
        self.assertFalse(pending)
        self.assertEqual(accepted[0]["evidence_ids"], ["ev1"])
        response.ontology.properties[0].definition += "; revised wording"
        self.assertFalse(assess_alignment(response, payload, decisions)[1])
        # A later target edit must be examined again, not use an old AI decision.
        response.ontology.properties[0].unit = "s"
        self.assertTrue(assess_alignment(response, payload, decisions)[1])

    def test_review_cannot_waive_findings_without_specific_target_and_evidence(self):
        from code_ontology.schema_review import ReviewIssue, assess_alignment
        payload, _ = merge_payload([schema()], "general")
        response = renamed_response(payload)
        response.ontology.properties[0].datatype = "string"
        sid = response.source_mapping[-1].source_id
        issue = ReviewIssue(source_ids=[sid], target_ids=[], severity="info", status="fixed", description="Looks fine")
        self.assertTrue(assess_alignment(response, payload, {}, [issue])[1])
        issue.target_ids = [response.ontology.properties[0].id]
        response.ontology.properties[0].evidence_ids = ["other-source-evidence"]
        self.assertTrue(assess_alignment(response, payload, {}, [issue])[1])

    def test_review_preserves_mapped_provenance_when_model_drops_source_anchors(self):
        from code_ontology.schema_review import ReviewIssue, assess_alignment
        raw = schema()
        raw["properties"][0]["evidence_ids"] = ["ev1", "ev2"]
        payload, _ = merge_payload([raw], "general")
        response = renamed_response(payload)
        response.ontology.properties[0].datatype = "integer"
        response.ontology.properties[0].evidence_ids = ["ev1"]
        validate_merge(response, payload, compatibility=False)
        retain_source_metadata(response, payload)
        self.assertEqual(response.ontology.properties[0].evidence_ids, ["ev1", "ev2"])
        self.assertIn("duration", response.ontology.properties[0].aliases)
        sid = response.source_mapping[-1].source_id
        issue = ReviewIssue(source_ids=[sid], target_ids=[response.ontology.properties[0].id],
                            severity="warning", status="fixed", description="The code declares integer hours")
        accepted, pending, _ = assess_alignment(response, payload, {}, [issue])
        self.assertFalse(pending)
        self.assertEqual(accepted[0]["evidence_ids"], ["ev1", "ev2"])


if __name__ == "__main__":
    unittest.main()
