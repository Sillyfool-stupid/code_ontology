# 六组基线实验与人工审核操作规程

> 无 Joern 时可使用内置 [Tree-sitter AST 后端](TREE_SITTER.md)：`configs/tree-sitter.yaml`。仅支持 AST，实验为两种 profile 共两组；本文 Joern 的 CFG/CPG、六组与 CPG 产物说明只适用于 Joern 后端。

本文配合 [内网 Agent 手册](INTRANET_AGENT_GUIDE.md) 使用。Agent 负责运行、整理产物、生成审核表和统计；人工负责读源码、判断语义和签署结论。填写报告可复制 [空白评审报告模板](templates/BASELINE_REVIEW_REPORT.md)。以下命令使用 CMD，先激活虚拟环境，之后统一用 `python`。

## 1. 必须完成的六组是什么

| 编号 | 图视图 | 提示词 profile | 实验目录（相对 runs/实验ID） | 核查重点 |
|---|---|---|---|---|
| B1 | AST | general | ast/general | 通用概念、属性、对象组成 |
| B2 | AST | maintenance | ast/maintenance | 运维概念、属性、设备组成 |
| B3 | CFG | general | cfg/general | 通用流程、条件和动作 |
| B4 | CFG | maintenance | cfg/maintenance | 告警、故障处置及完整触发条件 |
| B5 | 综合 CPG | general | cpg/general | 调用、类型、引用关联与业务概念 |
| B6 | 综合 CPG | maintenance | cpg/maintenance | 综合运维知识及跨函数依据 |

这是 **3 种视图 × 2 套提示词 = 6 组**，每组一次基线运行。环境诊断、示例验证、恢复测试和人工审核是配套工作，不另外算成第七组；三次独立重复属于后续稳定性实验。

六组都有基础源码、签名和位置，区别在附加图上下文；不是让 CFG 组只能看到控制流边。每组分别发现、合并 schema，再抽取实例，因此比较的是端到端效果。固定同一个 schema 再比较视图属于可选扩展，见 [实验手册 E3](EXPERIMENTS.md)。

### 公平比较前固定这些条件

- 同一源码快照、文件范围、宏、头文件和编译数据库；人工参考答案不能进入输入。
- 同一模型、温度、上下文/输出预算、并发、重试策略和程序版本。
- 同一 `baseline.yaml`：发现最多 24 包、每包预算 4500、每批最多 2 包、邻接上限 3、图节点上限 100。实际调用数以任务清单为准。
- 同一套 common 提示词、两套 profile 文件的固定版本；比较期间不临时修改。
- 核对各视图实际源码单元、`symbol_id`、`source_refs`、发现选包和裁剪记录。pack ID 含视图，不能只按 pack ID 判断输入是否相同。
- 同一人工评分口径和采样策略；运行失败、输入遗漏和条件不可见必须单列。

`smoke.yaml` 只跑 CPG + maintenance，发现最多 8 包。之前的 smoke 结果可以证明闭环，不能直接顶替本次 baseline 的 B6。

## 2. 执行命令与六组交付清单

在项目根目录打开 CMD：

```bat
call .venv\Scripts\activate.bat
python -m code_ontology doctor --no-llm
python -m code_ontology doctor --env-file .env
python -m code_ontology experiment --repo examples/maintenance --config configs/baseline.yaml --experiment intranet-six-01 --env-file .env
```

真实仓库另建 YAML，保留六组配置并固定实际文件范围；将 `--repo` 和 `--config` 改成实际路径。先完成示例和一个小模块的闭环，再运行已确认范围的六组。命令会调用模型，不要为了读说明重复执行。

记录终端打印的 SNAPSHOT，再设置变量，下面的路径需要替换：

```bat
set "SNAPSHOT=D:\实际输出根目录\实际源码快照ID"
set "EXP=intranet-six-01"
set "RUN_DIR=%SNAPSHOT%\runs\%EXP%\cpg\maintenance"
```

每组均需完成以下检查，不能只检查 B6：

- [ ] `run_manifest.json` 中阶段状态完成；`metrics.json`、`failures.jsonl` 与任务记录一致。
- [ ] `final/schema.json`、实体/关系/属性/规则以及候选、冲突可读取；零条结果明确记录，不能当作高质量。
- [ ] 单独执行 `validate`，保存 `validation.json`。
- [ ] 有适用 gold 时执行 `evaluate`；人工另外核对语义覆盖和漏项。
- [ ] 生成分层审核 CSV 与对应 manifest，由人工填写后汇总。
- [ ] 保存首次运行成本、输入范围、裁剪/未处理项和典型错误。

