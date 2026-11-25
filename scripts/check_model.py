"""Probe the configured model without scanning code or initializing an experiment."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, StrictBool

from code_ontology.config import PROJECT, load_settings, validate_settings
from code_ontology.console import configure_console
from code_ontology.llm import LLM, RequestUnknown
from code_ontology.util import now, write_json


class Probe(BaseModel):
    ok: StrictBool


def main(argv=None):
    parser = argparse.ArgumentParser(description="测试 .env 中配置的真实模型，不依赖 Joern 或实验会话")
    parser.add_argument("--env-file", help="环境文件；默认读取工具项目根目录的 .env")
    parser.add_argument("--stage", choices=("global", "discover", "merge", "review", "extract"), default="global",
                        help="选择阶段参数，默认 global；连接和模型仍共用")
    parser.add_argument("--timeout", type=float, help="覆盖单次请求超时秒数")
    parser.add_argument("--out", type=Path, default=PROJECT / "outputs" / "model-check",
                        help="诊断输出根目录；每次创建独立子目录")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.env_file)
        if args.stage != "global":
            settings = settings.for_stage(args.stage)
        # Connectivity checks must not repeatedly submit a failing request.
        settings = replace(settings, retries=0, stage_overrides={})
        if args.timeout is not None:
            settings = replace(settings, timeout=args.timeout)
        validate_settings(settings)
        if not settings.base_url or not settings.model:
            raise ValueError("missing connection settings")
    except (ValueError, OSError):
        print("配置无效：请检查环境文件、OPENAI_BASE_URL、OPENAI_MODEL 和 LLM 参数；未发送请求。", file=sys.stderr)
        return 2

    identifier = now().replace(":", "-") + "-" + uuid4().hex[:8]
    output = args.out.resolve() / identifier
    started = time.monotonic()
    result = {"created_at": now(), "stage": args.stage, "ok": False}
    llm = None
    try:
        llm = LLM(settings, output / "cache")
        public = settings.public()
        result.update(endpoint=public["base_url"], model=settings.model, timeout=settings.timeout)
        print(f"开始模型探测：stage={args.stage} timeout={settings.timeout:g}s", flush=True)
        print(f"诊断目录：{output}", flush=True)
        probe = llm.call('Return JSON only: {"ok":true}.', {"task": "connection_probe"},
                         Probe, output / "llm_requests", namespace=identifier)
        result.update(ok=probe.ok, json_mode_supported=llm.json_supported, stats=dict(llm.stats))
        if not probe.ok:
            result["error"] = "模型响应 ok=false，探测未通过。"
    except Exception as error:
        result["error"] = llm.safe_error(error) if llm else "模型客户端初始化失败，请检查端点及证书配置。"
        result["state"] = "unknown" if isinstance(error, RequestUnknown) else "failed"
    finally:
        if llm:
            llm.close()
    result.update(ended_at=now(), elapsed_seconds=round(time.monotonic() - started, 3))
    write_json(output / "result.json", result)
    print(f"{'成功' if result['ok'] else '失败'}：耗时 {result['elapsed_seconds']} 秒")
    if result.get("error"):
        print(result["error"])
    print(f"诊断结果：{output / 'result.json'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
