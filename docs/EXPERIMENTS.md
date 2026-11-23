# 内网实验与质量评估手册

本手册回答：先做哪些实验、怎样公平比较、怎样判定优劣、出了问题应回查哪个阶段。现有流水线保持不变；浏览器和人工评审工具独立读取产物。

六组 B1–B6 的明确清单、CMD 批量执行/抽样命令、人工判据和源码例子见 [六组基线与人工审核操作规程](BASELINE_REVIEW.md)，评审交付可复制 [报告模板](templates/BASELINE_REVIEW_REPORT.md)。本手册的 E0–E6 是工作阶段编号，其中 E2 包含六组基线，不是另外七组基线。

## 1. 先确定要回答的业务问题

先选一个范围明确的模块，建议包含设备类型、配置、阈值判断和处置动作。由熟悉代码的人列出 5–10 个业务问题，再运行模型，避免看到输出后才修改评价标准。

设备运维示例：

| 问题 | 应查看的产物 | 人工检查重点 |
|---|---|---|
| 有哪些设备、部件和监测值？ | schema、entities | 业务类型是否完整，是否混入 Logger 等工具类 |
| 设备由哪些部件组成？ | schema.relations、relations | 方向、端点和组成语义是否正确 |
| 温度阈值是多少，单位是什么？ | properties、attributes | 来自配置还是代码默认值；单位是否保留 |
| 传感器离线时如何处理？ | rules、源码 | 分支条件、状态改变、返回故障码是否完整 |
| 温度超限一定会停止设备吗？ | rules、宏配置 | 是否遗漏 ENABLE_REMOTE 等条件 |
| 告警如何转成维护任务？ | schema、relations、rules | 是否确有实现支持，是否只看到声明 |
| 配置定义和真实现场资产能否区分？ | entities.scope | 不能把示例或配置声明当成现场事件 |
| 两个命名空间中的 Device 是同一概念吗？ | entities、schema、evidence | 防止按名字误合并 |

本体展示和上述问题的人工覆盖检查已经可用；本项目尚不提供自动自然语言问答系统。

人工参考答案独立保存在模型输入目录外。现有示例的 `gold.json` 默认排除；自定义参考文件也应显式排除，避免参考答案进入模型。

## 2. 推荐实验顺序

| 编号 | 实验 | 固定什么 / 改变什么 | 主要产物与验收 |
|---|---|---|---|
| E0 | 环境与示例闭环 | 使用内网 Joern、模型和现有示例 | doctor、CPG、schema、实例、validate 均可运行 |
| E1 | 一个真实模块试验 | 限定文件范围，先 CPG + maintenance | 检查源码覆盖、解析告警、业务概念和条件动作 |
| E2 | 六组端到端比较 | 同源码、模型、预算；变化图视图和 profile | AST/CFG/CPG × general/maintenance；比较整条流水线 |
| E3 | 固定 schema 的实例对照 | 同一版 schema、相同 profile；变化图视图 | 隔离 schema 发现差异，比较实例与规则的质量 |
| E4 | 提示词修改 A/B | 同一视图、源码、模型、预算；只改一组提示词 | 通过独立实验 ID 对比概念偏离、重复、漏条件等 |
| E5 | 恢复与异常测试 | 使用自动化测试和一次相同命令续跑 | 成功任务复用；失败、截断、非法 JSON 可定位 |
| E6 | 稳定性与扩容，后续再做 | 优选组合新建 3 个实验 ID；随后逐步加模块 | 人工看同义概念和条件是否稳定，同时看调用成本 |

第一轮优先完成 E0、E1 和少量人工评审，再做 E2。大仓库先明确处理模块，不把采样 schema 宣称成全仓完整本体。E6 不是第一天验收的前置条件。

### E0：环境与小示例

完整命令见 `RUNNING.md`。先 `doctor --no-llm`，再 `doctor`，然后 `run --config configs/smoke.yaml`。该配置局部发现采样 8 包，实例阶段处理纳入范围的全部 prepared packs。

检查：有无未解析文件；schema 是否为空；失败任务是否为 0；正式输出是否都能回查源码；规则是否保留原始条件。

### E1：真实仓库的一个模块

```powershell
$py = '.\.venv\Scripts\python.exe'
& $py -m code_ontology run --repo 'D:/internal-repo' --include 'src/monitor/*' --include 'include/*' --config configs/smoke.yaml --experiment module-01 --out outputs/internal-pilot
```

include 路径按实际仓库调整；必要的头文件、配置和 CMake 也纳入。检查 manifest 的 files/skipped/warnings。路径过滤只影响纳入内容，不保证宏的所有构建变体都被解析；includes、defines 和编译数据库在 YAML 中明确配置。

### E2：六组端到端比较

```powershell
& $py -m code_ontology experiment --repo 'D:/internal-repo' --config configs/baseline.yaml --experiment six-01 --out outputs/internal-pilot
```

