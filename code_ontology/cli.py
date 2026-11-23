from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from .config import PROJECT, RESOURCE, load_policy, load_settings
from .console import configure_console
from .joern import executable, invoke, normalize, parse
from .llm import LLM
from .models import Ontology, Extraction
from .pipeline import Pipeline, comparison, report_run, supported_ids
from .prepare import prepare, evidence_valid
from .repository import snapshot, verify_sources
from .util import iter_jsonl, now, read_json, write_json, write_jsonl, write_text


def parser():
    root = argparse.ArgumentParser(description="Joern + LLM 代码业务本体抽取基线")
    sub = root.add_subparsers(dest="command", required=True)
    commands = {}
    for name in ("scan", "parse", "prepare", "discover", "extract", "run", "experiment", "doctor", "report", "validate", "evaluate", "import-graph"):
        p = sub.add_parser(name)
        p.add_argument("--env-file")
        p.add_argument("--config", help="YAML policy; unknown keys are rejected")
        commands[name] = p
    for name in ("scan", "parse", "run", "experiment"):
        p = commands[name]
        p.add_argument("--repo", type=Path, required=True)
        p.add_argument("--out", type=Path, help="Output root; actual snapshot is printed")
        p.add_argument("--include", action="append")
        p.add_argument("--exclude", action="append")
        p.add_argument("--max-files", type=int)
    for name in ("parse", "run", "experiment"):
        commands[name].add_argument("--views", help="Comma-separated ast,cfg,cpg")
        commands[name].add_argument("--force-parse", action="store_true")
    for name in ("prepare", "discover", "extract", "validate", "import-graph"):
        commands[name].add_argument("--snapshot", type=Path, required=True)
    for name in ("prepare", "discover", "extract", "validate"):
        commands[name].add_argument("--view", choices=["ast", "cfg", "cpg"], default="cpg")
    for name in ("discover", "extract", "run", "experiment"):
        p = commands[name]
        p.add_argument("--profile", choices=["general", "maintenance", "both"])
        p.add_argument("--experiment", default="baseline", help="Use a new ID for independent repeated experiments")
        p.add_argument("--update-schema", action="store_true")
        if name != "discover":
            p.add_argument("--schema", type=Path)
    commands["doctor"].add_argument("--no-llm", action="store_true")
    commands["doctor"].add_argument("--out", type=Path, default=PROJECT / "outputs" / "doctor")
    commands["report"].add_argument("--snapshot", type=Path)
    commands["report"].add_argument("--experiment", default="baseline")
    commands["report"].add_argument("--run-dir", type=Path)
    commands["validate"].add_argument("--run-dir", type=Path)
    commands["evaluate"].add_argument("--run-dir", type=Path, required=True)
    commands["evaluate"].add_argument("--gold", type=Path, required=True)
    commands["import-graph"].add_argument("--nodes", type=Path, required=True)
    commands["import-graph"].add_argument("--edges", type=Path, required=True)
    return root


def split_choice(text, defaults, allowed):
    values = text.split(",") if text else defaults
    if not values or not set(values) <= allowed:
        raise ValueError(f"Choose from {sorted(allowed)}")
    return list(dict.fromkeys(values))


