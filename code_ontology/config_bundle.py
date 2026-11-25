"""Explicit YAML run manifests and step files; no implicit latest-file selection."""
from pathlib import Path

import yaml

STAGES = {"scan", "parse", "normalize", "prepare", "discover", "merge", "review", "extract", "finalize", "validate", "report"}
LLM_FIELDS = {"timeout", "retries", "concurrency", "temperature", "context_tokens", "max_output_tokens", "send_max_tokens", "json_mode", "thinking"}


def read_bundle(path):
    empty = {"policy": {}, "llm": {}, "stages": {}, "run": {}, "sources": {}}
    if not path:
        return empty
    target = Path(path).resolve()
    if target.is_dir():
        target = target / "run.yaml"
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML configuration must be a mapping")
    if "config_version" not in data:
        empty["policy"] = data
        empty["sources"] = {"policy." + key: str(target) for key in data}
        _paths(empty["policy"], target)
        return empty
    if data["config_version"] != 1 or set(data) - {"config_version", "run", "defaults", "steps"}:
        raise ValueError("Unsupported run configuration version or fields")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - {"policy", "llm"}:
        raise ValueError("defaults only supports policy and llm")
    result = empty
    _merge(result, defaults, target)
    run = data.get("run", {})
    if not isinstance(run, dict) or set(run) - {"repo", "out", "experiment", "view", "profile"}:
        raise ValueError("Unknown run fields")
    result["run"] = dict(run)
    for key in ("repo", "out"):
        if result["run"].get(key):
            result["run"][key] = str((target.parent / result["run"][key]).resolve())
    steps = data.get("steps", {})
    if not isinstance(steps, dict) or set(steps) - STAGES:
        raise ValueError("Unknown step configuration")
    for stage, reference in steps.items():
        if not isinstance(reference, str):
            raise ValueError("Each step must explicitly name a YAML file")
        file = (target.parent / reference).resolve()
        if file == target:
            raise ValueError("Configuration cannot reference itself")
        step = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(step, dict) or set(step) - {"policy", "llm"}:
            raise ValueError("Step file only supports policy and llm; nested includes are forbidden")
        _merge(result, {"policy": step.get("policy", {})}, file)
        if step.get("llm"):
            if stage not in {"discover", "merge", "review", "extract"}:
                raise ValueError("LLM overrides only apply to model stages")
            _check_llm(step["llm"])
            result["stages"][stage] = step["llm"]
            result["sources"].update({f"stages.{stage}.{key}": str(file) for key in step["llm"]})
    return result


def _paths(policy, file):
    for key in ("prompt_dir", "compile_commands"):
        if policy.get(key):
            policy[key] = str((file.parent / policy[key]).resolve())
    if policy.get("includes"):
        policy["includes"] = [str((file.parent / value).resolve()) for value in policy["includes"]]


def _check_llm(values):
    if not isinstance(values, dict) or set(values) - LLM_FIELDS:
        raise ValueError("Unknown LLM YAML fields; credentials and model connection belong in .env")


def _merge(result, values, file):
    policy, llm = dict(values.get("policy", {})), dict(values.get("llm", {}))
    _check_llm(llm)
    _paths(policy, file)
    result["policy"].update(policy)
    result["llm"].update(llm)
    for section, fields in (("policy", policy), ("llm", llm)):
        result["sources"].update({section + "." + key: str(file) for key in fields})
