"""Independent, resumable AI review. A draft remains available even if review fails."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from .llm import LLM, BudgetError, RequestUnknown, parse_json
from .models import Ontology, StrictModel
from .schema_history import SchemaHistory, split_location
from .schema_merge import (CATEGORIES, MergeResponse, SourceMapping, alignment_findings,
                           canonical_merge, merge_payload, retain_source_metadata, validate_merge)
from .util import canonical, digest, now, read_json, write_json, write_jsonl
from .events import emit
from .review_ledger import update_ledger
import time


class ReviewIssue(StrictModel):
    issue_id: str = ""
    category: str = "semantic"
    source_ids: list[str] = Field(default_factory=list)
    target_ids: list[str] = Field(default_factory=list)
    severity: Literal["info", "warning", "error"]
    status: Literal["fixed", "unresolved"]
    description: str = Field(min_length=1)


class ReviewResponse(StrictModel):
    ontology: Ontology
    source_mapping: list[SourceMapping]
    issues: list[ReviewIssue]


def assess_alignment(result, original, decisions, issues=()):
    """A source schema is a model hypothesis. Explicit AI corrections remain auditable.

    This checks references and evidence anchors, not whether the AI explanation is true.
    A later edit to the target or referenced class mappings invalidates the decision.
    """
    sources = {r["id"]: r for s in original["candidates"] for cat in CATEGORIES for r in s[cat]}
    targets = {r.id: r for cat in CATEGORIES for r in getattr(result.ontology, cat)}
    mappings = {m.source_id: m.target_id for m in result.source_mapping}
    accepted, pending = [], []
    updated = dict(decisions)
    for finding in alignment_findings(result, original):
        sid = finding["source_id"]
        target = targets.get(mappings[sid])
        refs = {ref: mappings[ref] for field in ("parents", "domain", "range") for ref in sources[sid].get(field, [])}
        # Wording and alias edits alone do not invalidate a structural correction.
        semantic = {k: v for k, v in target.model_dump().items()
                    if k in {"id", "parents", "domain", "range", "datatype", "unit"}} if target else None
        signature = digest({"target": semantic, "references": refs}, 40)
        key = digest(finding, 40)
        proof = set(sources[sid]["evidence_ids"]).intersection(target.evidence_ids if target else [])
        explicit = next((i for i in issues if i.status == "fixed" and sid in i.source_ids
                         and mappings[sid] in i.target_ids and proof), None)
        if explicit is not None:
            updated[key] = {"finding": finding, "signature": signature, "issue": explicit.model_dump(),
                            "evidence_ids": sorted(proof), "status": "ai_corrected_source_not_human_verified"}
        decision = updated.get(key)
        if target is not None and decision and decision["signature"] == signature:
            accepted.append(decision)
        else:
            pending.append(finding)
    return accepted, pending, updated


class SchemaReview:
    def __init__(self, pipeline):
        from .pipeline import prompt
        self.p = pipeline
        self.root = pipeline.output / "schema"
        self.settings = pipeline.settings.for_stage("review")
        self.system = prompt(pipeline.policy, pipeline.profile, "review")
        self.llm = LLM(self.settings, pipeline.dest / "cache/llm")
        self.started = time.monotonic()
        self.llm.request_guard = self.check_budget

    def check_budget(self, messages):
        from .stages import IncompleteStage
        from .util import token_bound
        stats, policy = self.llm.stats, self.p.policy
        exhausted = (policy.get("review_max_requests", 0) and stats["requests"] >= policy["review_max_requests"]
            or policy.get("review_max_seconds", 0) and time.monotonic()-self.started >= policy["review_max_seconds"]
            or policy.get("review_max_tokens", 0) and (stats["usage_missing"] or stats["prompt_tokens"]+stats["completion_tokens"]+token_bound(messages)+self.settings.max_output_tokens > policy["review_max_tokens"]))
        if exhausted:
            raise IncompleteStage("Review budget reached or usage unknown; checkpoints retained. Inspect review report before resuming")

    def close(self):
        self.p.stage_stats.append(dict(self.llm.stats))
        self.llm.close()

    def fingerprint(self):
        return digest({"draft": read_json(self.root / "draft-schema.json"),
                       "mapping": read_json(self.root / "draft-source-mapping.json"),
                       "settings": self.settings.public(), "prompt": self.system,
                       "packs": self.p.packs, "batch_items": self.p.policy["review_batch_items"],
                       "max_rounds": self.p.policy["review_max_rounds"], "review_protocol": 3,
                       "budgets": {key: self.p.policy.get(key, 0) for key in ("review_max_requests", "review_max_seconds", "review_max_tokens")},
                       "contract": ReviewResponse.model_json_schema()}, 40)

    def run(self):
        from .stages import SchemaStages, IncompleteStage
        checker = SchemaStages(self.p, "merge")
        try:
            checker.verify_merged()
            local = checker.verify_local()
        finally:
            checker.close()
        key = self.fingerprint()
        archive = SchemaHistory(self.root, "review", key)
        print(f"SCHEMA_HISTORY={archive.root}", flush=True)
        self.p.save_state("review-schema", "running")
        write_json(self.root / "review-status.json", {"state": "running", "key": key, "started_at": now()})
        original, source_index = merge_payload(local["schemas"], self.p.profile)
        source_rows = {sid: row["row"] for sid, row in source_index.items()}
        audit = read_json(self.root / "draft-source-mapping.json")
        registry = {r["source_id"]: {k: r[k] for k in ("source_id", "category", "source_schema", "source")} for r in audit["sources"]}
        mappings = {r["source_id"]: SourceMapping(source_id=r["source_id"], target_id=r["target_id"],
                    action="merge" if r["target_id"] else "unresolved", reason=r.get("reason", "Merged draft mapping")) for r in audit["sources"]}
        schema = Ontology.model_validate(read_json(self.root / "draft-schema.json"))
        checkpoints, history = [], []
        decisions = {}
        ledger = {}
        archive.snapshot("input", [schema.model_dump()], mapping=audit)

        def combined(value, ids):
            found = {m.source_id: m for m in value.source_mapping}
            context_ids = {ref for sid in ids for field in ("parents", "domain", "range") for ref in source_rows[sid].get(field, [])}
            missing = set(ids) - found.keys()
            unknown = found.keys() - set(ids) - context_ids
            if len(found) != len(value.source_mapping) or missing or unknown:
                raise ValueError(f"Review mapping must cover source_items once; missing={sorted(missing)}, unknown={sorted(unknown)}")
            for sid in found.keys() - set(ids):
                if found[sid].target_id != mappings[sid].target_id:
                    raise ValueError(f"Read-only class_context mapping changed: {sid}")
            found = {sid: found[sid] for sid in ids}
            output_ids = {r.id for cat in CATEGORIES for r in getattr(value.ontology, cat)}
            for issue in value.issues:
                # A report can mention a known item elsewhere in the complete schema.
                # Only current source_items can receive a mapping/correction decision.
                unknown_sources = set(issue.source_ids) - source_rows.keys()
                unknown_targets = set(issue.target_ids) - output_ids
                if unknown_sources or unknown_targets:
                    raise ValueError(f"Review issue references unknown IDs: sources={sorted(unknown_sources)}, targets={sorted(unknown_targets)}")
            result = MergeResponse(ontology=value.ontology, source_mapping=list({**mappings, **found}.values()))
            validate_merge(result, original, compatibility=False)
            # Semantic disagreements are reviewed across complete rounds, not used to
            # discard an otherwise valid candidate before it can be inspected.
            return retain_source_metadata(result, original)

        def make(ids):
            references = {ref for sid in ids for field in ("parents", "domain", "range") for ref in source_rows[sid].get(field, [])}
            evidence = {e for sid in ids for e in source_rows[sid]["evidence_ids"]}
            # Review needs complete code and anchors, not the duplicated parser graph context.
            packs = [{k: p[k] for k in ("pack_id", "kind", "code", "source_refs", "evidence_ids")}
                     for p in self.p.packs if evidence.intersection(p["evidence_ids"])]
            return {"stage": "review", "profile": self.p.profile, "current_schema": schema.model_dump(),
                    "pending_issues": [row for row in ledger.values() if row["status"] == "unresolved" and (not row["source_ids"] or set(ids).intersection(row["source_ids"]))],
                    "source_items": [{"category": source_index[sid]["category"], "item": source_rows[sid],
                                      "current_mapping": mappings[sid].model_dump()} for sid in ids],
                    "class_context": [{"item": source_rows[ref], "current_mapping": mappings[ref].model_dump()} for ref in sorted(references)],
                    "program_findings": [f for f in alignment_findings(MergeResponse(ontology=schema, source_mapping=list(mappings.values())), original) if f["source_id"] in ids],
                    "evidence_packs": packs}

        def process(ids, round_number, location, depth=0):
            nonlocal schema, mappings, decisions
            payload = make(ids)
            if round_number > 1:
                payload["review_focus"] = ("This is a targeted correction round for remaining inconsistencies. "
                    "Correct source mappings to an existing compatible item where appropriate. If original local metadata is wrong, "
                    "fix it with an explicit fixed issue naming that source_id, corrected target_id and concrete code evidence. "
                    "Do not change unrelated items or merely repeat an already diagnosed mismatch.")
            task_key = digest({"input": payload, "prompt": self.system, "settings": self.settings.public(),
                               "contract": ReviewResponse.model_json_schema()}, 40)
            path = self.root / "review" / f"{task_key}.json"
            task = path.with_suffix(".task.json")
            try:
                if task.is_file() and read_json(task).get("state") == "needs_split":
                    raise BudgetError("Review checkpoint requires split")
                write_json(task, {"state": "running", "key": task_key, "source_ids": ids, "started_at": now()})
                if path.is_file():
                    value = ReviewResponse.model_validate(read_json(path)["response"])
                    combined(value, ids)
                else:
                    # Reuse an earlier successful review that additionally included parser graph
                    # context. Complete code, anchors, mappings and current schema are identical.
                    evidence = {e for sid in ids for e in source_rows[sid]["evidence_ids"]}
                    legacy = {**payload, "evidence_packs": [p for p in self.p.packs if evidence.intersection(p["evidence_ids"])]}
                    legacy_key = digest({"input": legacy, "prompt": self.system, "settings": self.settings.public(),
                                         "contract": ReviewResponse.model_json_schema()}, 40)
                    legacy_path = self.root / "review" / f"{legacy_key}.json"
                    if legacy_path.is_file():
                        value = ReviewResponse.model_validate(read_json(legacy_path)["response"])
                        combined(value, ids)
                        print(f"REVIEW cache reused={legacy_key[:10]} (same code and anchors)", flush=True)
                    else:
                        value = None
                        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": canonical(payload)}]
                        response_key = digest({"messages": messages, "settings": self.settings.public(),
                                               "contract": ReviewResponse.model_json_schema(), "namespace": self.p.namespace}, 40)
                        # A previously rejected response can become valid after a validator fix.
                        # Revalidate its exact input/contract/settings, never rewrite its contents.
                        for trace in sorted((self.p.output / "llm_requests").glob(f"{response_key}-*.json"), reverse=True):
                            record = read_json(trace)
                            if record.get("finish_reason") != "stop" or not record.get("raw_content") or record.get("parameters", {}).get("messages", [])[:2] != messages:
                                continue
                            try:
                                recovered = ReviewResponse.model_validate(parse_json(record["raw_content"]))
                                combined(recovered, ids)
                            except ValueError:
                                continue
                            value = recovered
                            print(f"REVIEW recovered validated response={trace.name}", flush=True)
                            break
                        if value is None:
                            candidate_number = 0
                            def validate_candidate(candidate):
                                nonlocal candidate_number
                                candidate_number += 1
                                archive.snapshot(f"{location}/candidate-{candidate_number:03d}",
                                    [candidate.ontology.model_dump()], state="candidate_not_validated",
                                    mapping=[m.model_dump() for m in candidate.source_mapping],
                                    issues=[i.model_dump() for i in candidate.issues])
                                combined(candidate, ids)
                            value = self.llm.call(self.system, payload, ReviewResponse, self.p.output / "llm_requests",
                                                  validate_candidate, self.p.namespace)
                    write_json(path, {"key": task_key, "response": value.model_dump(), "settings": self.settings.public(),
                                     "reused_from": legacy_path.name if legacy_path.is_file() else None})
                result = combined(value, ids)
                # Keep IDs stable during review; canonicalize and compose once at publication.
                schema = result.ontology
                mappings = {m.source_id: m for m in result.source_mapping}
                current_issues = [i.model_copy(update={"source_ids": [sid for sid in i.source_ids if sid in ids]}) for i in value.issues]
                accepted, pending, decisions = assess_alignment(result, original, decisions, current_issues)
                checkpoints.append(path.relative_to(self.root).as_posix())
                history.extend([{"round": round_number, **i.model_dump()} for i in value.issues])
                update_ledger(ledger, value.issues, schema, round_number)
                write_json(self.root / "review-issues.json", {"issues": list(ledger.values()), "schema_hash": digest(schema.model_dump()),
                           "business_acceptance": "pending_human_review", "coverage": {"planned_sources": len(source_rows), "completed_checkpoints": len(checkpoints)}})
                write_json(self.root / "review-candidate.json", schema.model_dump())
                archive.snapshot(f"{location}/result", [schema.model_dump()],
                    mapping=[m.model_dump() for m in result.source_mapping],
                    issues=[i.model_dump() for i in value.issues], program_findings=pending, ai_corrections=accepted,
                    checkpoint=task_key)
                write_json(task, {"state": "success", "key": task_key, "ended_at": now()})
            except BudgetError:
                write_json(task, {"state": "needs_split", "key": task_key, "source_ids": ids, "ended_at": now()})
                if len(ids) < 2 or depth >= self.p.policy["recovery_max_depth"]:
                    raise
                cut = len(ids) // 2
                process(ids[:cut], round_number, split_location(location, 1), depth + 1)
                process(ids[cut:], round_number, split_location(location, 2), depth + 1)
            except Exception as exc:
                write_json(task, {"state": "unknown" if isinstance(exc, RequestUnknown) else "failed", "key": task_key,
                                  "source_ids": ids, "error": self.llm.safe_error(exc), "ended_at": now()})
                raise

        try:
            ids = sorted(source_rows, key=lambda sid: (CATEGORIES.index(source_index[sid]["category"]), sid))
            size = self.p.policy["review_batch_items"]
            for round_number in range(1, self.p.policy["review_max_rounds"] + 1):
                batches = [ids[i:i+size] for i in range(0, len(ids), size)]
                history_start = len(history)
                for i, batch in enumerate(batches):
                    emit("progress", stage="review", completed=i, total=len(batches), round=round_number, message="审查批次开始")
                    print(f"REVIEW round={round_number} batch={i+1}/{len(batches)} source_items={len(batch)} started={now()}", flush=True)
                    process(batch, round_number, f"round-{round_number:03d}/batch-{i+1:03d}")
                    emit("progress", stage="review", completed=i+1, total=len(batches), successful=i+1, round=round_number, message="审查批次完成")
                    write_json(self.root / "review-status.json", {"state": "running", "key": key, "round": round_number,
                               "completed": i+1, "total": len(batches), "updated_at": now()})
                result = MergeResponse(ontology=schema, source_mapping=list(mappings.values()))
                accepted, findings, decisions = assess_alignment(result, original, decisions)
                archive.snapshot(f"round-{round_number:03d}/result", [schema.model_dump()],
                    mapping=[m.model_dump() for m in result.source_mapping], program_findings=findings, ai_corrections=accepted)
                errors = [i for i in ledger.values() if i["severity"] == "error" and i["status"] == "unresolved"]
                write_json(self.root / "review-report.json", {"key": key, "issues": history, "program_findings": findings,
                           "ai_corrections": accepted, "history": str(archive.root),
                           "review_status": "ai_review_not_human_verified", "checkpoints": checkpoints})
                if not findings and not errors:
                    review_targets = {m.source_id: m.target_id for m in result.source_mapping}
                    result = canonical_merge(result, original, self.p.profile, compatibility=False)
                    final = result.ontology.model_dump()
                    publication_ids = {review_targets[m.source_id]: m.target_id for m in result.source_mapping
                                       if review_targets[m.source_id] is not None}
                    targets = {r.id: r.name for cat in CATEGORIES for r in getattr(result.ontology, cat)}
                    mapped_audit = {**audit, "schema_hash": digest(final), "review_key": key,
                        "sources": [{**registry[m.source_id], "target_id": m.target_id, "target_name": targets.get(m.target_id),
                                     "state": "mapped" if m.target_id else "unresolved", "reason": m.reason} for m in result.source_mapping],
                        "review_checkpoints": checkpoints, "review_status": "ai_review_not_human_verified"}
                    write_json(self.root / "schema.json", final)
                    write_json(self.root / "source-mapping.json", mapped_audit)
                    write_jsonl(self.root / "unresolved.jsonl", [i for i in history[history_start:] if i["status"] == "unresolved"])
                    write_json(self.root / "review-report.json", {"key": key, "issues": history, "program_findings": findings,
                               "ai_corrections": accepted, "history": str(archive.root), "checkpoints": checkpoints,
                               "review_status": "ai_review_not_human_verified", "publication_id_mapping": publication_ids,
                               "published_schema_hash": digest(final)})
                    archive.snapshot("final", [final], mapping=mapped_audit, ai_corrections=accepted,
                                     publication_id_mapping=publication_ids)
                    archive.finish("success", review_status="ai_review_not_human_verified")
                    write_json(self.root / "review-status.json", {"state": "success", "key": key, "schema_hash": digest(final),
                               "mapping_hash": digest(mapped_audit), "history": str(archive.root), "ended_at": now(), "review_status": "ai_review_not_human_verified"})
                    from .artifacts import update_cost_coverage
                    update_cost_coverage(self.root, "review", len(self.p.packs), state="success",
                                         planned_sources=len(source_rows), completed_checkpoints=len(checkpoints),
                                         unresolved_issues=0, rounds=round_number,
                                         elapsed_seconds=round(time.monotonic()-self.started, 3), llm=dict(self.llm.stats))
                    self.p.save_state("review-schema", "success", schema_hash=digest(final))
                    return result.ontology
                focus = {f["source_id"] for f in findings} | {sid for i in errors for sid in i["source_ids"]}
                # Errors without a source list require revisiting all original items.
                if errors and any(not i["source_ids"] for i in errors):
                    focus = set(source_rows)
                ids = sorted(focus, key=lambda sid: (CATEGORIES.index(source_index[sid]["category"]), sid))
            raise IncompleteStage("AI review still has unresolved errors; draft and review candidates retained; inspect review-report.json")
        except BaseException as exc:
            archive.finish("partial", error=self.llm.safe_error(exc))
            from .artifacts import update_cost_coverage
            update_cost_coverage(self.root, "review", len(self.p.packs), state="partial",
                                 planned_sources=len(source_rows), completed_checkpoints=len(checkpoints),
                                 unresolved_issues=sum(1 for issue in ledger.values() if issue["status"] == "unresolved"),
                                 error=self.llm.safe_error(exc), elapsed_seconds=round(time.monotonic()-self.started, 3),
                                 llm=dict(self.llm.stats))
            partial = MergeResponse(ontology=schema, source_mapping=list(mappings.values()))
            accepted, pending, _ = assess_alignment(partial, original, decisions)
            write_json(self.root / "review-report.json", {"key": key, "issues": history,
                       "program_findings": pending, "ai_corrections": accepted, "history": str(archive.root),
                       "error": self.llm.safe_error(exc), "checkpoints": checkpoints})
            write_json(self.root / "review-status.json", {"state": "partial", "key": key, "history": str(archive.root), "error": self.llm.safe_error(exc), "ended_at": now()})
            self.p.save_state("review-schema", "partial", error=self.llm.safe_error(exc))
            raise

    def verify(self):
        path = self.root / "review-status.json"
        status = read_json(path) if path.is_file() else {}
        if status.get("state") != "success" or status.get("key") != self.fingerprint():
            raise ValueError("Run review-schema first; review missing, incomplete or configuration changed")
        if status.get("schema_hash") != digest(read_json(self.root / "schema.json")) or status.get("mapping_hash") != digest(read_json(self.root / "source-mapping.json")):
            raise ValueError("Reviewed schema/mapping changed; rerun review-schema")


def check_schema_use(pipeline, require_review=False):
    """Allow the merged draft by default; require AI review only on explicit request."""
    from .stages import SchemaStages
    runner = SchemaStages(pipeline, "merge")
    try:
        runner.verify_merged()
    finally:
        runner.close()
    reviewer = SchemaReview(pipeline)
    try:
        try:
            reviewer.verify()
        except ValueError:
            if require_review:
                raise
            root = pipeline.output / "schema"
            if read_json(root / "schema.json") != read_json(root / "draft-schema.json"):
                raise ValueError("Current schema differs from merged draft without valid review provenance; rerun merge-schema or explicitly select a schema")
            return "merged_unreviewed"
    finally:
        reviewer.close()
    return "ai_reviewed_not_human_verified"
