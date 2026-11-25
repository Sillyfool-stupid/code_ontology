"""Explicit, finite output allowance diagnostics; never measures a theoretical hard cap."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from code_ontology.config import PROJECT, load_settings, validate_settings
from code_ontology.console import configure_console
from code_ontology.llm import LLM, BudgetError, RequestUnknown
from code_ontology.util import now, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="按显式有限档位测试输出额度；不扫描源码、不修改实验")
    parser.add_argument("--env-file")
    parser.add_argument("--limits", required=True, help="逗号分隔正整数，最多 8 档，例如 1024,2048,4096")
    parser.add_argument("--max-total", type=int, default=65536, help="所有档位申请输出额度之和上限")
    parser.add_argument("--without-cap", action="store_true", help="额外一次省略 max_tokens 的对照；服务默认额度未知")
    parser.add_argument("--dry-run", action="store_true", help="仅检查配置与计划，不发送请求")
    parser.add_argument("--out", type=Path, default=PROJECT / "outputs/model-limits")
    args = parser.parse_args(argv)
    try:
        limits = [int(value) for value in args.limits.split(",")]
        if not 1 <= len(limits) <= 8 or any(value <= 0 for value in limits) or sum(limits) > args.max_total:
            raise ValueError("Invalid finite limits")
        settings = replace(load_settings(args.env_file), retries=0, json_mode="off", stage_overrides={})
        for limit in limits:
            validate_settings(replace(settings, max_output_tokens=limit))
        if not settings.base_url or not settings.model:
            raise ValueError("Missing model connection")
    except (ValueError, OSError, TypeError):
        print("配置或额度无效；未发送请求。请检查档位、上下文窗口与模型连接。", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"仅预览：limits={limits} without_cap={args.without_cap} timeout={settings.timeout:g}s retries=0")
        return 0
    output = args.out.resolve() / (now().replace(":", "-") + "-" + uuid4().hex[:8])
    result = {"started_at": now(), "model": settings.model, "state": "running", "tiers": [],
              "planned": limits + ([None] if args.without_cap else []),
              "note": "接口接受额度与实际达到额度不同；usage、推理 token 和可见文本长度分别记录，非理论硬上限。"}
    write_json(output / "result.json", result)
    for index, limit in enumerate(result["planned"]):
        print(f"档位 {index+1}/{len(result['planned'])} allowance={limit}；诊断目录 {output}", flush=True)
        effective = replace(settings, max_output_tokens=limit or settings.max_output_tokens, send_max_tokens=limit is not None)
        llm = None
        tier = {"allowance": limit, "state": "running"}
        traces = output / f"tier-{index+1:02d}" / "llm_requests"
        try:
            llm = LLM(effective, output / f"tier-{index+1:02d}" / "diagnostic-state")
            llm.wait_messages = False
            llm._request([{"role": "system", "content": "Output plain text only. Do not summarize or abbreviate."},
                          {"role": "user", "content": "Write a very long numbered list of distinct short sentences about everyday objects. Continue until your output allowance is exhausted. No code or business source is required."}],
                         traces, f"tier-{index+1}")
            tier["state"] = "early_stop"
        except BudgetError:
            tier["state"] = "length_or_input_budget"
        except RequestUnknown:
            tier["state"] = "unknown"
        except Exception as error:
            tier.update(state="rejected_or_failed", error=llm.safe_error(error) if llm else "客户端初始化失败")
        finally:
            if llm:
                llm.close()
        files = sorted(traces.glob("*.json"))
        if files:
            record = read_json(files[-1])
            usage = record.get("usage")
            tier.update(finish_reason=record.get("finish_reason"), usage=usage,
                        elapsed_seconds=record.get("elapsed_seconds"), trace=str(files[-1]),
                        visible_characters=len(record.get("raw_content") or ""))
            if record.get("finish_reason") == "length":
                tier["state"] = "length"
            tier["reached_requested_usage"] = (usage.get("completion_tokens", 0) >= limit) if usage and limit else None
            tier["usage_missing"] = usage is None
        result["tiers"].append(tier)
        result["unexecuted"] = result["planned"][index+1:]
        write_json(output / "result.json", result)
        if tier["state"] == "unknown":
            break
    result.update(state="unknown" if result["tiers"][-1]["state"] == "unknown" else "finished", ended_at=now())
    write_json(output / "result.json", result)
    print(f"诊断结果：{output / 'result.json'}；真实输出硬上限尚不能据此确认。")
    return 1 if result["state"] == "unknown" or any(t["state"] == "rejected_or_failed" for t in result["tiers"]) else 0


if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
