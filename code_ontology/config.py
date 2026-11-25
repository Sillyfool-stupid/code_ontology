from __future__ import annotations

import os
from dataclasses import dataclass, asdict, field, replace
import json
from pathlib import Path

import yaml
from dotenv import dotenv_values

RESOURCE = Path(__file__).parent / "resources"
PROJECT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    base_url: str = ""
    api_key: str = "unused"
    model: str = ""
    timeout: float = 180
    retries: int = 3
    concurrency: int = 2
    temperature: float = 0
    context_tokens: int = 16384
    max_output_tokens: int = 4096
    send_max_tokens: bool = True
    json_mode: str = "auto"
    joern_home: str = ""
    ca_bundle: str = ""
    thinking: str = "auto"
    review_base_url: str = ""
    review_api_key: str = ""
    review_model: str = ""
    review_ca_bundle: str = ""
    stage_overrides: dict = field(default_factory=dict)

    def for_stage(self, stage):
        connection = {}
        if stage == "review":
            connection = {key: value for key, value in {
                "base_url": self.review_base_url, "api_key": self.review_api_key,
                "model": self.review_model, "ca_bundle": self.review_ca_bundle,
            }.items() if value}
        values = {**connection, **self.stage_overrides.get(stage, {}), "stage_overrides": {},
                  "review_base_url": "", "review_api_key": "", "review_model": "", "review_ca_bundle": ""}
        # Apply the selected review connection after clearing helper fields.
        values.update(connection)
        result = replace(self, **values)
        validate_settings(result)
        return result

    def public(self):
        data = asdict(self)
        data.pop("api_key")
        data.pop("review_api_key")
        # Endpoint may contain embedded credentials. Never persist URL query or user info.
        from urllib.parse import urlsplit, urlunsplit
        for field in ("base_url", "review_base_url"):
            u = urlsplit(data[field])
            data[field] = urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, "", ""))
        return data

    def request_identity(self):
        """Response semantics and transport capacity, excluding local scheduling."""
        data = self.public()
        for key in ("timeout", "retries", "concurrency", "joern_home", "ca_bundle", "stage_overrides",
                    "review_base_url", "review_model", "review_ca_bundle"):
            data.pop(key, None)
        return data


def env_values(env_file=None):
    path = Path(env_file) if env_file else PROJECT / ".env"
    if env_file and not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    return {**(dotenv_values(path) if path.is_file() else {}), **os.environ}


def parse_bool(value):
    if value.lower() not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("Boolean configuration must be true/false, 1/0 or yes/no")
    return value.lower() in {"true", "1", "yes"}


def load_settings(env_file: str | None = None, policy_file: str | None = None) -> Settings:
    from .config_bundle import read_bundle
    bundle = read_bundle(policy_file)
    values = env_values(env_file)
    mapping = {
        "base_url": ("OPENAI_BASE_URL", str), "api_key": ("OPENAI_API_KEY", str),
        "model": ("OPENAI_MODEL", str), "timeout": ("LLM_TIMEOUT_SECONDS", float),
        "retries": ("LLM_MAX_RETRIES", int), "concurrency": ("LLM_CONCURRENCY", int),
        "temperature": ("LLM_TEMPERATURE", float), "context_tokens": ("LLM_CONTEXT_TOKENS", int),
        "max_output_tokens": ("LLM_MAX_OUTPUT_TOKENS", int), "json_mode": ("LLM_JSON_MODE", str),
        "joern_home": ("JOERN_HOME", str), "ca_bundle": ("LLM_CA_BUNDLE", str),
        "thinking": ("LLM_THINKING", str),
        "send_max_tokens": ("LLM_SEND_MAX_TOKENS", parse_bool),
        "review_base_url": ("OPENAI_REVIEW_BASE_URL", str),
        "review_api_key": ("OPENAI_REVIEW_API_KEY", str),
        "review_model": ("OPENAI_REVIEW_MODEL", str),
        "review_ca_bundle": ("LLM_REVIEW_CA_BUNDLE", str),
    }
    result = Settings(**{**bundle["llm"], **{k: cast(values[key]) for k, (key, cast) in mapping.items() if values.get(key) not in (None, "")}})
    for stage in ("discover", "merge", "review", "extract"):
        overrides = dict(bundle["stages"].get(stage, {}))
        for attr, suffix, cast in (("context_tokens", "CONTEXT_TOKENS", int), ("max_output_tokens", "MAX_OUTPUT_TOKENS", int),
                                   ("timeout", "TIMEOUT_SECONDS", float), ("retries", "MAX_RETRIES", int), ("concurrency", "CONCURRENCY", int),
                                   ("send_max_tokens", "SEND_MAX_TOKENS", parse_bool),
                                   ("temperature", "TEMPERATURE", float), ("json_mode", "JSON_MODE", str), ("thinking", "THINKING", str)):
            value = values.get(f"LLM_{stage.upper()}_{suffix}")
            if value not in (None, ""):
                overrides[attr] = cast(value)
        result.stage_overrides[stage] = overrides
    if not result.joern_home:
        import shutil
        found = shutil.which("joern.bat" if os.name == "nt" else "joern")
        if found:
            result.joern_home = str(Path(found).parent)
    validate_settings(result)
    for stage in result.stage_overrides:
        result.for_stage(stage)
    return result