下面在 CMD 交互终端依次检查六组并生成每类最多 10 条的审核表：

```bat
for %V in (ast cfg cpg) do for %P in (general maintenance) do python -m code_ontology validate --snapshot "%SNAPSHOT%" --view %V --run-dir "%SNAPSHOT%\runs\%EXP%\%V\%P"
for %V in (ast cfg cpg) do for %P in (general maintenance) do python -m code_ontology.review sample --run-dir "%SNAPSHOT%\runs\%EXP%\%V\%P" --out "reviews/%EXP%/%V-%P.csv" --per-kind 10 --seed 42
```

把命令写入 `.bat` 文件时，循环变量需用 `%%V`、`%%P`。循环不会因某组失败自动停止，逐组检查退出结果及产物；失败组不进入“完整六组”结论。已有审核表不会覆盖，重复采样用新文件名，勿删除已填写内容。

仅对于自带 maintenance 示例，可额外运行：

```bat
for %V in (ast cfg cpg) do for %P in (general maintenance) do python -m code_ontology evaluate --run-dir "%SNAPSHOT%\runs\%EXP%\%V\%P" --gold examples/maintenance/gold.json
```

真实仓库应使用独立建立的本仓库参考答案。当前 `evaluate` 只计算概念名称/别名匹配召回，不验证关系、规则或事实真假。普通 `experiment/run` 不会自动执行以上独立校验与人工评审。

某一组失败，保持配置与实验 ID 不变重跑原命令即可恢复。只重跑 B4 也可使用：

```bat
python -m code_ontology run --repo examples/maintenance --config configs/baseline.yaml --views cfg --profile maintenance --experiment intranet-six-01 --env-file .env
python -m code_ontology report --snapshot "%SNAPSHOT%" --experiment "%EXP%"
```

真实仓库必须保留自己的 repo、范围配置和输出根目录。更换提示词/模型/范围做新对照时用新实验 ID，记录变化。缓存恢复成本与首次生成成本分开保存；最新 metrics 可能已被续跑更新。

## 3. 人工审核先做什么，再做什么

### 第一步：在看输出前建立评价基准

找熟悉 C++ 实现和业务的人员，先固定 5–10 个业务问题，以及一份独立参考集合。每个参考项记录“预期概念/关系/规则、源码文件行号、条件、范围”。参考文件放在输入仓库外或明确排除。

示例问题：有哪些设备与部件？温度阈值来自哪里？传感器离线时做什么？超温是否一定停机？维护任务由什么生成？不要看到模型输出后再把缺失的问题删掉。

### 第二步：选样本，记录工作量

审核工具分别采样 9 个集合：schema 类、关系类型、属性类型，实体、实例关系、属性值、规则、候选、冲突。

- 口径试审：每类最多 10 条，不足则全取；最多每组 90 条，六组 540 条。实际数量由 manifest 给出。
- 正式抽样：口径一致后可增至每类 20–30 条，另存新样本，并记录两轮有无重复。不要把重复条目当成独立样本翻倍计数。
- schema 小时建议全审，并检查整体层级、孤立概念、同义重复和误合并；这些全局问题不能只看单条随机样本。
- 涉及停机、启停联锁等关键规则另做逐条专项检查，保存检查清单。专项非随机审核与分层样本指标分开报告。

相同 seed 保证同组同产物抽样可复现，不保证六组抽到相同语义条目，因为 schema、数量和记录 ID 可能不同。跨组共同错误通过独立参考项和业务问题表比较，不强行按生成 ID 对齐。

### 第三步：按证据回查源码

先启动界面：

