# AST-01：可选的 Schema 发现方案

日期：2026-11-26。原 `ast` 视图、prepared 包、`auto/legacy/budget` 策略和旧实验继续保留；默认行为不变。AST-01 是 AST 上的独立发现策略，不是新的解析后端，也不提供 CFG/CPG。

全部文档见 [文档导航](README.md)，完整阶段操作见 [使用指南](使用指南.md)。本页离线检查与历史测试记录不代表真实模型语义验收。

## 启用方式

保留现有 `.env`，在原来成功的命令上增加 `--discovery-strategy ast-01`，并换用独立实验名。例如在项目根目录的 Windows CMD 中：

```bat
call .venv\Scripts\activate.bat
python -m code_ontology discover-local --snapshot "实际快照路径" --view ast --profile general --experiment AST-01 --discovery-strategy ast-01
python -m code_ontology merge-schema --snapshot "实际快照路径" --view ast --profile general --experiment AST-01 --discovery-strategy ast-01
```

使用与基线相同的 profile（如 maintenance）、模型、提示词和预算。已有 prepared AST 包可直接复用，无需重新解析或 prepare；不要用旧实验名覆盖基线。AST-01 只影响局部 Schema 请求组织，固定 Schema 后的实例抽取流程不变。它仍遵守当前 full/sample 选包配置；需要全量时设 `CO_DISCOVERY_MODE=full`。

内网脚本和工作台通过现有配置入口启用：在**独立实验**的配置中设 `CO_DISCOVERY_STRATEGY=ast-01`、`CO_VIEW=ast`、`CO_EXPERIMENT=AST-01`，或 YAML `discovery_strategy: ast-01`。CLI 参数优先于环境配置。不启用 AST-01 时继续原方案；不要把 `CO_VIEW` 改成 `ast-01`。

## 改进内容与边界

- 模块内按文件和源码范围排列证据，同一文件中的类型/方法重叠更容易进入同一请求；包数与输入预算仍为硬上限，单包超限沿用已有拆分恢复。
- 对同一文件、同一源码 SHA、同一行范围且文本一致的重叠区间，使用请求内共享字符串。每个包的源码可由 segments 按顺序无分隔拼接还原；图上下文、包 ID、原始证据 ID 与来源位置继续保留。完全相同的图节点沿用原有共享机制。
- 只有缩小序列化输入时才采用重叠表示；跨文件、不同 SHA、不同行区间不按重叠共享。恢复片段、范围不完整、校验失败保留全文。相同文本的批内共享只是传输压缩，不表示概念等价。
- 能力说明、解析警告及图边/邻居列表完全相同时，也可使用共享引用；只有连同字典开销仍能节省空间时才启用，所有字段可递归还原。
- 模型输入明确要求：同义概念在当前响应内汇总证据，重复源码不生成独立概念；同名异义、不同角色、条件和单位分别保留，无法确认放入 unresolved。程序不靠名称强制归并。
- 原始 prepared 包未被修改或删除。不同请求没有共享字典，跨批次的同义候选仍由原合并流程处理；AST-01 不承诺消除全部 Schema 重复。

## 结果与验证

实验路径沿用 `快照/runs/AST-01/ast/<profile>/`。`schema/ast-01-input-report.json` 列出每批包 ID、原始 JSON 字节、实际计划请求 JSON 字节、共享项数与分段包数；包括协议说明的额外字节，输入很小时可能比原始输入更大。它只统计初始请求计划，不是 token、实际收费或语义质量报告。实际请求与恢复看 `llm_requests/`、`local-tasks.json`、`local-manifest.json`、`cost-coverage.json`。

AST-01 请求包含独立格式版本，局部清单与请求缓存单独校验；同配置续跑可复用成功任务，改策略或格式不会静默复用不匹配的局部结果。`auto` 仍沿用已有实验策略，新实验仍默认 budget。

本次验证使用真实 Tree-sitter grammar 与本地模拟模型，覆盖重叠/交叉范围还原、不同位置和冲突隔离、原输入不变、预算组批、检查点复用、合并及旧策略兼容。没有运行真实模型 A/B，也没有改写旧实验产物。Schema 重复率与语义完整性需用相同输入和真实模型对照，按人工评审规程确认。

```bat
python -m unittest tests.test_ast_discovery -v
python -m unittest discover -s tests -v
```

只读离线对比入口（不连接模型，不修改快照）：

```bat
python scripts/check_ast_01.py --snapshot "实际快照路径" --out "outputs/research/AST-01-input-check.json" --batch-packs 8
```

2026-11-26 已对 `outputs/31f709e34ce66699f7d0` 的49个包运行此检查；使用内置 general 提示词、32768上下文预算、4096输出预留、批量上限8，budget 和 AST-01 均为7批。最终计划 user JSON 从140342字节降到134317字节（减少4.29%），全部原包字段逐项还原相等。报告在 `outputs/research/AST-01-offline-20261126.json`。这项结果来自已有包的离线序列化，不表示模型 token、耗时、调用费用或 Schema 语义重复减少4.29%。真实模型质量尚未验证。

完整回归123项通过，日志在 `outputs/AST-01-regression.log`；AST-01专项6项通过，包含截断后的子任务恢复和同配置续跑。此次未新增正式抽取实验目录，未调用真实模型。