实际仓库仍需维持 E1 的文件范围，可使用同样的 `--include` 参数或把范围写入 YAML。六组共享一次解析。每组独立发现 schema，因此结果差异同时包含 schema 和实例阶段的影响；这是一组端到端比较。

不要假设 CFG 应比其他视图更差或更好；三组共享源码证据，主要区别是提供的图上下文。报告节点裁剪、发现采样和任务失败，避免把输入覆盖不足当成模型能力结论。

### E3：固定 schema 对照

把下列 `$snapshot` 改成 `SNAPSHOT=...` 后的实际目录：

```powershell
$snapshot = '<同一个源码快照目录>'
& $py -m code_ontology discover --snapshot $snapshot --view cpg --profile maintenance --experiment schema-anchor --config configs/baseline.yaml
$schema = "$snapshot/runs/schema-anchor/cpg/maintenance/schema/schema.json"
foreach ($view in @('ast','cfg','cpg')) {
  & $py -m code_ontology extract --snapshot $snapshot --view $view --profile maintenance --schema $schema --experiment fixed-schema-01 --config configs/baseline.yaml
}
& $py -m code_ontology report --snapshot $snapshot --experiment fixed-schema-01
```

schema 必须属于同一快照，并在实际实验前人工确认。三视图选择到的证据 ID 必须兼容，否则程序明确拒绝；不强行转移无证据 schema。若按模块选取子 schema，检查 extraction/selection.json 中省略的条目。

### E4：提示词 A/B

复制完整 prompts 根目录（含 common、general、maintenance），在副本中只修改一个阶段。为新 YAML 配置 `prompt_dir`，相对路径以 YAML 所在目录为基准。每套提示词使用独立 `--experiment`，保留其文件版本。

若只比较实例提示词，复用同一版 `--schema`；若比较 schema 发现提示词，使用新实验 ID 或 `--update-schema`。固定 schema 的默认复用行为不能误当成提示词修改无效。

### E5：恢复与异常

使用相同参数、相同输出和相同实验 ID 重跑一次。预期复用成功请求；这属于恢复实验，不是新的独立抽取。模型参数、提示词、预处理内容改变会影响缓存。

```powershell
& $py -m unittest discover -s tests -v
& $py scripts/check_demo.py --real-joern
```

后者使用真实 Joern + 本地模拟模型接口，不使用真实模型配额。自动测试覆盖非法 JSON、空输出、鉴权、限流、截断和部分失败。不要为测试故障修改生产服务。

## 3. 分层判断生成结果的优劣

| 层次 | 指标 / 检查 | 自动程度 | 不足时回查 |
|---|---|---|---|
| 解析与覆盖 | 纳入文件、成功导图、悬空边、未解析符号、宏和头文件告警 | 可自动检查部分 | manifest、Joern 日志、normalized |
| 输入准备 | 有效源码范围、同批证据包、裁剪比例、未选包 | 可自动记录 | prepared/index、schema/selection |
| 输出结构 | JSON 合法、父类无环、domain/range 合法、端点和类型存在 | 自动 | 失败记录、schema、candidates |
| 证据引用 | ID 属于当前任务、文件行号有效、源码指纹一致 | 自动 | evidence、源码快照、validate |
| 证据语义 | 源码是否真的支持这个概念、关系、值或条件动作 | 人工 | 源码实现与配置，不只看模型解释 |
| 本体质量 | 业务相关、适当粒度、同义合并、层级合理、无误合并 | 人工 | schema 候选与合并过程 |
| 实例质量 | 实体身份、关系方向、属性值和单位、scope 区分 | 人工 | 实例、原始提取批次、源码 |
| 规则质量 | 条件、动作、否定、分支、宏配置是否完整 | 人工 | rules、CFG、实现与构建条件 |
| 业务用途 | 预先约定的问题有多少可由产物及证据回答 | 人工任务评估 | 业务问题表与漏项 |
| 成本与稳定性 | 耗时、token、失败、修复、重复试验差异 | 自动记录 + 人工对齐 | metrics、请求日志、不同实验 |

注意：文件中“证据可定位率 100%”不能说明证据语义支持率 100%。模型生成 100 个概念也不一定优于生成 20 个准确概念。

### 需要记录的人工指标

- **严格样本精确率** = 正确 /（正确 + 部分正确 + 错误）。无法判定与未评审单列，不能悄悄剔除后不报告。schema 与实例分别算，关系、属性、规则继续分开看。
- **证据真实支持率** = 充分支持 /（充分支持 + 部分支持 + 不支持）。这是人工阅读源码后的指标。
- **概念 / 关系召回率** = 参考答案中正确恢复的条目 / 参考答案应有条目。需事先建立参考集合并人工对齐同义名称。当前 `evaluate` 只做概念别名匹配。
- **业务相关率** = 业务相关条目 / 已判断相关性的条目；底层工具、容器、日志等是否算业务应事先约定。
- **范围正确率**：代码定义、配置、测试、示例是否被正确区分。
- **规则条件完整率**：检查完整条件的规则 / 已完成规则检查的规则；遗漏宏开关、离线优先分支等都应记录。
- **重复与误合并**：分别统计多个条目表达同一事实、不同实体被合成一个条目的情况；无法仅靠名字相同自动确定。
- **业务问题覆盖率**：有正确且可回查答案的问题数 / 事先约定问题数。若只凭知识图谱不能回答，标记需额外查询能力。