def doctor(settings, output: Path, no_llm: bool):
    output = output.resolve()
    result = {"created_at": now(), "python": sys.version, "settings": settings.public(), "checks": {}}
    for name in ("c2cpg", "joern"):
        try:
            path = executable(settings, name)
            log = output / f"{name}.help.log"
            invoke([str(path), "--help"], output / "work", log, 60)
            help_text = log.read_text(encoding="utf-8", errors="replace")
            required = ["--compilation-database", "--include-comments"] if name == "c2cpg" else ["--script", "--param "]
            missing = [v for v in required if v not in help_text]
            if missing:
                raise ValueError(f"Required capabilities missing: {missing}; adapter targets Joern 4 / flatgraph")
            result["checks"][name] = {"ok": True, "executable": str(path)}
        except Exception as e:
            result["checks"][name] = {"ok": False, "error": str(e)}
    if not no_llm:
        llm = None
        try:
            class Probe(BaseModel):
                ok: bool
            llm = LLM(settings, output / "cache")
            probe = llm.call("Return JSON only: {\"ok\":true}.", {"task": "connection_probe"}, Probe, output / "llm_requests", namespace="doctor-" + now())
            result["checks"]["llm"] = {"ok": probe.ok, "json_mode_supported": llm.json_supported}
        except Exception as e:
            result["checks"]["llm"] = {"ok": False, "error": llm.safe_error(e) if llm else str(e)}
        finally:
            if llm:
                llm.close()
    result["ok"] = all(c["ok"] for c in result["checks"].values())
    write_json(output / "doctor.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def validate(dest: Path, view: str, run_dir: Path | None):
    verify_sources(dest)
    errors = []
    packs = list(iter_jsonl(dest / "prepared" / view / "packs.jsonl"))
    allowed = set()
    for pack in packs:
        if not evidence_valid(dest, pack):
            errors.append({"pack": pack["pack_id"], "error": "invalid_source_evidence"})
        allowed.update(pack["evidence_ids"])
    if run_dir:
        schema = Ontology.model_validate(read_json(run_dir / "final" / "schema.json"))
        if supported_ids(schema) - allowed:
            errors.append({"error": "unknown_schema_evidence"})
        data = {key: list(iter_jsonl(run_dir / "final" / f"{key}.jsonl")) for key in ("entities", "relations", "attributes", "rules", "candidates")}
        extraction = Extraction.model_validate(data)
        entity_ids = {e.id for e in extraction.entities}
        classes = {c.id for c in schema.classes}
        rids = {r.id for r in schema.relations}; pids = {p.id for p in schema.properties}
        for key in ("entities", "relations", "attributes", "rules"):
            for row in data[key]:
                if set(row["evidence_ids"]) - allowed:
                    errors.append({"kind": key, "error": "unknown_evidence"})
        if any(e.type_id not in classes for e in extraction.entities):
            errors.append({"error": "unknown_entity_type"})
        if any(r.subject not in entity_ids or r.object not in entity_ids or r.relation_id not in rids for r in extraction.relations):
            errors.append({"error": "unknown_relation_endpoint_or_type"})
        if any(a.subject not in entity_ids or a.property_id not in pids for a in extraction.attributes):
            errors.append({"error": "unknown_attribute_endpoint_or_type"})
    result = {"ok": not errors, "packs": len(packs), "errors": errors,
              "note": "Structural and source-location checks only, not semantic truth verification"}
    write_json((run_dir or dest / "prepared" / view) / "validation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def evaluate(run_dir: Path, gold_path: Path):
    schema = Ontology.model_validate(read_json(run_dir / "final" / "schema.json"))
    gold = read_json(gold_path)
    normalize_name = lambda text: "".join(text.casefold().split())
    found, missing, matched = [], [], set()
    for reference in gold["concepts"]:
        names = {normalize_name(n) for n in [reference["name"]] + reference.get("aliases", [])}
        candidates = [c for c in schema.classes if names & {normalize_name(n) for n in [c.name] + c.aliases}]
        (found if candidates else missing).append(reference["name"])
        matched.update(c.id for c in candidates)
    result = {"metric": "concept_alias_recall", "found": found, "missing": missing,
              "recall": len(found) / len(gold["concepts"]) if gold["concepts"] else None,
              "unmatched_predicted": [c.name for c in schema.classes if c.id not in matched],
              "manual_checks": gold.get("manual_checks", []) + gold.get("forbidden_claims", []),
              "warning": "Unmatched predictions require review; this is not assertion precision or semantic correctness"}
    write_json(run_dir / "evaluation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    configure_console()
    args = parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
        policy = load_policy(args.config)
        for key in ("include", "exclude", "max_files"):
            value = getattr(args, key, None)
            if value is not None:
                policy[key] = policy[key] + value if key == "exclude" else value
        if policy["max_files"] < 0:
            raise ValueError("max_files cannot be negative")
        command = args.command
        if command == "doctor":
            return doctor(settings, args.out, args.no_llm)
        if command == "report":
            if args.run_dir:
                report_run(args.run_dir.resolve()); print(args.run_dir.resolve() / "report.md")
            elif args.snapshot:
                print(comparison(args.snapshot.resolve(), args.experiment))
            else:
                raise ValueError("report requires --run-dir or --snapshot")
            return 0
        if command == "evaluate":
            return evaluate(args.run_dir, args.gold)
        if command in {"scan", "parse", "run", "experiment"}:
            dest = snapshot(args.repo, args.out, policy, settings.joern_home)
            print(f"SNAPSHOT={dest}", flush=True)
        else:
            dest = args.snapshot.resolve()
        if command == "scan":
            return 0
        if command == "import-graph":
            import ijson
            def rows(path):
                if path.suffix.lower() == ".jsonl":
                    yield from iter_jsonl(path)
                else:
                    with path.open("rb") as f:
                        yield from ijson.items(f, "item", use_float=True)
            verify_sources(dest)
            write_jsonl(dest / "raw" / "cpg" / "nodes.jsonl", rows(args.nodes))
            write_jsonl(dest / "raw" / "cpg" / "edges.jsonl", rows(args.edges))
            write_json(dest / "raw" / "cpg" / "export.json", {"imported": True})
            normalize(dest, policy["views"])
            print(dest / "normalized")
            return 0
        if command == "validate":
            return validate(dest, args.view, args.run_dir)
        views = split_choice(getattr(args, "views", None), policy["views"], {"ast", "cfg", "cpg"})
        if command in {"parse", "run", "experiment"}:
            parse(dest, settings, policy, views, args.force_parse)
            if command == "parse":
                print("Joern parse and view exports complete")
                return 0
        if command == "prepare":
            print(prepare(dest, args.view, policy)); return 0
        if command in {"discover", "extract"}:
            views = [args.view]
        for view in views:
            prepare(dest, view, policy)
        profile_arg = getattr(args, "profile", None)
        profiles = ["general", "maintenance"] if profile_arg == "both" else [profile_arg] if profile_arg else policy["profiles"]
        errors = []
        # Run most informative demo first while keeping a single shared parse.
        pairs = [(v, p) for v in reversed(views) for p in reversed(profiles)]
        for view, profile in pairs:
            print(f"RUN {view}/{profile}", flush=True)
            pipeline = None
            try:
                pipeline = Pipeline(dest, view, profile, args.experiment, settings, policy)
                schema = pipeline.fixed_schema(getattr(args, "schema", None), args.update_schema)
                if command != "discover":
                    if not pipeline.extract(schema):
                        errors.append(f"{view}/{profile}: partial")
                else:
                    pipeline.save_state("execution", "partial" if pipeline.errors else "success")
                    if pipeline.errors:
                        errors.append(f"{view}/{profile}: partial discovery")
                (pipeline.output / "execution.failed.json").unlink(missing_ok=True)
                print(f"OUTPUT={pipeline.output}", flush=True)
            except Exception as e:
                if pipeline:
                    pipeline.save_state("execution", "failed", error=pipeline.llm.safe_error(e))
                failure = {"view": view, "profile": profile, "state": "failed",
                           "error": pipeline.llm.safe_error(e) if pipeline else str(e), "updated_at": now()}
                write_json(dest / "runs" / args.experiment / view / profile / "execution.failed.json", failure)
                errors.append(f"{view}/{profile}: {e}")
                print(f"FAILED {view}/{profile}: {e}", file=sys.stderr, flush=True)
            finally:
                if pipeline:
                    pipeline.close()
        if command != "discover":
            print(comparison(dest, args.experiment))
        if errors:
            write_json(dest / "runs" / args.experiment / "errors.json", errors)
            return 1
        (dest / "runs" / args.experiment / "errors.json").unlink(missing_ok=True)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed tasks remain cached; rerun the same command.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