```bat
python -m code_ontology.viewer --root outputs --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，选择正确快照、实验和组，在图谱/断言页面点击证据。外部输出目录需要调整 `--root`。

正式样本以 CSV 中的 `payload_json` 和 `evidence_ids` 为准。人工读断言全文，跟随证据查看 **本次快照的 `source/`**，补读必要头文件、调用实现和配置，不能只核对某一行含有同名词。离线也可用 `evidence.jsonl` 将 ID 映射到文件行号后打开源码。

逐项问：源码说的是不是这件事？类型、方向、数值、单位、条件是否一致？这只是声明、配置、测试，还是确有实现？多个证据是否拼接出了源码没有的事实？`basis=inferred` 也要有可复核推理，不能免除证据要求。

若当前条目引用不足、需要额外源码才能支持，在 notes 记录所需位置及“引用不足”；不要把它当成证据充分。无法判断时标 uncertain，保留原因，不猜答案。

### 第四步：填写 CSV，不修改模型结果

保留 CSV 旁边的 `.csv.manifest.json`。CSV 用 UTF-8 保存，只编辑后六列；原始四列 `record_id`、`kind`、`payload_json`、`evidence_ids` 不变，不删行、不改正式输出来使评审通过。

| 列 | 允许值 | 填写含义 |
|---|---|---|
| verdict | correct / partial / incorrect / uncertain | 整条语义完全正确 / 有正确部分但不完整 / 核心错误 / 暂无法判断 |
| evidence_support | supported / partial / unsupported / uncertain | 提供的证据充分支持 / 只支持部分 / 不支持 / 暂无法判断 |
| domain_relevant | yes / no / uncertain | 是否属于事先约定的业务范围 |
| scope_correct | yes / no / na / uncertain | 代码定义、配置记录、测试、示例范围是否正确；不适用填 na |
| rule_condition_complete | yes / no / na / uncertain | 规则条件是否完整；非规则填 na |
| notes | 自由文字 | 具体错误、源文件行号、遗漏条件、重复/误合并、复核意见 |

留空表示未审核，不是正确；`partial` 不按半分自动计算。verdict 与证据支持分别判断，例如正确的业务概念也可能引用了不相干代码，或技术细节虽正确却 `domain_relevant=no`。本项目没有自动推断人工评分。

### 第五步：复核与汇总

条件允许时，第二位人员独立复核约 20%–30% 样本及所有争议/高风险规则，分别保存标签；讨论后形成最终表，记录分歧和裁决原因。只有一位审核人时如实记录，不虚构双人一致性。

人工填写并保存六份 CSV 后：

```bat
for %V in (ast cfg cpg) do for %P in (general maintenance) do python -m code_ontology.review summarize --reviews "reviews/%EXP%/%V-%P.csv" --out "reviews/%EXP%/%V-%P-summary.json"
```

汇总不会调用模型、不会修改正式产物。检查 `missing_sample_rows` 是否为 0，查看 `unresolved_or_unreviewed`；全未填写时指标为 null，不可填 0% 或 100%。

网页「人工评审」也可录入并导出 JSON，但记录在浏览器 localStorage，需及时导出。页面随手选取是 `interactive_nonrandom`，适合找问题，不能替代规定的分层样本统计。正式 CSV 的人工标签需填写到原 CSV，页面标签不会自动同步过去。

## 4. 各类条目具体判什么

| 集合 | 人工核查重点 | 常见错误 |
|---|---|---|
| schema.classes | 业务含义、定义、别名、父类粒度；父类必须是“是一种” | 把 Logger 当告警概念；把组成关系当继承 |
| schema.relations | 关系定义、方向、domain/range；是否有业务依据 | contains 方向颠倒；同一个关系混合处置与组成 |
| schema.properties | 所属概念、数据类型、单位、定义 | 阈值单位猜错；方法名当属性 |
| entities | 身份是否可区分、类型、命名空间、scope、basis | 两个 Device 误合并；TEST-PUMP 当现场资产 |
| relations | 主语/宾语存在且身份正确、关系含义与方向、证据链 | 调用了函数就断言拥有某设备；跨分支拼错关系 |
| attributes | 值和类型、单位、适用对象及配置上下文 | 把变量 thresholdC_ 当确定数字；默认值当所有实例值 |
| rules | 完整条件、否定、优先分支、宏、动作及返回码 | 忽略离线优先分支或 ENABLE_REMOTE，写成无条件停机 |
| candidates | 原因、是否其实有依据、是否受 schema 漏项影响 | 将正确知识因 schema 缺失留在候选，或保留幻觉 |
| conflicts | 是否同对象同条件真冲突，还是范围/版本不同 | 把不同设备配置值误报冲突；错误覆盖有条件的多值 |

候选和冲突是诊断集合，不加入正式断言精确率分母。复核正确候选可以揭示漏抽，但本次评审只做独立记录，不将它手动塞回正式输出冒充流水线结果。schema 的 unresolved 也应单独看。

### 用 monitor.cpp 统一判定口径

示例 `examples/maintenance/src/monitor.cpp` 先检查 `!temperature.online` 并返回 SensorOffline；在线时才继续判断超温。`exceeds` 比较 `value > thresholdC_`；停机调用位于 `#ifdef ENABLE_REMOTE` 下。

