"""Workspace-owned step overrides, applied after the shared environment/YAML.

This layer is explicit to the web workbench; ordinary CLI precedence is unchanged.
Files use the existing step YAML sections, and never contain model credentials.
"""
from copy import deepcopy
import math
from pathlib import Path

import yaml

from .config import env_values, validate_settings
from .config_bundle import read_bundle
from .util import digest, within


# Each policy field has one editing home, including when a combined command uses it.
POLICY_STEPS = {
    "scan": ("include", "exclude", "max_files", "max_file_bytes", "encoding_fallback"),
    "parse": ("parser_backend", "includes", "defines", "compile_commands", "joern_timeout"),
    "prepare": ("pack_tokens", "neighbor_limit", "graph_nodes_per_pack"),
    "discover-local": ("discovery_max_packs", "batch_packs", "discovery_pass_count", "discovery_strategy", "output_language", "recovery_max_depth"),
    "merge-schema": ("merge_batch_items", "merge_max_rounds", "merge_pass_count"),
    "review-schema": ("review_batch_items", "review_max_rounds", "review_max_requests", "review_max_seconds", "review_max_tokens"),
    "extract": ("extract_batch_packs",),
}
MODEL_STEPS = {"discover-local": "discover", "merge-schema": "merge", "review-schema": "review", "extract": "extract"}
LLM_FIELDS = {
    "context_tokens": ("上下文预算（token）", "输入与预留输出须能放入模型窗口；修改可能重新分批。", 1),
    "max_output_tokens": ("输出预算（token）", "服务端也有容量上限；修改会改变请求缓存。", 1),
    "send_max_tokens": ("向服务端发送输出上限", "关闭后仍保留本地输出预算，不代表服务端无限输出。", None),
    "timeout": ("请求超时（秒）", "只影响等待；超时不证明服务端已经停止。", 0.001),
    "retries": ("请求重试次数", "0 表示不自动重试；unknown 状态仍须核查。", 0),
    "concurrency": ("并发请求数", "增加会提高服务端负载；合并轮次的串行依赖仍有效。", 1),
    "temperature": ("温度", "控制生成随机性；取值须符合所用服务要求。", 0),
    "json_mode": ("JSON 协议", "auto 为自动兼容，on/off 为显式选择。", None),
    "thinking": ("思考模式", "仅在服务端支持时生效。", None),
}
POLICY_HELP = {
    "include": ("纳入路径模式", "JSON 字符串数组；空数组使用默认扫描范围。"),
    "exclude": ("排除路径模式", "JSON 字符串数组；修改范围后从扫描生成新快照。"),
    "max_files": ("最多扫描文件数", "0 为不限制；这是范围上限，不是语义覆盖率。"),
    "max_file_bytes": ("单文件大小上限（字节）", "超过上限的文件会记录为跳过。"),
    "encoding_fallback": ("备用源码编码", "如 gb18030；影响生成的源码快照。"),
    "parser_backend": ("解析后端", "修改后需重新扫描/解析，已有快照不会转换后端。"),
    "includes": ("头文件搜索目录", "JSON 数组；自定义目录须为本机绝对路径。"),
    "defines": ("预处理宏", "JSON 字符串数组，例如 [\"DEBUG=1\"]。"),
    "compile_commands": ("编译数据库路径", "本机绝对路径；留空表示不使用。"),
    "joern_timeout": ("Joern 解析超时（秒）", "不影响模型请求超时。"),
    "pack_tokens": ("单个证据包预算", "修改后重新生成模型输入包，下游输入也会变化。"),
    "neighbor_limit": ("图邻居数量上限", "控制证据包的邻域范围，0 为不加入邻居。"),
    "graph_nodes_per_pack": ("每包图节点上限", "控制证据体积。"),
    "discovery_max_packs": ("局部发现采样上限", "0 为全部证据包；大于 0 为明确采样。"),
    "batch_packs": ("每次局部发现证据包数", "请求组合变化后，旧批次缓存可能不能复用。"),
    "discovery_pass_count": ("局部发现执行总轮数", "包含首次执行；用于有限补跑。"),
    "discovery_strategy": ("局部发现策略", "通常保留继承值；修改可能使局部结果失效。"),
    "output_language": ("输出语言（各模型步骤共享）", "mixed 允许中英文共存并保留源码标识符；修改会影响后续各模型步骤。"),
    "recovery_max_depth": ("截断恢复最大拆分深度（共享）", "各模型步骤共用的恢复限制；不是每个请求的重试次数。"),
    "merge_batch_items": ("每批合并片段数", "修改会影响合并分组；不会重跑未变化的局部请求。"),
    "merge_max_rounds": ("最大合并轮数", "不是无限上下文；达到上限仍可能 partial。"),
    "merge_pass_count": ("合并执行总轮数", "包含首次执行；与最大归并轮数不同。"),
    "review_batch_items": ("每批审查来源项数", "控制审查来源批量。"),
    "review_max_rounds": ("最大审查修正轮数", "AI 审查不代替人工语义评审。"),
    "review_max_requests": ("审查请求数上限", "0 表示此项不设限制。"),
    "review_max_seconds": ("审查累计时间上限（秒）", "0 表示此项不设限制。"),
    "review_max_tokens": ("审查累计 token 上限", "0 表示此项不设限制。"),
    "extract_batch_packs": ("每次实例抽取证据包数", "修改后重新执行抽取，再归并；不能直接归并旧清单。"),
}
CHOICES = {"parser_backend": ["auto", "joern", "tree-sitter"],
           "output_language": ["mixed", "en", "zh"],
           "discovery_strategy": ["auto", "legacy", "budget", "ast-01"],
           "json_mode": ["auto", "on", "off"], "thinking": ["auto", "enabled", "disabled"]}
