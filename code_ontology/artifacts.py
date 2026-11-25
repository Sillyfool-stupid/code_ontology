"""Experiment-first indexes over the unchanged snapshot/run layout."""
from pathlib import Path

from .util import now, read_json, write_json


def update_cost_coverage(schema_root, stage, total_prepared_packs, **details):
    path = Path(schema_root) / "cost-coverage.json"
    report = read_json(path) if path.is_file() else {"format_version": 1}
    report.update(updated_at=now(), total_prepared_packs=total_prepared_packs)
    report[stage] = details
    write_json(path, report)


def index_experiment(snapshot, experiment, view, profile, state):
    snapshot = Path(snapshot)
    run = snapshot / "runs" / experiment / view / profile
    entry = snapshot.parent / "experiments" / experiment / f"{view}-{profile}"
    paths = {
        "schema": run / "schema/schema.json", "schema_history": run / "schema/history",
        "review": run / "schema/review-report.json", "coverage": run / "schema/cost-coverage.json",
        "results": run / "final", "report": run / "report.md", "validation": run / "validation.json",
        "state": run / "run_manifest.json", "requests": run / "llm_requests",
        "failures": run / "failures.jsonl", "session": snapshot.parent / "sessions" / f"{experiment}.json",
        "logs": snapshot.parent / "sessions" / experiment,
    }
    scope_path = run / "schema-scope.json"
    provenance_path = run / "schema/provenance.json"
    scope = read_json(scope_path) if scope_path.is_file() else {}
    provenance = read_json(provenance_path) if provenance_path.is_file() else {}
    stages = state.get("stages", {})
    current_stage = next((name for name in reversed(stages) if name != "execution"),
                         "execution" if "execution" in stages else "")
    write_json(entry / "index.json", {"layout_version": 2, "updated_at": now(),
        "experiment": experiment, "view": view, "profile": profile, "snapshot": str(snapshot),
        "snapshot_id": state.get("source_snapshot", snapshot.name), "run": str(run),
        "overall_state": stages.get("execution", {}).get("state", "unknown"),
        "current_stage": current_stage, "review_state": scope.get("review_state", provenance.get("review_status", "not_available")),
        "stages": stages, "artifacts": {name: str(path) for name, path in paths.items()},
        "availability": {name: path.exists() for name, path in paths.items()}})
