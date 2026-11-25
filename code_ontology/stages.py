"""Independent, resumable schema stages. Checkpoints never imply semantic validation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from collections import defaultdict
import time

from .llm import LLM, BudgetError, ModelFailure, RequestUnknown
from .models import Ontology, supported_ids
from .schema_history import SchemaHistory, split_location
from .schema_merge import (MERGE_FORMAT, MergeResponse, canonical_merge, coalesce, compose,
                           final_mapping, initial_candidates, merge_payload, validate_merge, alignment_findings, retain_missing_sources)
from .util import canonical, digest, now, read_json, unique, write_json, write_jsonl


class IncompleteStage(RuntimeError):
    pass


class SchemaStages:
    def __init__(self, pipeline, stage):
        self.p = pipeline
        self.stage = stage
        self.settings = pipeline.settings.for_stage(stage)
        self.llm = LLM(self.settings, pipeline.dest / "cache" / "llm")
        from .pipeline import prompt
        self.system = prompt(pipeline.policy, pipeline.profile, stage)
        self.root = pipeline.output / "schema"
        self.errors = []
        self.history = None
        self.history_location = None

    def close(self):
        self.p.stage_stats.append(dict(self.llm.stats))
        self.llm.close()

    def signature(self, payload):
        contract = MergeResponse if self.stage == "merge" else Ontology
        return digest({"input": payload, "prompt": self.system, "settings": self.settings.public(),
                       "contract": contract.model_json_schema(), "format": MERGE_FORMAT if self.stage == "merge" else 1}, 40)

    def call(self, payload):
        """A successful checkpoint is reused only for precisely matching inputs."""
        from .pipeline import evidence_check, canonical_schema
        key = self.signature(payload)
        path = self.root / ("local" if self.stage == "discover" else "merge") / f"{key}.json"
        if path.is_file():
            saved = read_json(path)
            if saved.get("key") == key:
                if self.stage == "merge":
                    value = MergeResponse.model_validate({"ontology": saved["schema"], "source_mapping": saved["source_mapping"]})
                    validate_merge(value, payload, compatibility=False)
                else:
                    value = Ontology.model_validate(saved["schema"])
                return value
        task_path = path.with_suffix(".task.json")
        if task_path.is_file() and read_json(task_path).get("state") == "needs_split":
            raise BudgetError("Checkpoint requires smaller subtasks")
        items = payload.get("packs", payload.get("candidates", []))
        allowed = ({e for p in items for e in p["evidence_ids"]} if self.stage == "discover" else
                   {e for s in items for e in supported_ids(Ontology.model_validate(s))})
        write_json(path.with_suffix(".task.json"), {"key": key, "stage": self.stage, "state": "running",
                   "started_at": now(), "settings": self.settings.public(), "evidence_ids": sorted(allowed)})
        try:
            if self.stage == "merge":
                def validate_candidate(value):
                    # A readable typed candidate exists even if the mapping needs repair.
                    write_json(path.with_suffix(".candidate.json"), value.ontology.model_dump())
                    if self.history is not None:
                        self.history_candidate += 1
                        self.history.snapshot(f"{self.history_location}/candidate-{self.history_candidate:03d}",
                            [value.ontology.model_dump()], state="candidate_not_validated",
                            mapping=[m.model_dump() for m in value.source_mapping], checkpoint=key)
                    retain_missing_sources(value, payload)
                    validate_merge(value, payload, compatibility=False)
                result = self.llm.call(self.system, payload, MergeResponse, self.p.output / "llm_requests",
                                       validate_candidate, self.p.namespace)
                result = canonical_merge(result, payload, self.p.profile, compatibility=False)
                schema = result.ontology
                extra = {"source_mapping": [m.model_dump() for m in result.source_mapping], "format": MERGE_FORMAT}
            else:
                result = self.llm.call(self.system, payload, Ontology, self.p.output / "llm_requests",
                                       lambda r: evidence_check(r, allowed), self.p.namespace)
                result = canonical_schema(result, self.p.profile)
                schema, extra = result, {}
            write_json(path, {"key": key, "schema": schema.model_dump(), **extra,
                              "settings": self.settings.public(), "prompt_hash": digest(self.system)})
            write_json(path.with_suffix(".task.json"), {"key": key, "stage": self.stage, "state": "success", "ended_at": now()})
            return result
        except Exception as exc:
            state = "unknown" if isinstance(exc, RequestUnknown) else "needs_split" if isinstance(exc, BudgetError) else "failed"
            write_json(path.with_suffix(".task.json"), {"key": key, "stage": self.stage, "state": state,
                       "ended_at": now(), "error": self.llm.safe_error(exc), "evidence_ids": sorted(allowed)})
            raise

    def recover(self, packs, depth=0):
        """Split a truncated local request. A fragment retains its original evidence anchor."""
        try:
            return [self.call({"stage": "discover", "profile": self.p.profile, "packs": packs})]
        except BudgetError:
            if depth >= self.p.policy["recovery_max_depth"]:
                raise
            if len(packs) > 1:
                cut = len(packs) // 2
                groups = [packs[:cut], packs[cut:]]
            else:
                original = packs[0]
                code = original.get("code", "")
                if len(code) < 80:
                    raise
                middle = code.find("\n", len(code) // 2)
                cut = middle + 1 if 0 <= middle < len(code) - 1 else len(code) // 2
                groups = [[{**original, "code": fragment, "context": {},
                            "fragment_of": original.get("fragment_of", original["pack_id"]),
                            "pack_id": "fragment_" + digest([original["pack_id"], i, fragment]),
                            "parse_warnings": original.get("parse_warnings", []) + ["recovery fragment; evidence anchors full original range"]}]
                          for i, fragment in enumerate((code[:cut], code[cut:]))]
            print(f"SPLIT discover depth={depth} children={len(groups)}", flush=True)
            results, failures = [], []
            for group in groups:
                try:
                    results += self.recover(group, depth + 1)
                except ModelFailure as exc:
                    if not isinstance(exc, RequestUnknown):
                        raise
                    failures.append(str(exc))
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                raise IncompleteStage("Some recovery fragments unfinished: " + "; ".join(failures))
            return results

    def local(self):
        from .pipeline import selected_packs
        selected = selected_packs(self.p.packs, self.p.policy["discovery_max_packs"])
        chosen = {p["pack_id"] for p in selected}
        write_json(self.root / "selection.json", {"selected": sorted(chosen), "total": len(self.p.packs),
                   "unselected": [p["pack_id"] for p in self.p.packs if p["pack_id"] not in chosen]})
        size = self.p.policy["batch_packs"]
        # Group by module to keep local inputs related. Input overflows enter the same split path.
        ordered = sorted(selected, key=lambda p: (str(Path(p["source_refs"][0]["file"]).parent), p["pack_id"]))
        module_packs = defaultdict(list)
        for pack in ordered:
            module_packs[str(Path(pack["source_refs"][0]["file"]).parent)].append(pack)
        batches = [rows[i:i+size] for rows in module_packs.values() for i in range(0, len(rows), size)]
        fingerprint = digest({"packs": self.p.packs, "selected": sorted(chosen), "prompt": self.system,
                              "settings": self.settings.public(), "batch_size": size,
                              "recovery_depth": self.p.policy["recovery_max_depth"]}, 40)
        write_json(self.root / "local-manifest.json", {"state": "running", "key": fingerprint,
                   "selected": sorted(chosen), "started_at": now()})
        write_json(self.root / "merge-status.json", {"state": "stale", "reason": "local discovery started", "updated_at": now()})
        self.p.save_state("discover-local", "running", tasks=len(batches))
        values, module_results, completed, started = [], defaultdict(list), 0, time.monotonic()
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            iterator = iter(batches)
            pending = {}
            def refill():
                while len(pending) < self.settings.concurrency:
                    batch = next(iterator, None)
                    if batch is None:
                        break
                    pending[pool.submit(self.recover, batch)] = batch
            refill()
            while pending:
                done, _ = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
                if not done:
                    print(f"WAIT discover {completed}/{len(batches)} elapsed={time.monotonic()-started:.0f}s logs={self.p.output / 'llm_requests'}", flush=True)
                for future in done:
                    batch = pending.pop(future)
                    try:
                        result = future.result()
                        values += result
                        module_results[str(Path(batch[0]["source_refs"][0]["file"]).parent)] += [s.model_dump() for s in result]
                    except ModelFailure as exc:
                        if not isinstance(exc, RequestUnknown):
                            raise
                        self.errors.append({"state": "unknown", "packs": [p["pack_id"] for p in batch], "error": str(exc)})
                    except Exception as exc:
                        self.errors.append({"state": "failed", "packs": [p["pack_id"] for p in batch], "error": self.llm.safe_error(exc)})
                    completed += 1
                    print(f"TASK discover {completed}/{len(batches)} ({completed-len(self.errors)} successful)", flush=True)
                refill()
        # Deterministic order is essential for stable merge checkpoints across retries.
        schemas = sorted(unique([s.model_dump() for s in values]), key=canonical)
        manifest = {"state": "partial" if self.errors else "success", "key": fingerprint,
                    "packs_hash": digest(self.p.packs), "prompt_hash": digest(self.system),
                    "settings": self.settings.public(), "batch_size": size,
                    "recovery_depth": self.p.policy["recovery_max_depth"], "selected": sorted(chosen),
                    "schemas": schemas, "modules": {k: sorted(unique(v), key=canonical) for k,v in sorted(module_results.items())},
                    "errors": self.errors, "ended_at": now()}
        write_json(self.root / "local-manifest.json", manifest)
        self.p.save_state("discover-local", manifest["state"], tasks=len(batches), failures=len(self.errors))
        if self.errors or not schemas:
            raise IncompleteStage("Local discovery incomplete; rerun discover-local. Successful checkpoints are retained")
        return manifest

    def verify_local(self):
        from .pipeline import prompt
        path = self.root / "local-manifest.json"
        if not path.is_file():
            raise IncompleteStage("Run discover-local first")
        local = read_json(path)
        selected = {p["pack_id"] for p in self.p.packs}
        expected = digest({"packs": self.p.packs, "selected": local["selected"],
                           "prompt": prompt(self.p.policy, self.p.profile, "discover"),
                           "settings": self.p.settings.for_stage("discover").public(),
                           "batch_size": self.p.policy["batch_packs"],
                           "recovery_depth": self.p.policy["recovery_max_depth"]}, 40)
        from .pipeline import selected_packs
        current_selection = sorted(p["pack_id"] for p in selected_packs(self.p.packs, self.p.policy["discovery_max_packs"]))
        if local["state"] != "success" or local["key"] != expected or local["selected"] != current_selection or not set(local["selected"]) <= selected:
            raise IncompleteStage("Local inputs/configuration changed or incomplete; run discover-local first")
        return local

    def merge(self):
        local = self.verify_local()
        from .pipeline import prompt
        candidates, registry = initial_candidates(local["schemas"])
        self.history = SchemaHistory(self.root, "merge", self.signature({"local_key": local["key"]}))
        self.history.snapshot("input", [c["schema"] for c in candidates], mapping={"lineage": candidates, "sources": registry})
        print(f"SCHEMA_HISTORY={self.history.root}", flush=True)
        self.p.save_state("merge-schema", "running")
        write_json(self.root / "merge-status.json", {"state": "running", "started_at": now(), "local_key": local["key"]})
        audit_paths = []
        write_jsonl(self.root / "unresolved.jsonl", [])
        try:
            for round_number in range(self.p.policy["merge_max_rounds"]):
                # Budget grouping never submits an oversized request; single overflows remain explicit.
                from .pipeline import group_batches
                make = lambda rows: merge_payload([c["schema"] for c in rows], self.p.profile)[0]
                by_module = round_number == 0 and len(local.get("modules", {})) > 1
                shift = round_number % max(1, len(candidates))
                reordered = candidates[shift:] + candidates[:shift]
                groups = [initial_candidates(rows)[0] for rows in local["modules"].values()] if by_module else [reordered]
                batches = [batch for group in groups for batch in group_batches(group, self.system, self.llm, make, self.p.policy["merge_batch_items"])]
                merged = []
                def merge_batch(batch, location, depth=0):
                    try:
                        payload = make(batch)
                        self.history_location, self.history_candidate = location, 0
                        value = self.call(payload)
                        audit_paths.append(self.root / "merge" / f"{self.signature(payload)}.json")
                        _, sources = merge_payload([c["schema"] for c in batch], self.p.profile)
                        composed = compose(batch, value, sources)
                        self.history.snapshot(f"{location}/result", [composed["schema"]],
                            mapping={"source_mapping": [m.model_dump() for m in value.source_mapping], "lineage": composed},
                            checkpoint=self.signature(payload))
                        return [composed]
                    except BudgetError:
                        if len(batch) < 2 or depth >= self.p.policy["recovery_max_depth"]:
                            raise
                        cut = len(batch) // 2
                        return (merge_batch(batch[:cut], split_location(location, 1), depth + 1) +
                                merge_batch(batch[cut:], split_location(location, 2), depth + 1))
                for i, batch in enumerate(batches):
                    print(f"MERGE round={round_number+1} batch={i+1}/{len(batches)} items={len(batch)} started={now()}", flush=True)
                    merged += merge_batch(batch, f"round-{round_number+1:03d}/batch-{i+1:03d}")
                    write_json(self.root / "merge-status.json", {"state": "running", "round": round_number+1,
                               "completed": i+1, "total": len(batches), "local_key": local["key"], "updated_at": now()})
                merged = coalesce(merged)
                self.history.snapshot(f"round-{round_number+1:03d}/result", [c["schema"] for c in merged], mapping=merged)
                write_json(self.root / "rounds" / f"{digest([local['key'], self.system, self.settings.public(), round_number])}.json",
                           {"round": round_number+1, "schemas": [c["schema"] for c in merged],
                            "lineage": merged, "local_key": local["key"], "format": MERGE_FORMAT})
                if len(merged) == 1:
                    value = Ontology.model_validate(merged[0]["schema"])
                    mapping = final_mapping(merged[0], registry)
                    audit = {"format": MERGE_FORMAT, "local_key": local["key"], "schema_hash": digest(value.model_dump()),
                             "sources": mapping, "checkpoints": sorted({p.relative_to(self.root).as_posix() for p in audit_paths}),
                             "review_status": "machine_generated_not_human_verified"}
                    write_json(self.root / "source-mapping.json", audit)
                    original_payload, _ = merge_payload(local["schemas"], self.p.profile)
                    mapped = MergeResponse(ontology=value, source_mapping=[
                        {"source_id": row["source_id"], "target_id": row["target_id"],
                         "action": "unresolved" if row["state"] == "unresolved" else "merge",
                         "reason": row.get("reason", "Composed across merge checkpoints")} for row in mapping])
                    findings = alignment_findings(mapped, original_payload)
                    write_jsonl(self.root / "unresolved.jsonl", findings)
                    write_json(self.root / "draft-schema.json", value.model_dump())
                    write_json(self.root / "draft-source-mapping.json", audit)
                    write_json(self.root / "schema.json", value.model_dump())
                    write_json(self.root / "provenance.json", {"schema_hash": digest(value.model_dump()),
                               "packs_hash": digest(self.p.packs), "prompt_hash": digest(prompt(self.p.policy, self.p.profile, "discover") + self.system),
                               "local_key": local["key"], "failed_tasks": 0, "selected_packs": len(local["selected"]),
                               "merge_settings": self.settings.public(), "merge_prompt_hash": digest(self.system),
                               "merge_format": MERGE_FORMAT, "source_mapping_hash": digest(audit),
                               "total_packs": len(self.p.packs), "review_status": "pending_ai_review", "alignment_issues": len(findings)})
                    write_json(self.root / "review-status.json", {"state": "stale", "reason": "merge generated a draft", "updated_at": now()})
                    write_json(self.root / "merge-status.json", {"state": "success", "quality_state": "pending_ai_review",
                               "alignment_issues": len(findings), "local_key": local["key"], "history": str(self.history.root), "ended_at": now()})
                    self.history.snapshot("final", [value.model_dump()], mapping=audit, alignment_issues=findings)
                    self.history.finish("success", quality_state="pending_ai_review")
                    print(f"SCHEMA={self.root / 'schema.json'} alignment_issues={len(findings)} review=pending; Next: intranet review-schema", flush=True)
                    self.p.save_state("merge-schema", "success", schema_hash=digest(value.model_dump()))
                    self.p.save_state("discover", "success", schema_hash=digest(value.model_dump()))
                    return value
                if len(merged) >= len(candidates) and not by_module and all(len(batch) == 1 for batch in batches):
                    raise IncompleteStage("Merge cannot reduce within output/input budget; module schemas retained. Adjust merge budget or partition scope")
                candidates = merged
            raise IncompleteStage("Merge rounds exhausted; checkpoints retained")
        except BaseException as exc:
            self.history.finish("partial", error=self.llm.safe_error(exc))
            write_json(self.root / "module-schemas.json", {"schemas": [c["schema"] for c in candidates],
                       "lineage": candidates, "note": "Unaligned candidates; not a complete global ontology"})
            write_json(self.root / "merge-status.json", {"state": "partial", "error": self.llm.safe_error(exc),
                       "local_key": local["key"], "history": str(self.history.root), "ended_at": now()})
            self.p.save_state("merge-schema", "partial", error=self.llm.safe_error(exc))
            raise

    def verify_merged(self):
        local = self.verify_local()
        status = self.root / "merge-status.json"
        provenance = self.root / "provenance.json"
        if not status.is_file() or read_json(status).get("state") != "success" or not provenance.is_file():
            raise IncompleteStage("Run merge-schema successfully first")
        record = read_json(provenance)
        if record.get("local_key") != local["key"] or record.get("merge_settings") != self.settings.public() or record.get("merge_prompt_hash") != digest(self.system):
            raise IncompleteStage("Merge inputs/settings changed; run merge-schema first")
        mapping_path = self.root / "draft-source-mapping.json"
        if record.get("merge_format") != MERGE_FORMAT or not mapping_path.is_file() or record.get("source_mapping_hash") != digest(read_json(mapping_path)):
            raise IncompleteStage("Merge provenance missing/changed; run merge-schema first")
        if record["schema_hash"] != digest(read_json(self.root / "draft-schema.json")):
            raise IncompleteStage("Merged schema modified; rerun merge-schema or explicitly select reviewed schema")
