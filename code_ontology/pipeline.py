from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path
import json
import time

from .config import RESOURCE, Settings
from .llm import LLM, BudgetError, ModelFailure
from .models import Ontology, Extraction, supported_ids
from .prepare import evidence_valid
from .repository import verify_sources
from .util import canonical, digest, iter_jsonl, now, read_json, token_bound, unique, write_json, write_jsonl, write_text

SCHEMA_CONTRACT = """JSON shape (all top-level arrays required):
{"version":"0.1","profile":"","classes":[{"id":"c1","name":"Device","definition":"Equipment represented by source code","aliases":["Device"],"parents":[],"evidence_ids":["ev_..."]}],"relations":[{"id":"r1","name":"contains","definition":"Component membership","domain":["c1"],"range":["c1"],"evidence_ids":["ev_..."]}],"properties":[{"id":"p1","name":"identifier","definition":"Source identifier","domain":["c1"],"datatype":"string","unit":null,"evidence_ids":["ev_..."]}],"unresolved":[]}
datatype is string/number/integer/boolean. Parents/domain/range reference declared class IDs. Empty arrays are valid. No extra keys."""
EXTRACT_CONTRACT = """JSON shape (all top-level arrays required):
{"entities":[{"id":"e1","name":"SourceEntity","type_id":"ID from schema","scope":"code_definition","basis":"direct","evidence_ids":["ev_..."]}],"relations":[{"subject":"e1","relation_id":"ID from schema","object":"e1","basis":"direct","evidence_ids":["ev_..."]}],"attributes":[{"subject":"e1","property_id":"ID from schema","value":"example","unit":null,"basis":"direct","evidence_ids":["ev_..."]}],"rules":[{"name":"ThresholdRule","condition":"Original condition and configuration constraints","action":"Action implemented in code","scope":"code_definition","basis":"direct","evidence_ids":["ev_..."]}],"candidates":[{"reason":"Why the item cannot fit the fixed schema","payload":{}}]}
scope is code_definition/configuration_record/test/example; basis is direct/inferred. Declare all entity endpoints in the same response. Empty arrays are valid. No extra keys."""

# Keep the discovery prompt stable so a merge-only upgrade can reuse local manifests.
_MERGE_EXAMPLE = json.loads(SCHEMA_CONTRACT.splitlines()[1])
for _category in ("relations", "properties"):
    _MERGE_EXAMPLE[_category][0]["aliases"] = []
MERGE_CONTRACT = ("Return exactly this outer JSON shape (no extra keys):\n" + json.dumps({
    "ontology": _MERGE_EXAMPLE,
    "source_mapping": [{"source_id": "s_ID_FROM_INPUT", "target_id": "c1",
                        "action": "preserve", "reason": "Same concept; original label retained as alias"}]}) +
    "\nEvery input candidate item ID is a globally unique source_id. Map EVERY classes/relations/properties item exactly once. "
    "target_id references an output ontology item in the SAME category. action is preserve/merge/unresolved. "
    "Different sources may share a target only when equivalent. For unresolved use target_id:null and explain the conflict; never omit a source. "
    "Every output item must have a mapped source. Output IDs may differ from input IDs. "
    "Ontology datatype is string/number/integer/boolean; parents/domain/range reference its declared class IDs. "
    "Preserve mapped parents, relation direction/domain/range, property datatype/unit. "
    "An integer/number conflict may safely widen to number, never narrow number to integer; keep other datatype/unit conflicts separate. "
    "All three categories support aliases. "
    "Use separate items when meanings differ; do not force a merge just to reduce item counts.")
