from __future__ import annotations

import os
from dataclasses import dataclass, asdict
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
    json_mode: str = "auto"
    joern_home: str = ""
    ca_bundle: str = ""
    thinking: str = "auto"

    def public(self):
        data = asdict(self)
        data.pop("api_key")
        # Endpoint may contain embedded credentials. Never persist URL query or user info.
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(data["base_url"])
        data["base_url"] = urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, "", ""))
        return data


def load_settings(env_file: str | None = None) -> Settings:
    path = Path(env_file) if env_file else PROJECT / ".env"
    if env_file and not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    values = {**(dotenv_values(path) if path.is_file() else {}), **os.environ}
    mapping = {
        "base_url": ("OPENAI_BASE_URL", str), "api_key": ("OPENAI_API_KEY", str),
        "model": ("OPENAI_MODEL", str), "timeout": ("LLM_TIMEOUT_SECONDS", float),
        "retries": ("LLM_MAX_RETRIES", int), "concurrency": ("LLM_CONCURRENCY", int),
        "temperature": ("LLM_TEMPERATURE", float), "context_tokens": ("LLM_CONTEXT_TOKENS", int),
        "max_output_tokens": ("LLM_MAX_OUTPUT_TOKENS", int), "json_mode": ("LLM_JSON_MODE", str),
        "joern_home": ("JOERN_HOME", str), "ca_bundle": ("LLM_CA_BUNDLE", str),
        "thinking": ("LLM_THINKING", str),
    }
    result = Settings(**{k: cast(values[key]) for k, (key, cast) in mapping.items() if values.get(key) not in (None, "")})
    if not result.joern_home:
        import shutil
        found = shutil.which("joern.bat" if os.name == "nt" else "joern")
        if found:
            result.joern_home = str(Path(found).parent)
    if result.json_mode not in {"auto", "on", "off"}:
        raise ValueError("LLM_JSON_MODE must be auto, on or off")
    if result.thinking not in {"auto", "enabled", "disabled"}:
        raise ValueError("LLM_THINKING must be auto, enabled or disabled")
    if min(result.timeout, result.concurrency, result.max_output_tokens) <= 0 or result.retries < 0:
        raise ValueError("Invalid LLM limits")
    if result.context_tokens <= result.max_output_tokens + 1000:
        raise ValueError("Context must leave at least 1000 input tokens")
    return result


def load_policy(path: str | None = None) -> dict:
    data = yaml.safe_load((RESOURCE / "default.yaml").read_text(encoding="utf-8"))
    if path:
        supplied = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        unknown = set(supplied) - set(data)
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        data.update(supplied)
        for key in ("prompt_dir", "compile_commands"):
            if data.get(key):
                data[key] = str((Path(path).resolve().parent / data[key]).resolve())
    for key in ("pack_tokens", "graph_nodes_per_pack", "batch_packs", "merge_max_rounds", "joern_timeout", "max_file_bytes"):
        if not isinstance(data[key], int) or data[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("neighbor_limit", "discovery_max_packs", "max_files"):
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
    return data