若采用 P/R/F1，需先约定粒度：关系按“主语概念—关系语义—宾语概念”匹配；属性同时考虑值、单位和条件；规则同时考虑条件与动作。不要对原始 JSON ID 做字符串交集来代替语义匹配。

### 建议的首轮试用门槛

以下是可调整的项目门槛，不是通用学术标准：关键阶段无未说明失败；正式条目引用可定位；在选定模块上优先达到 90% 的严格人工样本精确率、95% 的证据真实支持率；涉及控制动作的规则逐条检查，不能遗漏关键条件；不出现把测试/配置解释成现场事实的严重错误。

先做每类 10–20 条预评审，统一口径后再扩大。若样本总共很少，应展示分子分母并保留全部错误样例；少量样本的高比例不能直接推广到全仓库。

## 4. 已提供的人工评审工具

### 生成分层抽样表

```powershell
$runDir = "$snapshot/runs/my-smoke-01/cpg/maintenance"
& $py -m code_ontology.review sample --run-dir $runDir --out reviews/smoke-01.csv --per-kind 20 --seed 42
```

schema 类、关系类型、属性类型，以及实体、实例关系、属性、规则、候选和冲突分别采样。每类最多 20 条，不足时全取。`.csv.manifest.json` 记录总量、抽样量、种子和原始内容指纹；保留它与 CSV 同目录。

只填写以下列，不改 record_id、kind、payload_json、evidence_ids：

| 列 | 允许填写的值 |
|---|---|
| verdict | correct / partial / incorrect / uncertain |
| evidence_support | supported / partial / unsupported / uncertain |
| domain_relevant | yes / no / uncertain |
| scope_correct | yes / no / na / uncertain |
| rule_condition_complete | yes / no / na / uncertain |
| notes | 错误原因、缺失条件、重复项等自由文本 |

暂不判断的列可以留空。对非规则条目将 rule_condition_complete 填 na；候选和冲突用于复核抽取遗漏及多值问题，不加入正式断言的精确率分母。

```powershell
& $py -m code_ontology.review summarize --reviews reviews/smoke-01.csv --out reviews/smoke-01-summary.json
```

汇总按集合输出样本精确率、证据支持率、范围和条件完整情况，同时报告未判断数。跨集合总值只是样本统计，**不自动视为按全仓库数量加权的总体精确率**。正式对比优先分别报告每类分子分母；完全未评审的指标显示 null。

### 在页面中评审

选择条目 → 点击证据阅读源码 → 填写人工判断 → 保存。记录只存在于当前浏览器的本地存储，流水线产物不变。在「人工评审」页导出 JSON 后：

```powershell
& $py -m code_ontology.review summarize --reviews '<下载的评审.json>' --out reviews/browser-review-summary.json
```

页面点击选择属于非随机样本，只用于发现问题和讨论，不能直接作为全仓库质量估计。浏览器缓存被清除会丢失未导出的记录。

## 5. 每组实验应保存什么

至少保存：源码快照 ID、文件范围、解析参数与 Joern 版本、模型及参数、提示词版本、证据包预算和裁剪信息、schema 哈希、任务选择、失败清单、输出计数、调用与 token、人工样本和判据、典型正确与错误例子。

报告先描述覆盖范围与失败，再给质量和成本。现有 metrics 是最近一次执行的统计；缓存续跑后的耗时与调用数不代表首次生成成本，首次成功指标应另外保留。模型生成结果必须标记机器生成及人工复核范围。

建议错误分类：解析缺失 / 预处理丢上下文 / schema 漏概念 / schema 粒度偏差 / 同义未归并 / 误合并 / 关系方向错 / 属性值或单位错 / 条件遗漏 / 范围混淆 / 无依据断言 / 重复。

## 6. 展示什么才算“Ontology”

当前页面有两层：

1. **本体结构**：概念为节点，父类和业务关系为边，属性定义从概念详情查看。这是 schema 层。
2. **抽取实例**：具体条目为节点，实例关系为边，属性值、规则和来源在清单与详情查看。这是知识断言层。

它们目前是项目约定的 JSON 本体表示，尚未导出成 OWL/RDF，也没有自动公理推理。图看起来连通不等于语义正确；有用的展示需要同时能看到定义、端点类型、条件、范围和源码证据。

图谱为有限子图，明确显示展示数量与上限；完整条目通过「知识断言」的搜索和分页访问。当前界面用于小范围检查，不是百万节点图数据库前端。