def validate_settings(result):
    if result.json_mode not in {"auto", "on", "off"}:
        raise ValueError("LLM_JSON_MODE must be auto, on or off")
    if result.thinking not in {"auto", "enabled", "disabled"}:
        raise ValueError("LLM_THINKING must be auto, enabled or disabled")
    if min(result.timeout, result.concurrency, result.max_output_tokens) <= 0 or result.retries < 0:
        raise ValueError("Invalid LLM limits")
    if result.context_tokens <= result.max_output_tokens + 1000:
        raise ValueError("Context must leave at least 1000 input tokens")


POLICY_ENV = {
    "parser_backend": ("CO_PARSER", str), "views": ("CO_VIEW", lambda x: x.split(",")),
    "profiles": ("CO_PROFILE", lambda x: x.split(",")), "output_language": ("CO_OUTPUT_LANGUAGE", str),
    "include": ("CO_SCAN_INCLUDE", json.loads), "exclude": ("CO_SCAN_EXCLUDE", json.loads),
    "max_files": ("CO_SCAN_MAX_FILES", int), "max_file_bytes": ("CO_SCAN_MAX_FILE_BYTES", int),
    "includes": ("CO_INCLUDE_DIRS", json.loads), "defines": ("CO_DEFINES", json.loads),
    "compile_commands": ("CO_COMPILE_COMMANDS", str), "prompt_dir": ("CO_PROMPT_DIR", str),
    "joern_timeout": ("CO_PARSE_TIMEOUT_SECONDS", int), "pack_tokens": ("CO_PACK_BUDGET", int),
    "graph_nodes_per_pack": ("CO_GRAPH_NODES_PER_PACK", int), "neighbor_limit": ("CO_NEIGHBOR_LIMIT", int),
    "discovery_max_packs": ("CO_DISCOVERY_SAMPLE_LIMIT", int), "batch_packs": ("CO_DISCOVERY_BATCH_PACKS", int),
    "merge_batch_items": ("CO_MERGE_BATCH_ITEMS", int), "merge_max_rounds": ("CO_MERGE_MAX_ROUNDS", int),
    "recovery_max_depth": ("CO_RECOVERY_MAX_DEPTH", int),
    "review_batch_items": ("CO_REVIEW_BATCH_ITEMS", int), "review_max_rounds": ("CO_REVIEW_MAX_ROUNDS", int),
    "allow_unreviewed_schema": ("CO_ALLOW_UNREVIEWED_SCHEMA", parse_bool),
    "discovery_strategy": ("CO_DISCOVERY_STRATEGY", str),
    "review_max_requests": ("CO_REVIEW_MAX_REQUESTS", int),
    "review_max_seconds": ("CO_REVIEW_MAX_SECONDS", int),
    "review_max_tokens": ("CO_REVIEW_MAX_TOKENS", int),
}


