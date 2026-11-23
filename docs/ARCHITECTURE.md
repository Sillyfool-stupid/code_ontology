# 实现说明与数据契约

## 阶段边界

1. `repository.snapshot` 扫描、排除、编码转换并建立不可变源码快照。
2. `joern.parse` 用 c2cpg 生成基础 CPG，再用 Scala 脚本在独立工作区加载它、添加默认分析层并流式导出。
3. `joern.normalize` 用 SQLite 建节点和边索引，按边集合投影 AST/CFG/综合 CPG。
4. `prepare.prepare` 按类型/函数取源码与局部图，增加受限一跳上下文，按预算拆分与裁剪。
5. `Pipeline.discover` 分层采样 → 分批局部发现 → 规范 ID → 分组合并 → 固定 schema。
6. `Pipeline.extract` 固定 schema，逐证据包抽取；超长 schema 使用词面相关子集与父类闭包，省略项单独记录。
7. `Pipeline.finalize` 校验类型和引用、去重、保留冲突，将不能纳入的断言移到候选。

Joern 脚本使用 flatgraph API，明确以 Joern 4 为基线。`doctor` 检查 CLI 参数；真实 demo 检查导出 API。它没有宣称兼容旧 overflowdb 版本。`raw/cpg.bin` 保持基础图，包含默认 overlay 的工作副本在 `logs/joern-work/workspace/`，实际导出位于 `raw/cpg/`。

## 原始图格式

节点 JSONL：

```json
{"id":"64-bit-id-as-string","label":"METHOD","properties":{"NAME":"evaluate","FULL_NAME":"plant.TemperatureMonitor.evaluate","FILENAME":"...","LINE_NUMBER":12}}
```

边 JSONL：

```json
{"src":"node-id","dst":"node-id","label":"CFG"}
```

同一节点文件内 ID 必须唯一；边端点必须存在。原始属性不裁剪，规范化属性采用白名单。`normalized/*/index.json` 保存边种类、数量、悬空边检查结果和指纹。

已有三万行 JSON 若是本工具格式，可使用：

```powershell
# 先 scan，获得包含源码和 manifest 的快照
python -m code_ontology scan --repo D:/repo --out D:/results
python -m code_ontology import-graph --snapshot '<快照目录>' --nodes nodes.json --edges edges.json
```

`.json` 必须是节点/边各自的顶层数组，用 ijson 流式读取；`.jsonl` 每行一个对象。文件名、行号必须能关联到该快照的仓库相对路径或原始仓库路径。任意 GraphSON/其他工具 JSON 不支持直接导入，需明确适配。`raw/` 与 SQLite 是磁盘级处理；单个证据包和当前实验的 prepared packs 才进入业务流水线内存。该版本不承诺无限规模。

## 证据包与预算

三个视图的选取单元相同：有位置的用户方法/类型，以及纳入的文档、配置和 CMake 文件。外部/合成/无定位节点不进入发现任务，但跳过列表保留。

`pack_id` 包含视图；`symbol_id` 是声明/定义的语义关联；`evidence_id` 关联具体源码片段。符号关联不是编译器级符号链接：重载使用完整签名，同名内部链接函数仍需要后续结合编译单元复核。

每包包含源码、位置和指纹、图节点与边、一跳上下文、解析及裁剪标记。类与成员函数的源码可能重叠，后处理会合并相同断言证据。CFG 不虚构分支标签。包含、宏配置不完整会记录警告；未解析调用保留原有 METHOD_FULL_NAME/IS_EXTERNAL 信息。

输入预算使用 UTF-8 字节数保守上界，而不是声称精确 token 数。单包、完整请求（含提示词和 schema）均检查预算，预留输出与协议开销。默认小上下文可能裁剪较多图节点；增大上下文后可提高 `pack_tokens` 和 `graph_nodes_per_pack`，不要只提高 API 的输出上限。

一个逻辑片段本身放不进请求时明确报错，不静默截断源码。模型输出 `finish_reason=length` 时任务失败并保留输入，要求缩小处理单元或增大预算；基线不自动无限重试来掩盖截断。

## schema 契约

概念：`id/name/definition/aliases/parents/evidence_ids`。