REVIEW_CONTRACT = ("Return one JSON object with exactly ontology, source_mapping, issues. ontology uses this shape:\n" +
    json.dumps(_MERGE_EXAMPLE) + "\nsource_mapping is an array of {source_id,target_id,action,reason}; "
    "map exactly every source_items.item.id once, using preserve/merge/unresolved (null target only for unresolved). "
    "issues is an array of {source_ids:[],target_ids:[],severity:info|warning|error,status:fixed|unresolved,description:string}. "
    "Return the COMPLETE corrected current_schema; keep existing IDs stable when editing definitions, names, references and types. "
    "Add a supported item and remap relevant sources when meanings must split. Never delete an item still used by another source mapping or reference. "
    "Only update mappings for source_items; class_context shows referenced source classes and their current target mappings. "
    "All source_items must remain mapped; every output item must have a mapped source. "
    "The original local schemas are model hypotheses, not ground truth. Usually preserve their direction/domain/range/parents, datatype and unit. "
    "If source code proves original metadata wrong, correct it and emit an explicit fixed issue listing the original source_id, "
    "its corrected target_id, and a concrete code-based explanation. The target must retain supporting evidence IDs of that source. "
    "Do not label an unfixed inconsistency fixed just to pass checks. For uncertainty report an unresolved issue; for a blocking contradiction use severity error. "
    "For genuinely different meanings keep separate items; integer may widen to number without loss. Evidence IDs must be supplied in current_schema/source_items/evidence_packs. "
    "No extra keys in schema or mapping; property datatype is string/number/integer/boolean.")


def prompt(policy: dict, profile: str, stage: str) -> str:
    directory = Path(policy["prompt_dir"]) if policy.get("prompt_dir") else RESOURCE / "prompts"
    common = (directory / "common" / "contract.txt").read_text(encoding="utf-8")
    special = (directory / profile / f"{stage}.txt").read_text(encoding="utf-8")
    language = policy.get("output_language", "mixed")
    guidance = {"mixed": "English and Chinese may coexist. Prefer original source names; translate only when reliable.",
                "en": "Use English readable labels and definitions, preserving original source identifiers.",
                "zh": "Use Chinese explanations where reliable, preserving original source identifiers and technical names."}[language]
    contract = {"extract": EXTRACT_CONTRACT, "merge": MERGE_CONTRACT, "review": REVIEW_CONTRACT}.get(stage, SCHEMA_CONTRACT)
    return common + "\n" + special + "\n" + guidance + "\n" + contract


def evidence_check(result: Ontology, allowed: set[str]):
    unknown = supported_ids(result) - allowed
    if unknown:
        raise ValueError(f"Unknown evidence IDs: {sorted(unknown)[:10]}")


def canonical_schema(schema: Ontology, profile: str, *, return_id_map=False):
    data = schema.model_dump()
    remap = {r["id"]: "c_" + digest([r["name"].casefold(), r["definition"]]) for r in data["classes"]}
    grouped = {}
    for row in data["classes"]:
        row["id"] = remap[row["id"]]
        row["parents"] = sorted({remap[x] for x in row["parents"]})
        key = row["id"]
        if key in grouped:
            for field in ("aliases", "parents", "evidence_ids"):
                grouped[key][field] = sorted(set(grouped[key][field] + row[field]))
        else:
            grouped[key] = row
    data["classes"] = sorted(grouped.values(), key=lambda x: x["id"])
    for category, prefix in (("relations", "r_"), ("properties", "p_")):
        grouped = {}
        for row in data[category]:
            old_id = row["id"]
            row["domain"] = sorted({remap[x] for x in row["domain"]})
            if "range" in row:
                row["range"] = sorted({remap[x] for x in row["range"]})
            row["aliases"] = sorted(set(row["aliases"]))
            row["id"] = prefix + digest({k: v for k, v in row.items() if k not in {"id", "evidence_ids", "aliases"}})
            remap[old_id] = row["id"]
            if row["id"] in grouped:
                grouped[row["id"]]["evidence_ids"] = sorted(set(grouped[row["id"]]["evidence_ids"] + row["evidence_ids"]))
                grouped[row["id"]]["aliases"] = sorted(set(grouped[row["id"]]["aliases"] + row["aliases"]))
            else:
                grouped[row["id"]] = row
        data[category] = sorted(grouped.values(), key=lambda x: x["id"])
    data["version"] = "0.1"; data["profile"] = profile
    value = Ontology.model_validate(data)
    return (value, remap) if return_id_map else value