def load_policy(path: str | None = None, env_file: str | None = None, frozen=False) -> dict:
    data = yaml.safe_load((RESOURCE / "default.yaml").read_text(encoding="utf-8"))
    if path:
        from .config_bundle import read_bundle
        supplied = read_bundle(path)["policy"]
        unknown = set(supplied) - set(data)
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        data.update(supplied)
    values = env_values(env_file)
    # Workbench jobs use an already resolved policy captured when submitted.
    if frozen or path and os.environ.get("CO_FROZEN_POLICY") == str(Path(path).resolve()):
        values = {}
    for field_name, (key, cast) in POLICY_ENV.items():
        if values.get(key) not in (None, ""):
            data[field_name] = cast(values[key])
            if field_name in {"prompt_dir", "compile_commands"}:
                base = Path(env_file).resolve().parent if env_file else PROJECT
                data[field_name] = str((base / data[field_name]).resolve())
    mode = values.get("CO_DISCOVERY_MODE")
    if mode not in (None, "", "full", "sample"):
        raise ValueError("CO_DISCOVERY_MODE must be full or sample")
    if mode == "full":
        data["discovery_max_packs"] = 0
    if data["output_language"] not in {"mixed", "en", "zh"}:
        raise ValueError("CO_OUTPUT_LANGUAGE must be mixed/en/zh")
    for key in ("pack_tokens", "graph_nodes_per_pack", "batch_packs", "merge_max_rounds", "merge_batch_items", "recovery_max_depth", "joern_timeout", "max_file_bytes", "review_batch_items", "review_max_rounds"):
        if not isinstance(data[key], int) or data[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("neighbor_limit", "discovery_max_packs", "max_files", "review_max_requests", "review_max_seconds", "review_max_tokens"):
        if not isinstance(data[key], int) or data[key] < 0:
            raise ValueError(f"{key} must be nonnegative")
    for key in ("include", "exclude", "includes", "defines", "views", "profiles"):
        if not isinstance(data[key], list) or not all(isinstance(x, str) for x in data[key]):
            raise ValueError(f"{key} must be a list of strings")
    for key, allowed in (("views", {"ast", "cfg", "cpg"}), ("profiles", {"general", "maintenance"})):
        if not data[key] or not set(data[key]) <= allowed:
            raise ValueError(f"Invalid {key}")
    if data["parser_backend"] not in {"auto", "joern", "tree-sitter"}:
        raise ValueError("parser_backend must be auto, joern or tree-sitter")
    if data["discovery_strategy"] not in {"auto", "legacy", "budget"}:
        raise ValueError("CO_DISCOVERY_STRATEGY must be auto/legacy/budget")
    return data


def configuration_sources(env_file=None, policy_file=None):
    """Names and origins only; never return values or secrets."""
    file = Path(env_file) if env_file else PROJECT / ".env"
    supplied = dotenv_values(file) if file.is_file() else {}
    from .config_bundle import read_bundle
    bundle = read_bundle(policy_file)
    yaml_values = bundle["policy"]
    def origin(key, fallback):
        if os.environ.get(key) not in (None, ""):
            return "system environment"
        if supplied.get(key) not in (None, ""):
            return str(file.resolve())
        return fallback
    policy = {field: origin(key, bundle["sources"].get("policy." + field, "built-in default"))
              for field, (key, _) in POLICY_ENV.items()}
    if env_values(env_file).get("CO_DISCOVERY_MODE") == "full":
        policy["discovery_max_packs"] = origin("CO_DISCOVERY_MODE", "built-in default") + " (full)"
    model = {key: origin(key, "inherited global/default") for key in set(supplied) | set(os.environ)
             if key.startswith(("LLM_", "OPENAI_"))}
    return {"policy": policy, "model": model, "yaml": bundle["sources"],
            "overrides": [key for key in policy if "policy." + key in bundle["sources"] and policy[key] != bundle["sources"]["policy." + key]]}
