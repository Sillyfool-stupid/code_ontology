# 每阶段的输入、输出与检查方法

设 `$snapshot` 为 CLI 打印的 `SNAPSHOT=...`，`$runDir` 为 `runs/<实验ID>/<视图>/<profile>`。所有源码位置相对 `$snapshot/source/`。

| 阶段 | 输入 | 核心输出 | 如何判断阶段完成 | 出问题后重跑 |
|---|---|---|---|---|
| 0. 配置与诊断 | `.env`、YAML、Joern CLI | outputs/doctor/doctor.json、诊断日志 | Joern 能力与模型 JSON 探测通过 | doctor；不代表已经解析仓库 |
| 1. 扫描与快照 | 仓库路径、纳入排除、编码与解析配置 | manifest.json、policy.json、source/ | 文件范围正确；内容指纹、编码和告警记录齐全 | scan 或 parse；源内容变化生成新快照 |
| 2. Joern 解析 | 固定源码、头文件/宏/编译数据库 | raw/cpg.bin、Joern 日志 | 进程成功且真实导图成功，检查解析限制 | parse；必要时 --force-parse |
| 3. 导出与规范化 | 同一个基础 CPG | raw/cpg/{nodes,edges}.jsonl、raw/ast、raw/cfg、normalized/graph.sqlite、symbols、三视图 | 节点 ID 和边端点合法；各视图 index 记录规模和图指纹 | parse；已有约定格式用 import-graph |
| 4. 证据包准备 | 规范图、源码、预算和邻接限制 | prepared/<view>/packs.jsonl、index.json | 每包有源码与图上下文；记录裁剪、跳过项；validate 可定位 | prepare --view ... |
| 5. 局部 schema 发现 | 采样证据包、common + profile/discover 提示词、JSON 契约 | schema/local/*.json、schema/selection.json、请求记录 | 局部输出合法，已选与未选清晰，无未说明任务失败 | discover；成功 API 请求可缓存 |
| 6. 合并固定 schema | 去重局部候选、merge 提示词 | schema/merge/、schema/schema.json、provenance.json | 类/父类/关系/属性引用合法，人工检查业务粒度 | discover --update-schema 或新实验 ID |
| 7. 实例抽取 | 固定 schema、逐批证据包、extract 提示词 | extraction/*.json、selection.json、失败任务文件 | 所有计划任务有状态；保留 schema 子集省略记录 | extract；可 --schema 显式指定 |
| 8. 校验与归并 | 抽取结果、schema、证据索引 | final/schema.json 和各类 JSONL、conflicts、metrics、report.md | 正式端点/类型/值合法；无依据或不兼容内容归候选；冲突保留 | extract（复用成功 API 缓存后重做归并） |
| 9. 独立位置校验 | 准备包、正式结果与源码快照 | validation.json | ok=true；明确只是结构和位置检查 | validate |
| 10. 参考概念匹配 | 人工 gold.json、正式 schema | evaluation.json | 显示找到、缺失、别名召回和需复核项 | evaluate |
| 11. 人工抽样评审 | final 结果与源码；人工判据 | 独立 reviews/*.csv、manifest、summary.json | 标签由人填写，汇总记录分母与未判断数 | review sample / summarize |
| 12. 展示与比较 | 已存在的上述产物 | 本机只读网页、浏览器导出 SVG / 评审 JSON | 能在图、表、证据和对照之间浏览 | viewer；不会调用模型或重做任何阶段 |

## 数据契约速查

### 原始图

```json
{"id":"node-id","label":"METHOD","properties":{"NAME":"evaluate","LINE_NUMBER":12}}
```

```json
{"src":"node-id","dst":"another-id","label":"CFG"}
```

节点、边各为独立 JSONL；`import-graph` 也支持约定的顶层 JSON 数组，不接受任意第三方 JSON。原始大图不整份输入模型。

### 证据包

真实字段包括 pack_id、view、symbol_id、kind、source_refs、evidence_ids、code、context 和警告等。source_refs 的 file/start_line/end_line/evidence_id/source_sha256/snippet_hash 用于回查和完整性检查。不同视图共享源码基础，图结构不同。

### Schema

```text
classes: id, name, definition, aliases, parents, evidence_ids
relations: id, name, definition, domain, range, evidence_ids
properties: id, name, definition, domain, datatype, unit, evidence_ids
top-level: version, profile, classes, relations, properties, unresolved
```

domain/range/parents 引用类 ID；datatype 为 string/number/integer/boolean。schema 固定后记录内容哈希；仅改提示词不会自动覆盖已固定 schema，按实验目的显式更新。

### 正式断言

| 文件 | 必要含义 | 人工检查 |
|---|---|---|
| entities.jsonl | 名称、类型、对象范围、依据、证据 | 身份、类型、是否把声明当成现场事实 |
| relations.jsonl | subject—relation_id—object、依据、证据 | 方向、端点类型、关系是否真的成立 |
| attributes.jsonl | subject、property_id、value、unit、依据、证据 | 值与单位，代码表达式和确定数值的区别 |
| rules.jsonl | name、condition、action、scope、basis、证据 | 条件完整性、宏约束、动作与否定 |
| candidates.jsonl | reason、payload | 未纳入原因、漏项、潜在正确断言 |
| conflicts.jsonl | subject、property_id、多个值、复核状态 | 多值合法还是抽取错误，不能无理由覆盖 |

scope 为 code_definition/configuration_record/test/example；basis 为 direct/inferred。两者都由模型判断，仍需人工核验。

## 最有效的回查顺序

如果概念漏了：检查源文件是否纳入 → Joern 是否解析 → 是否生成相应证据包 → 是否进入发现采样 → 局部 schema 是否有 → 合并是否丢失。

如果规则错了：检查模型该任务实际看到的代码与 CFG → 条件是否被分块或宏配置排除 → 固定 schema 是否限制表达 → 原始回答是否错 → 后处理是否仅完成结构检查。

如果实例数量奇怪：比较 extraction/selection、失败任务、候选原因、去重与冲突，而不是直接增加模型轮数。

`llm_requests` 包含渲染后请求和原始回答；可精确检查当时输入，不需要依赖一次持续聊天的隐藏上下文。

