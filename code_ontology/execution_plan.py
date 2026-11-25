"""Shared, side-effect-free command planning for staged executions."""
from __future__ import annotations

from pathlib import Path
import sys


def build_stage_command(stage, workspace, config, env_file, roots, options=None, python=None):
    """Build the exact argv used by a staged execution without starting it."""
    options = options or {}
    python = python or sys.executable
    args = [python, "-u", "-m", "code_ontology"]
    common = (["--config", str(config)] if config else []) + ["--env-file", str(env_file)]
    snapshot = workspace["snapshot"]
    run = roots.get("run")
    if stage in {"doctor", "connection"}:
        return args + ["doctor", "--out", str(roots["doctor"])] + common + (["--no-llm"] if stage == "doctor" else [])
    if stage in {"scan", "parse", "experiment"}:
        args += [stage, "--repo", workspace["repo"], "--out", workspace["out"]] + common
        if stage == "parse":
            args += ["--views", "ast,cfg,cpg"]
            args[3] = "code_ontology.workbench_runner"
            args += ["--expected-snapshot", workspace["snapshot"]]
        if stage == "experiment":
            args += ["--views", "ast,cfg,cpg", "--profile", "both", "--experiment", workspace["experiment"]]
        return args
    if stage == "prepare":
        return args + [stage, "--snapshot", snapshot, "--view", workspace["view"]] + common
    if stage in {"discover", "discover-local", "merge-schema", "review-schema", "finalize", "extract"}:
        args += [stage, "--snapshot", snapshot, "--view", workspace["view"], "--profile", workspace["profile"],
                 "--experiment", workspace["experiment"]] + common
        if stage == "extract":
            args += ["--defer-finalize", "--schema", str(Path(run) / "schema" / "schema.json"), "--require-current-schema"]
        elif options.get("update_schema"):
            args += ["--update-schema"]
        return args
    if stage == "validate":
        return args + [stage, "--snapshot", snapshot, "--view", workspace["view"], "--run-dir", str(run)] + common
    if stage == "export-rdf":
        return args + [stage, "--snapshot", snapshot, "--run-dir", str(run)] + common
    if stage == "evaluate":
        return args + [stage, "--run-dir", str(run), "--gold", workspace["gold"]] + common
    if stage == "report":
        return args + [stage, "--snapshot", snapshot, "--experiment", workspace["experiment"]] + common
    review = Path(roots["reviews"]) / f"{workspace['view']}-{workspace['profile']}.csv"
    if stage == "sample":
        return [python, "-u", "-m", "code_ontology.review", "sample", "--run-dir", str(run), "--out", str(review),
                "--per-kind", "10", "--seed", "42"]
    if stage == "summarize":
        return [python, "-u", "-m", "code_ontology.review", "summarize", "--reviews", str(review),
                "--out", str(review.with_suffix(".summary.json"))]
    raise ValueError("不支持的阶段")