| 示例断言（用于说明，不是已有输出的评分） | verdict 建议 | 其他标签与原因 |
|---|---|---|
| 传感器离线时设 Warning，返回 SensorOffline | correct | 对应源码引用充分时 supported；规则条件完整 yes |
| 温度超限一定会停机 | partial | 保留超温关联但漏在线前提和宏开关；evidence partial、条件完整 no |
| 所有设备温度阈值固定为 85℃ | incorrect | 阈值来自构造参数；不能由某配置推广到所有设备 |
| 现场已经发生一次超温故障 | incorrect | 代码分支不证明真实事件；unsupported、scope_correct=no |
| 存在 TemperatureMonitor 类定义 | correct | 若只是在陈述代码定义可正确；是否有业务价值另填 domain_relevant |

以上第二行约定“核心过程有依据但条件遗漏”为 partial，严格精确率不计入正确；方向/对象/动作完全错误则 incorrect。高风险动作遗漏任何关键条件都不能通过专项规则验收。若宏状态或源码解析不足无法确认，写 uncertain 和限制。

## 5. 如何计算指标和选出较好组合

与现有 `review summarize` 口径一致：

- **严格样本精确率** = correct / (correct + partial + incorrect)。例如 10 条：正确 7、部分 1、错误 1、无法判定 1，则为 7/9，另报 uncertain=1。
- **人工证据支持率** = supported / (supported + partial + unsupported)。与 verdict 分母独立，须人工阅读证据后填写。
- **业务相关、范围正确、条件完整率**分别为对应列 yes / (yes + no)。na、uncertain 和空白均单列，不藏分母。
- **召回/业务问题覆盖**需独立参考答案，由人工逐项语义对齐并报告分子分母。名称别名召回不是完整语义召回，抽样检查已有输出也不能发现全部遗漏。

查看汇总的 `per_kind`，分别报告 schema 三类、实例四类；`assertion_sample` 只包含实体/关系/属性/规则，`schema_sample` 单独汇总。每类等量抽样后的合并比例只是样本结果，不能当成全仓总体精确率，不能单按总体条数排名。小样本展示分子分母，不凭一两条差异宣称统计显著。

另对参考集合记录“已恢复 / 部分恢复 / 缺失 / 范围外及理由”，各组使用同一判据；类层级、同义重复、误合并和高风险规则单独列结果。通用/运维 profile 仍采用同一目标范围审核，不因某组名称多就加分。

第一轮建议依次比较：

1. 流程与解析覆盖是否可比；失败或明显漏输入先解决，否则结论限定为部分结果。
2. 关键断言是否有依据，是否混淆配置/测试/现场，关键规则条件是否完整。
3. 分类型严格样本精确率、证据支持率、相关性，以及独立参考项和业务问题覆盖。
4. 重复/误合并、人工修正工作量，最后才比较首次生成耗时、token、修复与失败成本。

可采用试用目标：严格人工样本精确率约 90%、证据支持率约 95%，关键控制规则逐条通过。它们是可调整的工程目标，不是统计保证或通用标准；需同时报告样本量、未审核项和覆盖。不要把“证据能定位”当作语义通过。

## 6. Agent 最终应交付什么

- 六组目录与 `comparison.md`；每组状态、范围、校验、失败和首次成本。
- 六份人工审核 CSV、对应 manifest 与汇总 JSON；未完成审核的保留空标签并明确标记待人工。
- 独立参考答案、业务问题覆盖表、整体 schema 审核与高风险规则专项清单。
- 按模板填写的 Markdown 报告，包含典型正确/错误条目和源码位置、推荐组合及局限。

可直接给内网 Agent：

> 阅读 AGENTS.md、docs/INTRANET_AGENT_GUIDE.md、docs/BASELINE_REVIEW.md。对我指定仓库与范围执行 AST/CFG/CPG × general/maintenance 六组基线，保持其余条件固定。逐组检查阶段状态和独立 validate，生成每类最多 10 条的审核 CSV 与 manifest，保留首次成本。准备源码证据导航、参考问题表、schema 全局检查和关键规则专项清单。人工标签留空交我填写，不能把模型自评写成人工评审。收到填写后的表再运行 summarize，按 docs/templates/BASELINE_REVIEW_REPORT.md 整理六组报告，说明漏项、失败和未评审范围。实际仓库、配置和输出路径如下：……
