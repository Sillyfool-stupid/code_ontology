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
{"version":"0.1","profile":"","classes":[{"id":"c1","name":"设备","definition":"业务定义","aliases":["Device"],"parents":[],"evidence_ids":["ev_..."]}],"relations":[{"id":"r1","name":"组成","definition":"业务含义","domain":["c1"],"range":["c1"],"evidence_ids":["ev_..."]}],"properties":[{"id":"p1","name":"标识","definition":"业务含义","domain":["c1"],"datatype":"string","unit":null,"evidence_ids":["ev_..."]}],"unresolved":[]}
datatype is string/number/integer/boolean. Parents/domain/range reference declared class IDs. Empty arrays are valid. No extra keys."""
EXTRACT_CONTRACT = """JSON shape (all top-level arrays required):
{"entities":[{"id":"e1","name":"业务实体","type_id":"ID from schema","scope":"code_definition","basis":"direct","evidence_ids":["ev_..."]}],"relations":[{"subject":"e1","relation_id":"ID from schema","object":"e1","basis":"direct","evidence_ids":["ev_..."]}],"attributes":[{"subject":"e1","property_id":"ID from schema","value":"example","unit":null,"basis":"direct","evidence_ids":["ev_..."]}],"rules":[{"name":"规则","condition":"原始条件及配置约束","action":"代码执行的动作","scope":"code_definition","basis":"direct","evidence_ids":["ev_..."]}],"candidates":[{"reason":"为何无法纳入 schema","payload":{}}]}
scope is code_definition/configuration_record/test/example; basis is direct/inferred. Declare all entity endpoints in the same response. Empty arrays are valid. No extra keys."""


def prompt(policy: dict, profile: str, stage: str) -> str:
    directory = Path(policy["prompt_dir"]) if policy.get("prompt_dir") else RESOURCE / "prompts"
    common = (directory / "common" / "contract.txt").read_text(encoding="utf-8")
    special = (directory / profile / f"{stage}.txt").read_text(encoding="utf-8")
    return common + "\n" + special + "\n" + (EXTRACT_CONTRACT if stage == "extract" else SCHEMA_CONTRACT)


def evidence_check(result: Ontology, allowed: set[str]):
    unknown = supported_ids(result) - allowed
    if unknown:
        raise ValueError(f"Unknown evidence IDs: {sorted(unknown)[:10]}")


def canonical_schema(schema: Ontology, profile: str) -> Ontology:
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
            row["domain"] = sorted({remap[x] for x in row["domain"]})
            if "range" in row:
                row["range"] = sorted({remap[x] for x in row["range"]})
            row["id"] = prefix + digest({k: v for k, v in row.items() if k not in {"id", "evidence_ids"}})
            if row["id"] in grouped:
                grouped[row["id"]]["evidence_ids"] = sorted(set(grouped[row["id"]]["evidence_ids"] + row["evidence_ids"]))
            else:
                grouped[row["id"]] = row
        data[category] = sorted(grouped.values(), key=lambda x: x["id"])
    data["version"] = "0.1"; data["profile"] = profile
    return Ontology.model_validate(data)


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
        self.llm = LLM(settings, dest / "cache" / "llm")
        self.namespace = experiment
        self.started = time.monotonic()
        self.errors = []
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
        self.state = {"created_at": now(), "view": view, "profile": profile, "experiment": experiment,
                      "settings": settings.public(), "policy": policy, "stages": {}, "source_snapshot": read_json(dest / "manifest.json")["snapshot_id"]}
        write_jsonl(self.output / "evidence.jsonl", self.evidence.values())
        # A previous success must not hide a failed rerun.
        self.save_state("execution", "running")

    def save_state(self, stage, status, **detail):
        self.state["stages"][stage] = {"state": status, "updated_at": now(), **detail}
        write_json(self.output / "run_manifest.json", self.state)

    def execute_jobs(self, stage, jobs):
        """Bounded parallelism; every failed job gets a persistent, rerunnable task record."""
        results = []
        completed = 0
        print(f"STAGE {stage}: {len(jobs)} tasks", flush=True)
        iterator = iter(jobs)
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            while window := list(islice(iterator, self.settings.concurrency)):
                futures = [(job, pool.submit(job["call"])) for job in window]
                for job, future in futures:
                    try:
                        value = future.result()
                        write_json(job["path"], value.model_dump())
                        job["path"].with_suffix(".failed.json").unlink(missing_ok=True)
                        results.append(value)
                    except ModelFailure:
                        # Configuration/auth failures should not fan out to every evidence pack.
                        raise
                    except Exception as e:
                        error = {"stage": stage, "task_id": job["id"], "error": self.llm.safe_error(e),
                                 "evidence_ids": job.get("evidence_ids", []), "state": "failed"}
                        self.errors.append(error)
                        job["path"].unlink(missing_ok=True)
                        write_json(job["path"].with_suffix(".failed.json"), error)
                    completed += 1
                    print(f"TASK {stage} {completed}/{len(jobs)} ({len(results)} successful)", flush=True)
        return results

    def discover(self):
        self.save_state("discover", "running")
        system = prompt(self.policy, self.profile, "discover")
        selected = selected_packs(self.packs, self.policy["discovery_max_packs"])
        selection = {"selected": [p["pack_id"] for p in selected],
                     "unselected": [p["pack_id"] for p in self.packs if p not in selected], "total": len(self.packs)}
        write_json(self.output / "schema" / "selection.json", selection)
        payload_for = lambda batch: {"stage": "discover", "profile": self.profile, "packs": batch}
        batches = list(group_batches(selected, system, self.llm, payload_for, self.policy["batch_packs"]))
        jobs = []
        for batch in batches:
            tid = digest([p["pack_id"] for p in batch])
            allowed = {e for p in batch for e in p["evidence_ids"]}
            payload = payload_for(batch)
            jobs.append({"id": tid, "evidence_ids": sorted(allowed), "path": self.output / "schema" / "local" / f"{tid}.json",
                         "call": lambda payload=payload, allowed=allowed: self.llm.call(system, payload, Ontology,
                         self.output / "llm_requests", lambda r: evidence_check(r, allowed), self.namespace)})
        local = self.execute_jobs("discover", jobs)
        if not local:
            self.save_state("discover", "failed", errors=self.errors)
            raise RuntimeError("All schema discovery tasks failed; inspect schema/local/*.failed.json")
        candidates = [canonical_schema(s, self.profile).model_dump() for s in local]
        candidates = list(unique(candidates))
        merge_system = prompt(self.policy, self.profile, "merge")
        merge_payload = lambda batch: {"stage": "merge", "profile": self.profile, "candidates": batch}
        # Always run at least one global merge, even for one local schema.
        for round_number in range(self.policy["merge_max_rounds"]):
            batches = list(group_batches(candidates, merge_system, self.llm, merge_payload, 10000))
            if len(candidates) > 1 and all(len(b) == 1 for b in batches):
                raise BudgetError("Schema merge cannot reduce within context budget. Increase context or reduce discovery scope")
            merged = []
            for idx, batch in enumerate(batches):
                allowed = {e for s in batch for e in supported_ids(Ontology.model_validate(s))}
                value = self.llm.call(merge_system, merge_payload(batch), Ontology, self.output / "llm_requests",
                                      lambda r: evidence_check(r, allowed), self.namespace)
                value = canonical_schema(value, self.profile)
                # Track source candidates omitted by the merge instead of silently losing coverage.
                source_names = {c["name"] for s in batch for c in s["classes"]}
                target_names = {c.name for c in value.classes} | {a for c in value.classes for a in c.aliases}
                write_json(self.output / "schema" / "merge" / f"{round_number}-{idx}.json", {
                    "schema": value.model_dump(), "unmatched_source_names": sorted(source_names - target_names)})
                merged.append(value.model_dump())
            candidates = merged
            if len(candidates) == 1:
                break
        if len(candidates) != 1:
            raise RuntimeError("Schema merge rounds exhausted; reduce scope or increase configured rounds")
        schema = Ontology.model_validate(candidates[0])
        write_json(self.output / "schema" / "schema.json", schema.model_dump())
        write_json(self.output / "schema" / "provenance.json", {
            "schema_hash": digest(schema.model_dump()), "prompt_hash": digest(system + merge_system),
            "packs_hash": digest(self.packs), "review_status": "machine_generated_not_human_verified",
            "local_tasks": len(jobs), "failed_tasks": len(self.errors), "selected_packs": len(selected), "total_packs": len(self.packs)})
        self.save_state("discover", "partial" if self.errors else "success", schema_hash=digest(schema.model_dump()))
        return schema

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

    def extract(self, schema: Ontology):
        self.save_state("extract", "running")
        system = prompt(self.policy, self.profile, "extract")
        jobs, selection = [], []
        # Start with one pack to keep fixed-schema budget handling deterministic.
        for pack in self.packs:
            sub, omitted = self.extraction_schema(schema, [pack], system)
            payload = {"stage": "extract", "schema": sub.model_dump(), "packs": [pack]}
            tid = digest([pack["pack_id"], digest(sub.model_dump())])
            selection.append({"task_id": tid, "packs": [pack["pack_id"]], "omitted_schema_ids": omitted})
            allowed = set(pack["evidence_ids"])

            def validator(result, allowed=allowed):
                found = {eid for category in (result.entities, result.relations, result.attributes, result.rules) for row in category for eid in row.evidence_ids}
                if found - allowed:
                    raise ValueError("Extraction references evidence outside this task")

            jobs.append({"id": tid, "evidence_ids": list(allowed), "path": self.output / "extraction" / f"{tid}.json",
                         "call": lambda payload=payload, validator=validator: self.llm.call(system, payload, Extraction,
                         self.output / "llm_requests", validator, self.namespace)})
        write_json(self.output / "extraction" / "selection.json", selection)
        results = self.execute_jobs("extract", jobs)
        self.finalize(schema, results)
        self.save_state("extract", "partial" if self.errors else "success", tasks=len(jobs), successful=len(results))
        self.save_state("execution", "partial" if self.errors else "success")
        return not self.errors

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
                   "view": self.view, "profile": self.profile, "schema_classes": len(schema.classes),
                   "schema_relations": len(schema.relations), "schema_properties": len(schema.properties),
                   **{k: len(v) for k, v in final.items()}, "conflicts": len(conflicts),
                   "prepared_packs": len(self.packs), "failures": len(self.errors),
                   "evidence_locatable_rate": 1.0 if assertions else None,
                   "evidence_note": "Source location validity only; semantic support still needs human review",
                   "elapsed_seconds": round(time.monotonic() - self.started, 3), "llm": dict(self.llm.stats)}
        write_json(self.output / "metrics.json", metrics)
        write_jsonl(self.output / "failures.jsonl", self.errors)
        report_run(self.output)

    def close(self):
        self.llm.close()


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
             "- extraction/selection.json：实例任务与被省略的 schema 项。",
             "- evidence.jsonl：证据到源码快照的路径和行号。",
             "- llm_requests/：渲染后的请求、原始回答与实际参数。",
             "- final/candidates.jsonl、final/conflicts.jsonl：需要复核的内容。",
             "- failures.jsonl：未完成任务；重新运行可复用成功请求缓存。", ""]
    write_text(output / "report.md", "\n".join(text))


def comparison(dest: Path, experiment: str):
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
    rows = ["# 六组基线对比", "", "概念多不等于质量高；缺失组合与失败应单独检查。", "",
            "| 视图 | 模式 | 状态 | 概念 | 实体 | 关系 | 属性 | 规则 | 候选 | 失败 | 请求 | 缓存 |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    present = set()
    for r in reports:
        present.add((r["view"], r["profile"]))
        rows.append("| " + " | ".join(str(v) for v in [r["view"], r["profile"], r["status"], r["schema_classes"],
                    r["entities"], r["relations"], r["attributes"], r["rules"], r["candidates"], r["failures"],
                    r["llm"]["requests"], r["llm"]["cache_hits"]]) + " |")
    missing = [(v, p) for v in ("ast", "cfg", "cpg") for p in ("general", "maintenance") if (v, p) not in present]
    failures = [read_json(p) for p in sorted(root.glob("*/*/execution.failed.json"))]
    rows += ["", "未完成组合：" + (canonical(missing) if missing else "无"), "",
             "后续人工检查：业务意义、同义归并、领域偏离、条件遗漏、测试/现场混淆、源码是否真正支持断言。", ""]
    if failures:
        rows += ["", "最新失败记录：", "", "```json", json.dumps(failures, ensure_ascii=False, indent=2), "```", ""]
    write_text(root / "comparison.md", "\n".join(rows))
    write_json(root / "comparison.json", {"runs": reports, "missing": missing, "failures": failures})
    return root / "comparison.md"