def selected_packs(packs: list[dict], limit: int):
    groups = defaultdict(deque)
    for pack in packs:
        file = pack["source_refs"][0]["file"]
        groups[(str(Path(file).parent), pack["kind"])].append(pack)
    selected = []
    keys = sorted(groups)
    while keys and (not limit or len(selected) < limit):
        remaining = []
        for key in keys:
            if limit and len(selected) >= limit:
                break
            selected.append(groups[key].popleft())
            if groups[key]:
                remaining.append(key)
        keys = remaining
    return selected


def group_batches(rows: list, system: str, llm: LLM, make_payload, max_items: int):
    batch = []
    for row in rows:
        proposed = batch + [row]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": canonical(make_payload(proposed))}]
        if batch and (len(proposed) > max_items or not llm.fits(messages)):
            yield batch
            batch = [row]
        else:
            batch = proposed
        single = [{"role": "system", "content": system}, {"role": "user", "content": canonical(make_payload(batch))}]
        if not llm.fits(single):
            raise BudgetError("One task cannot fit the configured context; reduce pack_tokens or split the schema")
    if batch:
        yield batch


class Pipeline:
    def __init__(self, dest: Path, view: str, profile: str, experiment: str, settings: Settings, policy: dict):
        self.dest, self.view, self.profile, self.policy = dest, view, profile, policy
        if not experiment or any(x in experiment for x in ("/", "\\", "..")):
            raise ValueError("experiment must be a simple directory name")
        self.output = dest / "runs" / experiment / view / profile
        self.output.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._llm = None
        self.namespace = experiment
        self.started = time.monotonic()
        self.errors = []
        self.stage_stats = []
        self.packs = list(iter_jsonl(dest / "prepared" / view / "packs.jsonl"))
        if not self.packs:
            raise ValueError("No prepared evidence packs")
        verify_sources(dest)
        self.evidence = {}
        for pack in self.packs:
            if not evidence_valid(dest, pack):
                raise ValueError(f"Invalid source evidence: {pack['pack_id']}")
            for ref in pack["source_refs"]:
                self.evidence[ref["evidence_id"]] = ref
        previous_state = read_json(self.output / "run_manifest.json") if (self.output / "run_manifest.json").is_file() else {}
        self.state = {"created_at": now(), "view": view, "profile": profile, "experiment": experiment,
                      "settings": settings.public(), "policy": policy, "stages": {}, "source_snapshot": read_json(dest / "manifest.json")["snapshot_id"]}
        self.state["stages"] = previous_state.get("stages", {})
        from .parsing import snapshot_parser
        self.state["parser"] = snapshot_parser(dest)
        write_jsonl(self.output / "evidence.jsonl", self.evidence.values())
        # A previous success must not hide a failed rerun.
        self.save_state("execution", "running")

    @property
    def llm(self):
        if self._llm is None:
            self._llm = LLM(self.settings.for_stage("extract"), self.dest / "cache" / "llm")
        return self._llm

    def discover_local(self):
        from .stages import SchemaStages
        runner = SchemaStages(self, "discover")
        try:
            return runner.local()
        finally:
            runner.close()

    def merge_schema(self):
        from .stages import SchemaStages
        runner = SchemaStages(self, "merge")
        try:
            return runner.merge()
        finally:
            runner.close()

    def review_schema(self):
        from .schema_review import SchemaReview
        runner = SchemaReview(self)
        try:
            return runner.run()
        finally:
            runner.close()

    def save_state(self, stage, status, **detail):
        self.state["stages"][stage] = {"state": status, "updated_at": now(), **detail}
        write_json(self.output / "run_manifest.json", self.state)
        from .artifacts import index_experiment
        index_experiment(self.dest, self.namespace, self.view, self.profile, self.state)

    def execute_jobs(self, stage, jobs):
        """Bounded parallelism; every failed job gets a persistent, rerunnable task record."""
        results = []
        completed = 0
        from .events import emit
        emit("progress", stage=stage, completed=0, total=len(jobs), successful=0, failed=0, unknown=0, message="实例任务开始")
        print(f"STAGE {stage}: {len(jobs)} tasks", flush=True)
        iterator = iter(jobs)
        with ThreadPoolExecutor(max_workers=self.settings.for_stage("extract").concurrency) as pool:
            while window := list(islice(iterator, self.settings.for_stage("extract").concurrency)):
                futures = [(job, pool.submit(job["call"])) for job in window]
                for job, future in futures:
                    try:
                        value = future.result()
                        write_json(job["path"], value.model_dump())
                        job["path"].with_suffix(".failed.json").unlink(missing_ok=True)
                        results.append(value)
                    except ModelFailure as exc:
                        # Configuration/auth failures should not fan out to every evidence pack.
                        from .llm import RequestUnknown
                        if not isinstance(exc, RequestUnknown):
                            raise
                        error = {"stage": stage, "task_id": job["id"], "error": str(exc), "state": "unknown"}
                        self.errors.append(error)
                        job["path"].unlink(missing_ok=True)
                        write_json(job["path"].with_suffix(".failed.json"), error)
                    except Exception as e:
                        error = {"stage": stage, "task_id": job["id"], "error": self.llm.safe_error(e),
                                 "evidence_ids": job.get("evidence_ids", []), "state": "failed"}
                        self.errors.append(error)
                        job["path"].unlink(missing_ok=True)
                        write_json(job["path"].with_suffix(".failed.json"), error)
                    completed += 1
                    print(f"TASK {stage} {completed}/{len(jobs)} ({len(results)} successful)", flush=True)
                    emit("progress", stage=stage, completed=completed, total=len(jobs), successful=len(results),
                         failed=sum(e.get("state") == "failed" for e in self.errors),
                         unknown=sum(e.get("state") == "unknown" for e in self.errors),
                         remaining=len(jobs)-completed, message="实例任务处理结束")
        return results

    def discover(self):
        self.discover_local()
        return self.merge_schema()

    def fixed_schema(self, supplied: Path | None = None, update=False):
        path = supplied or self.output / "schema" / "schema.json"
        if not supplied and update:
            return self.discover()
        if path.is_file():
            provenance = self.output / "schema" / "provenance.json"
            if not supplied and provenance.is_file():
                previous = read_json(provenance)
                if previous.get("failed_tasks", 0) or previous.get("packs_hash") != digest(self.packs):
                    return self.discover()
            schema = Ontology.model_validate(read_json(path))
            evidence_check(schema, set(self.evidence))
            # A fixed schema is deliberately reused until --update-schema.
            if path.resolve() != (self.output / "schema" / "schema.json").resolve():
                write_json(self.output / "schema" / "schema.json", schema.model_dump())
            self.save_state("discover", "reused", schema_hash=digest(schema.model_dump()))
            return schema
        if supplied:
            raise ValueError(f"Schema not found: {supplied}")
        return self.discover()

    def extraction_schema(self, schema: Ontology, batch, system):
        """Use full schema when it fits; otherwise deterministic lexical subset + ancestor closure."""
        def payload(s):
            return {"stage": "extract", "schema": s.model_dump(), "packs": batch}
        messages = lambda s: [{"role": "system", "content": system}, {"role": "user", "content": canonical(payload(s))}]
        if self.llm.fits(messages(schema)):
            return schema, []
        text = canonical(batch).casefold()
        ranked = sorted(schema.classes, key=lambda c: (-sum(term.casefold() in text for term in [c.name] + c.aliases if term), c.id))
        classes = {c.id: c for c in schema.classes}
        selected = set()
        best = Ontology(classes=[], relations=[], properties=[])
        for concept in ranked:
            if not any(t.casefold() in text for t in [concept.name] + concept.aliases if t):
                continue
            proposed = selected | {concept.id}
            queue = list(proposed)
            while queue:
                cid = queue.pop()
                for parent in classes[cid].parents:
                    if parent not in proposed:
                        proposed.add(parent); queue.append(parent)
            sub = Ontology(version=schema.version, profile=schema.profile, classes=[classes[c] for c in sorted(proposed)],
                           relations=[r for r in schema.relations if set(r.domain + r.range) <= proposed],
                           properties=[p for p in schema.properties if set(p.domain) <= proposed])
            if self.llm.fits(messages(sub)):
                selected, best = proposed, sub
        if schema.classes and not best.classes:
            raise BudgetError("No relevant schema subset fits; increase context or narrow evidence packs")
        all_ids = {r.id for col in (schema.classes, schema.relations, schema.properties) for r in col}
        chosen = {r.id for col in (best.classes, best.relations, best.properties) for r in col}
        return best, sorted(all_ids - chosen)

    def extract(self, schema: Ontology, finalize=True):
        self.save_state("extract", "running")
        system = prompt(self.policy, self.profile, "extract")
        jobs, selection = [], []
        signature = self.extraction_fingerprint(schema)
        # Start with one pack to keep fixed-schema budget handling deterministic.
        for pack in self.packs:
            try:
                sub, omitted = self.extraction_schema(schema, [pack], system)
            except BudgetError as exc:
                failure = {"stage": "extract", "task_id": digest(pack["pack_id"]), "state": "failed",
                           "packs": [pack["pack_id"]], "evidence_ids": pack["evidence_ids"], "error": str(exc)}
                self.errors.append(failure)
                write_json(self.output / "extraction" / f"{failure['task_id']}.failed.json", failure)
                selection.append({"task_id": failure["task_id"], "packs": [pack["pack_id"]], "state": "budget_failed"})
                continue
            payload = {"stage": "extract", "schema": sub.model_dump(), "packs": [pack]}
            tid = digest([pack["pack_id"], digest(sub.model_dump())])
            selection.append({"task_id": tid, "packs": [pack["pack_id"]], "omitted_schema_ids": omitted})
            allowed = set(pack["evidence_ids"])

            def validator(result, allowed=allowed):
                found = {eid for category in (result.entities, result.relations, result.attributes, result.rules) for row in category for eid in row.evidence_ids}
                if found - allowed:
                    raise ValueError("Extraction references evidence outside this task: " + canonical(sorted(found - allowed)) +
                                     ". Use only this pack's evidence IDs: " + canonical(sorted(allowed)) +
                                     ". The fixed schema's evidence IDs describe type provenance, not evidence for this extraction. "
                                     "Keep an assertion only if the current pack's code supports it; otherwise remove it.")

            jobs.append({"id": tid, "evidence_ids": list(allowed), "path": self.output / "extraction" / f"{tid}.json",
                         "call": lambda payload=payload, validator=validator: self.extract_task(system, payload, validator)})
        write_json(self.output / "extraction" / "selection.json", selection)
        write_json(self.output / "extraction" / "manifest.json", {"state": "running", "key": signature,
                   "tasks": [j["id"] for j in jobs], "schema": schema.model_dump()})
        results = self.execute_jobs("extract", jobs)
        write_json(self.output / "extraction" / "manifest.json", {"state": "partial" if self.errors else "success", "key": signature,
                   "tasks": [j["id"] for j in jobs], "schema": schema.model_dump(), "errors": self.errors,
                   "llm": dict(self.llm.stats), "elapsed_seconds": round(time.monotonic()-self.started, 3)})
        if finalize:
            self.finalize(schema, results)
        self.save_state("extract", "partial" if self.errors else "success", tasks=len(jobs), successful=len(results))
        self.save_state("execution", "partial" if self.errors else "success")
        return not self.errors

    def extraction_fingerprint(self, schema):
        return digest({"packs": self.packs, "schema": schema.model_dump(), "prompt": prompt(self.policy, self.profile, "extract"),
                       "settings": self.settings.for_stage("extract").public(), "recovery": self.policy["recovery_max_depth"]})

    def extract_task(self, system, payload, validator, depth=0):
        recovery_key = digest({"payload": payload, "system": system, "settings": self.settings.for_stage("extract").public()})
        recovery = self.output / "extraction" / "recovery" / f"{recovery_key}.json"
        try:
            if recovery.is_file():
                raise BudgetError("Checkpoint requires split")
            return self.llm.call(system, payload, Extraction, self.output / "llm_requests", validator, self.namespace)
        except BudgetError:
            write_json(recovery, {"state": "needs_split", "key": recovery_key})
            code = payload["packs"][0]["code"]
            if depth >= self.policy["recovery_max_depth"] or len(code) < 80:
                raise
            cut = code.find("\n", len(code)//2)
            cut = cut+1 if 0 <= cut < len(code)-1 else len(code)//2
            combined = {k: [] for k in ("entities", "relations", "attributes", "rules", "candidates")}
            for index, fragment in enumerate((code[:cut], code[cut:])):
                pack = {**payload["packs"][0], "code": fragment, "context": {},
                        "fragment_of": payload["packs"][0]["pack_id"]}
                result = self.extract_task(system, {**payload, "packs": [pack]}, validator, depth+1).model_dump()
                remap = {r["id"]: f"part{index}_{r['id']}" for r in result["entities"]}
                for row in result["entities"]:
                    row["id"] = remap[row["id"]]
                for row in result["relations"]:
                    row["subject"] = remap.get(row["subject"], row["subject"])
                    row["object"] = remap.get(row["object"], row["object"])
                for row in result["attributes"]:
                    row["subject"] = remap.get(row["subject"], row["subject"])
                for key in combined:
                    combined[key] += result[key]
            return Extraction.model_validate(combined)

    def finalize_saved(self):
        path = self.output / "extraction" / "manifest.json"
        if not path.is_file():
            raise ValueError("Run extract first")
        manifest = read_json(path)
        schema = Ontology.model_validate(manifest["schema"])
        if manifest["state"] != "success" or manifest["key"] != self.extraction_fingerprint(schema):
            raise ValueError("Extraction incomplete or configuration changed; rerun extract")
        if read_json(self.output / "schema/schema.json") != schema.model_dump():
            raise ValueError("Fixed schema changed since extraction; rerun extract")
        self.stage_stats.append(manifest.get("llm", {}))
        results = [Extraction.model_validate(read_json(self.output / "extraction" / f"{tid}.json")) for tid in manifest["tasks"]]
        self.finalize(schema, results)
        metrics_path = self.output / "metrics.json"
        metrics = read_json(metrics_path)
        metrics["elapsed_seconds"] += manifest.get("elapsed_seconds", 0)
        metrics["elapsed_note"] = "Independent extract + finalize duration; upstream stages recorded separately"
        write_json(metrics_path, metrics)
        report_run(self.output)
        self.save_state("finalize", "success")
        self.save_state("execution", "success")

    def finalize(self, schema: Ontology, results: list[Extraction]):
        classes = {c.id: c for c in schema.classes}
        relations = {r.id: r for r in schema.relations}
        properties = {p.id: p for p in schema.properties}
        final = {k: [] for k in ("entities", "relations", "attributes", "rules", "candidates")}

        def reject(reason, payload):
            final["candidates"].append({"reason": reason, "payload": payload})

        def isa(cid, targets):
            visited = set(); queue = [cid]
            while queue:
                item = queue.pop()
                if item in targets:
                    return True
                if item not in visited and item in classes:
                    visited.add(item); queue.extend(classes[item].parents)
            return False

        for result in results:
            local, remap = {}, {}
            duplicated = {e.id for e in result.entities if sum(x.id == e.id for x in result.entities) > 1}
            for entity in result.entities:
                row = entity.model_dump()
                if entity.id in duplicated or entity.type_id not in classes:
                    reject("duplicate_entity_id_or_unknown_type", row); continue
                # Avoid merging same-named entities from different source files.
                origins = sorted({self.evidence[e]["file"] for e in entity.evidence_ids})
                eid = "entity_" + digest([entity.name.casefold(), entity.type_id, entity.scope, origins])
                remap[entity.id] = eid; local[entity.id] = entity
                row["id"] = eid
                final["entities"].append(row)
            for relation in result.relations:
                row = relation.model_dump()
                spec = relations.get(relation.relation_id)
                if not spec or relation.subject not in local or relation.object not in local:
                    reject("unknown_relation_or_endpoint", row); continue
                if not isa(local[relation.subject].type_id, spec.domain) or not isa(local[relation.object].type_id, spec.range):
                    reject("relation_domain_range_mismatch", row); continue
                row["subject"] = remap[relation.subject]; row["object"] = remap[relation.object]
                final["relations"].append(row)
            for attribute in result.attributes:
                row = attribute.model_dump(); spec = properties.get(attribute.property_id)
                if not spec or attribute.subject not in local or not isa(local[attribute.subject].type_id, spec.domain):
                    reject("unknown_property_or_domain", row); continue
                expected = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}[spec.datatype]
                if type(attribute.value) not in expected or (spec.unit and attribute.unit != spec.unit):
                    reject("property_datatype_or_unit_mismatch", row); continue
                row["subject"] = remap[attribute.subject]
                final["attributes"].append(row)
            for rule in result.rules:
                final["rules"].append(rule.model_dump())
            final["candidates"].extend(c.model_dump() for c in result.candidates)
        conflicts = []
        for category, rows in final.items():
            if category == "candidates":
                final[category] = list(unique(rows)); continue
            grouped = {}
            for row in rows:
                base = {k: v for k, v in row.items() if k != "evidence_ids"}
                key = canonical(base)
                if key not in grouped:
                    grouped[key] = row
                else:
                    grouped[key]["evidence_ids"] = sorted(set(grouped[key]["evidence_ids"] + row["evidence_ids"]))
            final[category] = list(grouped.values())
        values = defaultdict(set)
        for a in final["attributes"]:
            values[(a["subject"], a["property_id"])].add(canonical([a["value"], a["unit"]]))
        for (subject, prop), vals in values.items():
            if len(vals) > 1:
                conflicts.append({"subject": subject, "property_id": prop, "values": sorted(vals), "status": "preserved_for_review"})
        for category, rows in final.items():
            write_jsonl(self.output / "final" / f"{category}.jsonl", rows)
        write_jsonl(self.output / "final" / "conflicts.jsonl", conflicts)
        write_json(self.output / "final" / "schema.json", schema.model_dump())
        assertions = sum(len(final[k]) for k in ("entities", "relations", "attributes", "rules"))
        metrics = {"status": "partial" if self.errors else "success", "human_verified": False,
                   "schema_scope": read_json(self.output / "schema-scope.json") if (self.output / "schema-scope.json").is_file() else {"review_state": "direct_pipeline_or_explicit_schema"},
                   "parser": self.state["parser"],
                   "view": self.view, "profile": self.profile, "schema_classes": len(schema.classes),
                   "schema_relations": len(schema.relations), "schema_properties": len(schema.properties),
                   **{k: len(v) for k, v in final.items()}, "conflicts": len(conflicts),
                   "prepared_packs": len(self.packs), "failures": len(self.errors),
                   "evidence_locatable_rate": 1.0 if assertions else None,
                   "evidence_note": "Source location validity only; semantic support still needs human review",
                   "elapsed_seconds": round(time.monotonic() - self.started, 3),
                   "llm": {key: sum(s.get(key, 0) for s in self.stage_stats + ([self._llm.stats] if self._llm else []))
                           for key in ("requests", "cache_hits", "retries", "repairs", "prompt_tokens", "completion_tokens")}}
        write_json(self.output / "metrics.json", metrics)
        write_jsonl(self.output / "failures.jsonl", self.errors)
        report_run(self.output)

    def close(self):
        if self._llm is not None:
            self._llm.close()


