"""Experiment-first indexes over the unchanged snapshot/run layout."""
from pathlib import Path

from .util import now, write_json


def index_experiment(snapshot, experiment, view, profile, state):
    snapshot = Path(snapshot)
    run = snapshot / "runs" / experiment / view / profile
    entry = snapshot.parent / "experiments" / experiment / f"{view}-{profile}"
    write_json(entry / "index.json", {"layout_version": 1, "updated_at": now(),
        "experiment": experiment, "view": view, "profile": profile, "snapshot": str(snapshot),
        "run": str(run), "stages": state.get("stages", {}), "artifacts": {
            name: str(run / relative) for name, relative in {
                "schema": "schema/schema.json", "review": "schema/review-report.json",
                "results": "final", "report": "report.md", "validation": "validation.json",
                "state": "run_manifest.json", "requests": "llm_requests"}.items()}})
