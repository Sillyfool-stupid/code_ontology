from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError
from pydantic import ValidationError

from .config import Settings
from .util import canonical, digest, now, read_json, token_bound, write_json
from .events import emit


class BudgetError(ValueError):
    pass


class ModelFailure(RuntimeError):
    pass


class RequestUnknown(ModelFailure):
    """The client stopped waiting; the server may still be computing."""
    pass


def parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Response must be one JSON object")
    return result


def trace_path(directory: Path, key: str, kind: str, attempt: int | None = None) -> Path:
    """Sort trace files by Beijing creation time, retaining nanoseconds and request identity."""
    instant = time.time_ns()
    local = datetime.fromtimestamp(instant // 1_000_000_000, timezone(timedelta(hours=8)))
    stamp = f"{local:%Y%m%d-%H%M%S}-{instant % 1_000_000_000:09d}-UTC08"
    suffix = f"-{attempt}" if attempt is not None else ""
    return directory / f"{stamp}-{key}-{kind}{suffix}.json"


class LLM:
    def __init__(self, settings: Settings, cache_dir: Path):
        if not settings.base_url or not settings.model:
            raise ValueError("Set OPENAI_BASE_URL and OPENAI_MODEL in .env (or environment)")
        from urllib.parse import urlsplit
        url = urlsplit(settings.base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query:
            raise ValueError("OPENAI_BASE_URL must be a plain HTTP(S) endpoint without embedded credentials or query")
        self.settings = settings
        self.directory = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(settings.concurrency)
        self.json_supported = settings.json_mode != "off"
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "repairs": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.stats.update(request_seconds=0.0, unknown_requests=0, truncated_requests=0, usage_missing=0)
        self.wait_messages = True
        self.request_guard = None
        self._http = None
        if settings.ca_bundle:
            import httpx
            import ssl
            self._http = httpx.Client(verify=ssl.create_default_context(cafile=settings.ca_bundle))
        options = {"base_url": settings.base_url, "api_key": settings.api_key or "unused",
                   "timeout": settings.timeout, "max_retries": 0}
        if self._http:
            options["http_client"] = self._http
        self.client = OpenAI(**options)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks(key TEXT PRIMARY KEY, state TEXT, updated TEXT, detail TEXT)")

    def db(self):
        return sqlite3.connect(self.directory / "tasks.sqlite", timeout=30)

    def state(self, key, state, detail=""):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?)", (key, state, now(), detail))

    def safe_error(self, error) -> str:
        text = str(error)
        if self.settings.api_key:
            text = text.replace(self.settings.api_key, "<redacted>")
        return text[:2500]

    def fits(self, messages) -> bool:
        return token_bound(messages) + self.settings.max_output_tokens + 512 <= self.settings.context_tokens

    def _request(self, messages, traces: Path, key: str, repair: bool = False):
        if not self.fits(messages):
            raise BudgetError("Request exceeds conservative context budget; decrease pack_tokens/batch_packs or enlarge LLM_CONTEXT_TOKENS")
        with self.semaphore:
            for attempt in range(self.settings.retries + 2):
                if self.request_guard:
                    self.request_guard(messages)
                params = {"model": self.settings.model, "messages": messages, "max_tokens": self.settings.max_output_tokens,
                          "temperature": self.settings.temperature}
                if not self.settings.send_max_tokens:
                    params.pop("max_tokens")
                if self.settings.thinking != "auto":
                    params["extra_body"] = {"thinking": {"type": self.settings.thinking}}
                    if self.settings.thinking == "enabled":
                        params.pop("temperature", None)
                if self.json_supported:
                    params["response_format"] = {"type": "json_object"}
                record = {"created_at": now(), "key": key, "repair": repair, "attempt": attempt,
                          "endpoint": self.settings.public()["base_url"], "parameters": params}
                path = trace_path(traces, key, "repair" if repair else "initial", attempt)
                write_json(path, record)
                started = time.monotonic()
                emit("request_started", request_id=key, attempt=attempt, repair=repair,
                     message="模型请求已提交", artifact=str(path))
                heartbeat = threading.Event()
                def progress():
                    while not heartbeat.wait(15):
                        if not self.wait_messages:
                            continue
                        print(f"WAIT model stage={key[:10]} started={record['created_at']} elapsed={time.monotonic()-started:.0f}s log={path}", flush=True)
                monitor = threading.Thread(target=progress, daemon=True)
                monitor.start()
                try:
                    with self.lock:
                        self.stats["requests"] += 1
                    response = self.client.chat.completions.create(**params)
                    choice = response.choices[0] if response.choices else None
                    text = choice.message.content if choice else None
                    record.update({"raw_content": text, "finish_reason": choice.finish_reason if choice else None,
                                   "usage": response.usage.model_dump() if response.usage else None,
                                   "response_model": response.model})
                    write_json(path, record)
                    if response.usage:
                        with self.lock:
                            self.stats["prompt_tokens"] += response.usage.prompt_tokens or 0
                            self.stats["completion_tokens"] += response.usage.completion_tokens or 0
                    else:
                        with self.lock:
                            self.stats["usage_missing"] += 1
                    if choice and choice.finish_reason == "length":
                        raise BudgetError("Model output truncated; split this task or increase output/context budget")
                    # Empty text is handled by the same single repair pass as malformed JSON.
                    return text or ""
                except APIStatusError as e:
                    message = self.safe_error(e)
                    record["error"] = message; write_json(path, record)
                    if e.status_code in {408, 504}:
                        record["state"] = "unknown"; write_json(path, record)
                        raise RequestUnknown(f"Gateway/request timeout ({e.status_code}); server state unknown. Retry explicitly after checking server") from e
                    # Downgrade only for an explicit response_format/JSON capability rejection.
                    unsupported = e.status_code in {400, 422} and re.search(r"response_format|json_object|json mode", message, re.I) and re.search(r"unsupported|not support|invalid|unknown", message, re.I)
                    if unsupported and self.settings.json_mode == "auto" and self.json_supported:
                        self.json_supported = False
                        continue
                    if e.status_code not in {408, 409, 429} and e.status_code < 500:
                        raise ModelFailure(f"Non-retryable API error ({e.status_code}); inspect request log") from e
                    if attempt >= self.settings.retries:
                        raise ModelFailure(f"API retries exhausted ({e.status_code}); inspect request log") from e
                except APITimeoutError as e:
                    record.update(error=self.safe_error(e), state="unknown", ended_at=now())
                    write_json(path, record)
                    raise RequestUnknown("Client timeout; server execution state unknown. Inspect logs before explicitly retrying") from e
                except APIConnectionError as e:
                    record["error"] = self.safe_error(e); write_json(path, record)
                    if attempt >= self.settings.retries:
                        raise ModelFailure("Connection retries exhausted; inspect request log") from e
                finally:
                    heartbeat.set()
                    monitor.join(timeout=1)
                    elapsed = round(time.monotonic() - started, 3)
                    record.update(ended_at=now(), elapsed_seconds=elapsed)
                    if "state" not in record:
                        record["state"] = "truncated" if record.get("finish_reason") == "length" else "failed" if record.get("error") else "responded"
                    write_json(path, record)
                    with self.lock:
                        self.stats["request_seconds"] += elapsed
                        self.stats["unknown_requests"] += record["state"] == "unknown"
                        self.stats["truncated_requests"] += record["state"] == "truncated"
                    emit("request_ended", request_id=key, state=record["state"], elapsed_seconds=elapsed,
                         usage=record.get("usage"), finish_reason=record.get("finish_reason"), artifact=str(path),
                         level="warning" if record["state"] in {"unknown", "failed", "truncated"} else "info",
                         message="模型请求结束；响应仍须通过契约校验")
                with self.lock:
                    self.stats["retries"] += 1
                time.sleep(min(20, 2 ** attempt) + random.random() / 4)
        raise ModelFailure("Request attempts exhausted")

    def call(self, system: str, payload: dict, model_type, traces: Path, validator=None, namespace: str = ""):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": canonical(payload)}]
        identity = {"messages": messages, "settings": self.settings.request_identity(), "contract": model_type.model_json_schema(), "namespace": namespace}
        key = digest(identity, 40)
        cache = self.directory / "responses" / f"{key}.json"

        def validate(value):
            result = model_type.model_validate(value)
            if validator:
                validator(result)
            return result

        # Old caches are accepted only if the exact original input/contract/namespace key
        # and effective response parameters match. Do not infer equivalence from names.
        if not cache.is_file():
            legacy_key = digest({**identity, "settings": self.settings.public()}, 40)
            legacy = self.directory / "responses" / f"{legacy_key}.json"
            if legacy.is_file():
                write_json(cache, validate(read_json(legacy)).model_dump())
        if cache.is_file():
            result = validate(read_json(cache))
            with self.lock:
                self.stats["cache_hits"] += 1
            write_json(trace_path(traces, key, "cache"), {"created_at": now(), "key": key, "cache": str(cache), "state": "success", "cached": True})
            self.state(key, "success", "cache_hit")
            emit("cache_hit", request_id=key, message="复用已校验响应")
            return result
        self.state(key, "running")
        try:
            text = self._request(messages, traces, key)
            try:
                result = validate(parse_json(text))
            except (ValueError, ValidationError) as e:
                with self.lock:
                    self.stats["repairs"] += 1
                correction = messages + [{"role": "assistant", "content": text},
                                         {"role": "user", "content": "Correct this JSON once. Preserve supported facts; remove unsupported facts. Validation error:\n" + str(e)[:1800]}]
                # If history will not fit, provide a compact error with the original task instead.
                if not self.fits(correction):
                    correction = messages + [{"role": "user", "content": "Previous response invalid. Return corrected JSON. " + str(e)[:1000]}]
                result = validate(parse_json(self._request(correction, traces, key, repair=True)))
            write_json(cache, result.model_dump())
            self.state(key, "success")
            return result
        except Exception as e:
            self.state(key, "failed", self.safe_error(e))
            raise

    def close(self):
        self.client.close()