def report_run(output: Path):
    metrics = read_json(output / "metrics.json")
    schema = read_json(output / "final" / "schema.json")
    text = ["# 业务本体抽取结果", "", f"视图：{metrics['view']}；模式：{metrics['profile']}；状态：{metrics['status']}", "",
            "机器生成，尚未人工核验。证据可定位不代表业务解释已正确。", "", "## 统计", "",
            "```json", json.dumps(metrics, ensure_ascii=False, indent=2), "```", "", "## 概念", "",
            "| 名称 | 定义 | 证据 |", "|---|---|---|"]
    clean = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    text += [f"| {clean(c['name'])} | {clean(c['definition'])} | {', '.join(c['evidence_ids'])} |" for c in schema["classes"]]
    text += ["", "## 追溯", "", "- schema/schema.json：固定 schema；schema/selection.json：发现范围。",
             "- schema/draft-schema.json：原始合并草稿；schema/review-report.json：AI 审查记录。",
             "- schema-scope.json：Schema 选择及实际 AI 审查状态；合并后可直接抽取。",
             "- extraction/selection.json：实例任务与被省略的 schema 项。",
             "- evidence.jsonl：证据到源码快照的路径和行号。",
             "- llm_requests/：渲染后的请求、原始回答与实际参数。",
             "- final/candidates.jsonl、final/conflicts.jsonl：需要复核的内容。",
             "- failures.jsonl：未完成任务；重新运行可复用成功请求缓存。", ""]
    write_text(output / "report.md", "\n".join(text))