NONNEGATIVE = {"max_files", "neighbor_limit", "discovery_max_packs", "review_max_requests", "review_max_seconds", "review_max_tokens"}
LISTS = {"include", "exclude", "includes", "defines"}
TEXT = {"encoding_fallback", "compile_commands"}


class StepConfigurations:
    def __init__(self, state, workspace):
        # Workspace IDs are server-generated; additionally guard database/path tampering.
        self.root = (Path(state) / "step-configs").resolve()
        self.directory = (self.root / workspace["id"]).resolve()
        if not within(self.directory, self.root) or self.directory == self.root:
            raise ValueError("无效工作区配置路径")
        self.workspace = workspace

    def path(self, stage):
        if stage not in POLICY_STEPS:
            raise ValueError("此步骤没有独立可编辑参数，请使用页面列出的关联步骤")
        path = self.directory / (stage + ".yaml")
        if not within(path, self.directory):
            raise ValueError("配置文件超出工作区")
        return path

    def validate(self, stage, data):
        self.path(stage)
        if not isinstance(data, dict) or set(data) - {"policy", "llm"}:
            raise ValueError("步骤配置仅支持 policy 和 llm")
        for section, allowed in (("policy", POLICY_STEPS[stage]), ("llm", LLM_FIELDS if stage in MODEL_STEPS else ())):
            values = data.get(section, {})
            if not isinstance(values, dict) or set(values) - set(allowed):
                raise ValueError(f"{stage} 的 {section} 含未知或其他步骤的字段")
            for key, value in values.items():
                if key in CHOICES:
                    if value not in CHOICES[key]:
                        raise ValueError(f"{key} 取值无效")
                elif key in LISTS:
                    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                        raise ValueError(f"{key} 必须为字符串数组")
                    if key == "includes" and any(not Path(x).is_absolute() for x in value):
                        raise ValueError("头文件目录必须为绝对路径")
                elif key in TEXT:
                    if not isinstance(value, str) or (key == "encoding_fallback" and not value.strip()):
                        raise ValueError(f"{key} 必须为有效字符串")
                    if key == "compile_commands" and value and not Path(value).is_absolute():
                        raise ValueError("编译数据库必须为绝对路径")
                elif key == "send_max_tokens":
                    if not isinstance(value, bool):
                        raise ValueError("send_max_tokens 必须为布尔值")
                else:
                    fractional = key in {"timeout", "temperature"}
                    minimum = LLM_FIELDS[key][2] if section == "llm" else 0 if key in NONNEGATIVE else 1
                    if isinstance(value, bool) or not isinstance(value, (int, float) if fractional else int) or not math.isfinite(value) or value < minimum:
                        raise ValueError(f"{key} 必须为不小于 {minimum} 的{'数值' if fractional else '整数'}")
        return {s: deepcopy(data.get(s, {})) for s in ("policy", "llm")}

    def read(self):
        result = {}
        for stage in POLICY_STEPS:
            file = self.path(stage)
            try:
                result[stage] = self.validate(stage, yaml.safe_load(file.read_text(encoding="utf-8")) or {}) if file.is_file() else {"policy": {}, "llm": {}}
            except yaml.YAMLError as error:
                raise ValueError(f"步骤配置 YAML 无效：{file}，请修复后重新读取") from error
        return result

    @staticmethod
    def apply(policy, settings, overrides):
        policy, settings = deepcopy(policy), deepcopy(settings)
        for stage, data in overrides.items():
            policy.update(data["policy"])
            if stage in MODEL_STEPS:
                settings.stage_overrides.setdefault(MODEL_STEPS[stage], {}).update(data["llm"])
        for stage in MODEL_STEPS.values():
            validate_settings(settings.for_stage(stage))
        return policy, settings

    def describe(self, base_policy, base_settings, sources):
        overrides = self.read()
        policy, settings = self.apply(base_policy, base_settings, overrides)
        env = env_values(self.workspace["env_file"])
        bundle = read_bundle(self.workspace["config"] or None)

        def llm_source(stage, key):
            suffix = {"timeout": "TIMEOUT_SECONDS", "retries": "MAX_RETRIES"}.get(key, key.upper())
            for variable in (f"LLM_{stage.upper()}_{suffix}",):
                if env.get(variable) not in (None, ""):
                    return sources["model"][variable]
            origin = bundle["sources"].get(f"stages.{stage}.{key}")
            if origin:
                return origin
            variable = "LLM_" + suffix
            if env.get(variable) not in (None, ""):
                return sources["model"][variable]
            return bundle["sources"].get("llm." + key, "内置默认")

        configs = {}
        for stage, keys in POLICY_STEPS.items():
            data, rows = overrides[stage], []
            for section, fields in (("policy", keys), ("llm", LLM_FIELDS if stage in MODEL_STEPS else ())):
                for key in fields:
                    inherited = base_policy[key] if section == "policy" else getattr(base_settings.for_stage(MODEL_STEPS[stage]), key)
                    value = policy[key] if section == "policy" else getattr(settings.for_stage(MODEL_STEPS[stage]), key)
                    label, help_text = POLICY_HELP[key] if section == "policy" else LLM_FIELDS[key][:2]
                    custom = key in data[section]
                    origin = sources["policy"].get(key, bundle["sources"].get("policy." + key, "内置默认")) if section == "policy" else llm_source(MODEL_STEPS[stage], key)
                    kind = "choice" if key in CHOICES else "list" if key in LISTS else "text" if key in TEXT else "bool" if key == "send_max_tokens" else "number"
                    rows.append({"section": section, "key": key, "label": label, "help": help_text, "type": kind,
                                 "choices": CHOICES.get(key, []), "value": value, "inherited": inherited,
                                 "custom": custom, "source": str(self.path(stage)) if custom else origin,
                                 "inherited_source": origin})
            configs[stage] = {"file": str(self.path(stage)), "overrides": data, "fields": rows,
                              "yaml": yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                              "revision": digest({"stage": stage, "overrides": data, "base": [base_policy, base_settings.public()]}),
                              "connection": settings.for_stage(MODEL_STEPS[stage]).public() if stage in MODEL_STEPS else None}
        return configs