关系：`id/name/definition/domain/range/evidence_ids`，domain/range 为概念 ID 数组。

属性：`id/name/definition/domain/datatype/unit/evidence_ids`，datatype 为 `string/number/integer/boolean`。

顶层：`version/profile/classes/relations/properties/unresolved`。Pydantic 禁止额外字段、检查 ID 唯一性、引用和层级循环。最终 ID 程序生成；只合并确定一致的定义，不凭名称模糊匹配自动合并业务概念。

固定 schema 的证据必须存在于当前快照。跨仓库通用 schema 的无证据导入不在此版本范围；同快照可通过 `--schema` 共用 schema 比较实例抽取效果。

## 实例契约

- 实体：`id/name/type_id/scope/basis/evidence_ids`。
- 关系：`subject/relation_id/object/basis/evidence_ids`。
- 属性：`subject/property_id/value/unit/basis/evidence_ids`。
- 规则：`name/condition/action/scope/basis/evidence_ids`。
- 候选：`reason/payload`。

`scope` 只能是 `code_definition/configuration_record/test/example`；`basis` 为 `direct/inferred`。模型必须在同一响应中声明关系与属性端点，避免依赖聊天历史。

程序验证证据属于当前任务、位置与快照一致、类型属于固定 schema、关系和属性端点存在、domain/range 满足类或父类、值与单位合法。相同业务实体在同文件中可合并；跨文件同名对象保守分开，避免误合并现场资产。不同属性值同时保留并进入冲突清单。

这些检查无法自动证明“源码真的支持业务解释”。代码中的条件与动作由模型解释，仍需人工核验，不以结构合法代替语义准确。

## 缓存与恢复

- 源码内容、编译数据库、宏/头文件参数、Joern 发行版和导出脚本影响快照。
- 规范图指纹、预处理实现和预算影响证据包缓存。
- 模型、接口、参数、提示词、输入、输出契约、实验 ID 影响响应缓存。
- Schema 默认固定；`--update-schema` 显式刷新。局部发现有失败时，续跑会重新尝试发现并复用成功请求。
- 每个请求状态存 SQLite；只有通过输出校验的响应才能进入成功缓存。
- 一个任务只允许一次 JSON/证据修复；业务引用不合法的抽取条目移入候选。
- 网络/限流/暂时服务错误有限退避；鉴权等不可恢复错误不在同一组继续扩散。
- 使用不同输出目录或不同实验 ID 进行独立并行任务；同一快照目录不支持两个进程同时解析/写同一组结果。

## 实验与评估

六组比较共享一个 CPG、相同源代码单元和分层采样策略。首次使用每组独立发现的 schema，观察完整管线差异。需要隔离实例抽取变量时，使用同一个 `--schema` 在三视图之间比较。

相同实验 ID 重跑属于恢复，不属于独立重复实验。使用新 ID 才会绕过先前响应缓存。模型、服务版本和采样不一定完全确定，当前版本不声称可以消除模型随机性。

参考答案默认排除 `gold.json`，不送模型。概念别名匹配只作为召回导航。需要完整质量评估时人工标注概念、关系和断言，再记录精确率/召回率、证据真实支持率、漏项、误合并和领域偏离；不可用当前简单统计冒充这些指标。

## 独立展示和人工评审

`python -m code_ontology.viewer` 启动只监听 127.0.0.1 的只读服务。后端只导入产物工具，不导入或触发 Pipeline/LLM；静态 HTML/CSS/JS 随包分发，无 npm/CDN 依赖。接口限定运行 ID、记录集合与可查看的产物名称，不开放任意文件下载或写入路由。

图中 schema 与实例分层展示；有限子图显示数量和上限，完整断言通过 JSONL 搜索分页。证据回查验证路径与当前源码哈希；浏览器评审只保存 localStorage，导出后独立汇总，不改变正式输出。

`python -m code_ontology.review` 独立生成分层抽样 CSV 与原始内容指纹，汇总人填写的标签。它不自动标注语义、不生成总体精确率，也不把交互非随机样本当成随机评估。

详见 `RUNNING.md`、`EXPERIMENTS.md` 与 `PIPELINE_IO.md`。