def comparison(dest: Path, experiment: str):
    from .parsing import snapshot_parser
    parser = snapshot_parser(dest)
    root = dest / "runs" / experiment
    reports = []
    for path in sorted(root.glob("*/*/metrics.json")):
        row = read_json(path)
        manifest_path = path.parent / "run_manifest.json"
        if manifest_path.is_file():
            state = read_json(manifest_path).get("stages", {}).get("execution", {})
            if state.get("state") in {"failed", "running"}:
                row["status"] = state["state"]
                row["previous_outputs_only"] = True
                row["latest_attempt"] = state
        reports.append(row)
    rows = ["# AST 两组基线对比" if parser["backend"] == "tree-sitter" else "# 六组基线对比", "", "解析后端：" + parser["backend"], "", *parser["limitations"], "", "概念多不等于质量高；缺失组合与失败应单独检查。", "",
            "| 视图 | 模式 | 状态 | 概念 | 实体 | 关系 | 属性 | 规则 | 候选 | 失败 | 请求 | 缓存 |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    present = set()
    for r in reports:
        present.add((r["view"], r["profile"]))
        rows.append("| " + " | ".join(str(v) for v in [r["view"], r["profile"], r["status"], r["schema_classes"],
                    r["entities"], r["relations"], r["attributes"], r["rules"], r["candidates"], r["failures"],
                    r["llm"]["requests"], r["llm"]["cache_hits"]]) + " |")
    missing = [(v, p) for v in parser["available_views"] for p in ("general", "maintenance") if (v, p) not in present]
    failures = [read_json(p) for p in sorted(root.glob("*/*/execution.failed.json"))]
    rows += ["", "未完成组合：" + (canonical(missing) if missing else "无"), "",
             "后续人工检查：业务意义、同义归并、领域偏离、条件遗漏、测试/现场混淆、源码是否真正支持断言。", ""]
    if failures:
        rows += ["", "最新失败记录：", "", "```json", json.dumps(failures, ensure_ascii=False, indent=2), "```", ""]
    write_text(root / "comparison.md", "\n".join(rows))
    write_json(root / "comparison.json", {"runs": reports, "missing": missing, "failures": failures, "parser": parser})
    return root / "comparison.md"
