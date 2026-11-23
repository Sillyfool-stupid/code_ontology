# 内网 Agent 使用与接手手册

维护日期：2026-11-24。本文面向接手本项目的 Agent，也可作为人工运行清单。以当前仓库代码、CLI `--help` 和实际产物为准；历史外网验证不能代替本次内网验证。

## 1. 项目目标与当前边界

输入一个 C/C++ 仓库及必要配置，建立可追溯链条：

```text
代码仓库 → 不可变源码快照 → Joern CPG
        → AST / CFG / 综合 CPG 视图
        → 按函数/类型组织的证据包
        → 局部 schema → 合并固定 schema
        → 实体 / 关系 / 属性 / 条件动作规则
        → 校验、候选、冲突、报告、可视化和人工评审
```

重点是业务概念与源码依据，不是把所有 C++ 类直接当成本体。general 与 maintenance 两套提示词共用程序。综合 CPG 是有限选择的结构上下文，原始大图不会整份送给模型。

现有能力：文件驱动、阶段独立、缓存恢复、JSON/JSONL 输出、只读浏览器界面、人工抽样汇总。未实现完整 CMake 求值、全仓数据流推理、OWL/RDF 导出、自动本体推理或自然语言问答。网页只监听本机，不是生产多用户服务。

## 2. 接手时先确认哪些信息

先从用户任务、现有配置与目录中寻找答案，缺失且影响执行时再询问：

1. 项目根目录、输入代码仓路径、操作系统和当前 shell。
2. 本次是首次安装、示例验证、真实模块抽取、实验比较、结果审查，还是修复故障。
3. 内网 Joern/Java 路径和版本、Python 版本、依赖源是否可用。
4. 用户指定的模型端点和实际模型名是否已配置；是否获准运行真实模型任务及其范围。
5. 本次纳入模块、必要头文件、构建宏、编译数据库、配置文件与参考答案位置。
6. 已有输出根目录、源码快照和实验 ID；优先复用符合本次目标的产物。

不要一上来运行完整六组实验。先只读检查，再完成示例或已授权的小模块。不要自动访问公网替代内网依赖源或模型服务。

## 3. 环境与配置

### 3.1 CMD：推荐给用户的写法

下面使用一个可替换的项目路径；替换为内网真实路径。每个新终端需要重新激活：

```bat
cd /d "D:\code\code-ontology"
call .venv\Scripts\activate.bat
where python
python -c "import sys; print(sys.executable); print('venv active:', sys.prefix != sys.base_prefix)"
```

最后一行应显示项目虚拟环境的解释器以及 `venv active: True`。用户可能同时看到 `(base)` 与 `(.venv)`，应以实际解释器路径为准。

首次部署且没有 `.venv` 时：

```bat
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.lock.txt
python -m pip install -e .
python -m pip check
if not exist .env copy .env.example .env
```

先检查 `.env` 是否存在，再编辑需要的字段。不要覆盖已有密钥；不要将其内容完整回显。Joern 和 Java 不是 pip 依赖，需按组织允许方式单独准备。

Agent 工具可能每次启动新的 shell：上一条命令中的激活不会自动传到下一次调用。需要时在同一个 CMD 调用中执行激活与 Python 命令；不要依赖上一次工具调用的 PATH。

### 3.2 其他 shell

PowerShell 激活为 `.\.venv\Scripts\Activate.ps1`，变量写法是 `$snapshot = '实际路径'`，引用为 `"$snapshot"`。如果执行策略阻止激活，可使用 CMD 按上节执行，不需要修改系统执行策略。

Linux 为 `python3 -m venv .venv`、`source .venv/bin/activate`，随后使用 `python`；变量和路径使用 shell 对应语法。不要直接复制 CMD 的 `set`、`%VAR%` 或 PowerShell 的 `$env:`。

本项目已显式处理控制台 UTF-8 输出，文件读写明确指定编码；无需每次附加 `-X utf8`。

### 3.3 三类配置的分工

| 文件 | 管什么 | 修改后的影响 |
|---|---|---|
| `.env` | 模型连接、密钥、模型参数、并发、预算、Joern 路径 | 模型设置参与请求缓存键；解析配置与工具也可能影响快照 |
| YAML | 扫描范围、图视图、profile、发现采样、证据预算、解析宏和头文件 | 解析配置变化可能新建快照；预处理变化会重新准备包 |
| prompts | common 契约与 general/maintenance 的 discover/merge/extract 指导 | 请求缓存按实际提示词变化；已固定 schema 需显式更新 |

`.env` 至少检查 OPENAI_BASE_URL、OPENAI_API_KEY、OPENAI_MODEL、JOERN_HOME。优先级为系统环境变量高于 `.env`，配置文件值看似正确而行为不同，先检查覆盖来源，不要输出密钥。

接口需兼容 chat completions；BASE_URL 不填完整 `/chat/completions` 路由。内网模型名由该服务决定，不照搬外网的模型名。`LLM_THINKING=auto` 不发专有思考参数；仅在服务支持时设 enabled/disabled。

上下文/输出预算必须符合服务真实容量。当前 token 估算采用 UTF-8 字节保守上界，因此小窗口在中文 schema 合并时可能提前报预算不足；不能为了通过检查随意填超出模型能力的窗口。

### 3.4 策略配置

`configs/smoke.yaml`：综合 CPG + maintenance，schema 发现最多 8 包，每批最多 2 包；实例阶段仍使用纳入范围的全部 prepared packs。`configs/baseline.yaml`：三视图 × 两个 profile，发现最多 24 包。两者发现范围不同，不把 smoke 与 baseline 的结果差异全部归因于视图或提示词。

建议真实仓库另建自己的 YAML，而不是改示例配置：

```yaml
views: [cpg]
profiles: [maintenance]
include: ["src/monitor/*", "include/*", "config/*", "CMakeLists.txt"]
exclude: ["reference_answers", "generated"]
includes: ["include"]
defines: []
compile_commands: null
discovery_max_packs: 8
pack_tokens: 4500
batch_packs: 2
neighbor_limit: 3
graph_nodes_per_pack: 100
```

注意：YAML 中的列表替换默认对应列表，以上 `exclude` 会替换默认排除项。保留测试/第三方目录排除需求时，显式写全默认列表及追加项，或用命令行 `--exclude` 追加。支持的键以 `resources/default.yaml` 为准，未知键会拒绝。

include/exclude 匹配仓库相对路径，核对 manifest 实际文件清单。`includes` 的相对路径以输入仓库为基准；`compile_commands`、`prompt_dir` 的相对路径以 YAML 所在目录为基准。构建数据库只读取，不自动执行仓库构建脚本。需要分析测试文件时显式调整排除，并保留 test scope 的判断。

## 4. 标准执行流程：CMD 版

以下命令均在项目根目录、已激活 venv 的同一个 CMD 中执行。示例路径需要替换；不要将尖括号占位符直接粘贴到 CMD，尖括号有重定向含义。

### 4.1 环境诊断

```bat
python -m code_ontology doctor --no-llm
python -m code_ontology doctor --env-file .env
```

第一条不调用模型；第二条真实调用模型探测 JSON。检查退出码、终端结果和 `outputs/doctor/doctor.json`，不要只看命令已退出。

### 4.2 自带示例端到端

```bat
python -m code_ontology run --repo examples/maintenance --config configs/smoke.yaml --experiment intranet-demo-01 --env-file .env
```

记录打印的 SNAPSHOT 与 OUTPUT。前者保存共享解析产物，后者是本组实验目录。目录名不是固定常量，不能照抄外网快照哈希。

把实际 SNAPSHOT 路径填入：

```bat
set "SNAPSHOT=D:\实际输出根目录\实际源码快照ID"
set "RUN_DIR=%SNAPSHOT%\runs\intranet-demo-01\cpg\maintenance"
python -m code_ontology validate --snapshot "%SNAPSHOT%" --view cpg --run-dir "%RUN_DIR%"
python -m code_ontology evaluate --run-dir "%RUN_DIR%" --gold examples/maintenance/gold.json
```

validate 是结构和源码位置检查；evaluate 只做示例的概念别名召回导航。普通 run 不会自动完成这两步，不能报告成已经执行。真实仓库不能套用示例 gold。

### 4.3 真实模块

```bat
python -m code_ontology run --repo "D:\internal-repo" --config configs/internal.yaml --experiment internal-module-01 --out outputs/internal-pilot --env-file .env
```

`configs/internal.yaml` 需要先根据真实仓库创建并核对范围。人工参考答案放在排除范围；必要的配置和 CMake 应纳入。输出目录在仓库子目录内时会排除自身，但不能设成仓库根或仓库祖先。

先检查 manifest、图与证据包，再评价模型，避免解析不完整时反复调提示词。

### 4.4 六组对比

六组为 AST/CFG/CPG 分别配 general、maintenance。完整清单、逐组验收和人工审核步骤见 [六组基线与人工审核规程](BASELINE_REVIEW.md)，交付填写 [评审报告模板](templates/BASELINE_REVIEW_REPORT.md)。

```bat
python -m code_ontology experiment --repo examples/maintenance --config configs/baseline.yaml --experiment intranet-six-01 --env-file .env
```

真实仓库使用自己的范围配置，views/profiles 显式改为六组，并固定其他参数。不要与同一快照的解析任务并发。各组独立发现 schema 属于端到端比较，不能把差异都归因于实例抽取。

固定 schema 的对照实验见 `EXPERIMENTS.md`。schema 必须与当前快照证据兼容；跨仓库无证据的 schema 导入不是当前能力。

### 4.5 独立运行各阶段

```bat
python -m code_ontology parse --repo examples/maintenance --views ast,cfg,cpg --out outputs/staged --env-file .env
```

从打印结果重新设置 SNAPSHOT，随后：

```bat
python -m code_ontology prepare --snapshot "%SNAPSHOT%" --view cpg --config configs/smoke.yaml
python -m code_ontology discover --snapshot "%SNAPSHOT%" --view cpg --profile maintenance --experiment staged-01 --config configs/smoke.yaml --env-file .env
python -m code_ontology extract --snapshot "%SNAPSHOT%" --view cpg --profile maintenance --experiment staged-01 --schema "%SNAPSHOT%\runs\staged-01\cpg\maintenance\schema\schema.json" --config configs/smoke.yaml --env-file .env
python -m code_ontology report --snapshot "%SNAPSHOT%" --experiment staged-01
```

`extract` 在未指定现有 schema 时可能触发发现；要明确隔离阶段，应显式给 `--schema`。每一步使用一致配置，尤其是证据包预算，否则包内容变化可能触发重算与 schema 刷新。

## 5. 终端输出逐项解释

| 输出 | 意义 | 应检查的产物 |
|---|---|---|
| SNAPSHOT=... | 源码内容与解析配置标识的快照目录 | manifest、source、raw、normalized |
| RUN cpg/maintenance | 当前视图/profile 组合 | 对应 runs/<实验ID>/cpg/maintenance |
| STAGE discover: 4 tasks | 当前计划的局部 schema 发现任务 | schema/selection、schema/local |
| TASK discover 2/4 (2 successful) | 计划 4 个，已处理 2 个，成功 2 个 | 不表示整个流程已完成 |
| STAGE extract: 47 tasks | 固定 schema 后逐包抽取的任务数量 | extraction/selection 与批次输出 |
| OUTPUT=... | 本组最终产物目录 | final、metrics、report、failures |
| comparison.md 路径 | 此实验下已运行组合与缺失组合的报告 | 未运行组合会明确列出 |

schema 发现选 8 包、每批最多 2 包时通常是 4 个局部任务；预算不够时可能进一步分批。schema 合并会另调用模型，但目前没有逐轮终端进度。校验归并等步骤也没有每一项都打印。

一个任务不等于一个文件，也不等于一次 API 请求：可能修复、重试或命中缓存。若所有局部发现均成功、合并一次、抽取 47 次且无缓存，基础调用是 4+1+47；额外修复计入 requests，实际以 metrics/请求日志为准。

## 6. 每个阶段的输入输出与日志

| 阶段 | 输入 | 输出与日志 |
|---|---|---|
| 扫描 | 仓库、过滤与解析策略 | manifest.json、policy.json、source/ |
| Joern | 源码快照、编译信息 | raw/cpg.bin、logs/c2cpg.log |
| 导图/规范化 | 同一个 CPG | raw/cpg/{nodes,edges}.jsonl、raw/ast、raw/cfg、normalized/*、logs/export.log |
| 预处理 | 规范图、源码、预算 | prepared/<view>/packs.jsonl、index.json |
| 局部发现 | 选中证据包与 discover 提示词 | schema/local/*.json、schema/selection.json |
| 合并 | 局部 schema 与 merge 提示词 | schema/merge/、schema/schema.json、schema/provenance.json |
| 实例抽取 | 固定 schema、证据包、extract 提示词 | extraction/*.json、selection.json、任务 failed 文件 |
| 后处理 | 所有成功批次、schema、证据 | final/*.jsonl、final/schema.json、metrics、report |
| 独立校验 | 快照与正式输出 | validation.json |
| 人工评估 | 产物与人工判据 | reviews/*.csv、manifest、summary.json |

每组目录 `runs/<实验ID>/<view>/<profile>/` 中，优先读：

1. `report.md`：便于快速浏览，但不代替原始数据。
2. `metrics.json`：最近一次尝试的数量、请求、缓存、修复、token、耗时、失败。
3. `run_manifest.json`：实际设置、策略和 execution/discover/extract 状态。
4. `failures.jsonl` 与 `execution.failed.json`：当前失败信息；成功时前者可为空，后者通常不存在。
5. `llm_requests/`：完整渲染请求、原始回答和有效参数。

请求记录常见字段：parameters.messages 是实际提示词和任务输入；raw_content 是原始模型回答；finish_reason 是结束原因；usage 是服务返回的 token 用量；repair/attempt 表示修复和尝试。缓存命中记录较短，可指向已有缓存文件，不表示新调用了模型。

不是所有日志都叫 .log：Joern 用文本 .log，模型请求与阶段状态主要用 JSON/JSONL。当前不自动把整段终端输出另存为 run.log。如需独立进程日志，用 shell 的重定向并保存到输出目录；不要把含密钥的调试内容写进去。

## 7. 结果和质量怎么判断

先核对四个层次，不跨层下结论：

1. **流程成功**：阶段完成，任务失败为 0；有失败则标 partial/failed。
2. **结构正确**：JSON、类层级、端点、domain/range、值类型与证据引用合法。
3. **源码支持**：人工读源码确认语义、关系方向、值、单位、条件与动作。
4. **业务有用**：概念粒度合适、同义归并合理、关键业务问题有覆盖。

证据可定位率不是源码语义支持率。概念数量不是质量排名。实例的 scope 应区分代码定义、配置、测试和示例，不能推断为真实现场已经发生的事件。

### 7.1 需要人工检查的正式文件

- `final/schema.json`：classes、parents、relations、properties、unresolved。
- `final/entities.jsonl`：类型与对象身份、scope、basis、证据。
- `final/relations.jsonl`：关系含义、方向、端点与证据。
- `final/attributes.jsonl`：值、单位、条件；代码变量名不能随意当确定数值。
- `final/rules.jsonl`：条件/动作是否完整，是否遗漏宏或分支。
- `final/candidates.jsonl`：不符合当前 schema 或需复核的内容，既可能有错误，也可能有漏掉的正确知识。
- `final/conflicts.jsonl`：多个值需要判断，不自动覆盖为一个值。

### 7.2 人工评审工具

先按 [人工审核操作规程](BASELINE_REVIEW.md) 约定判据，再生成样本。首次每类最多 10 条，九个集合最多每组 90 条、六组 540 条；以下每类 20 条适合扩大审核。小型 schema 建议全审，关键控制规则另做逐条专项检查，不与随机样本混算。

```bat
python -m code_ontology.review sample --run-dir "%RUN_DIR%" --out reviews/internal-module-01.csv --per-kind 20 --seed 42
```

按集合分层抽样，不足 20 条时全取。填写 verdict/evidence_support/domain_relevant/scope_correct/rule_condition_complete 与 notes；不要改原始条目及 ID，保留旁边的 manifest。已有文件不会自动覆盖。

```bat
python -m code_ontology.review summarize --reviews reviews/internal-module-01.csv --out reviews/internal-module-01-summary.json
```

严格样本精确率把 partial 当作不完全正确；无法判定与未评审单列。跨集合汇总不是按总体分布加权后的全仓精确率。参考答案召回、业务问题覆盖需额外人工基准；工具不会凭空给出。

首次建议每类 10–20 条统一口径后扩大。Agent 可提供错误分析，但不能冒充人工专家填写“人工已验证”。完整评分值、实验矩阵和建议门槛见 `EXPERIMENTS.md`。

## 8. 可视化怎么启动和使用

另开终端，激活环境，然后：

```bat
python -m code_ontology.viewer --root outputs --port 8765
```

浏览器地址：`http://127.0.0.1:8765`。外部输出根目录可以通过 --root 指定。端口占用时先判断是否已有本项目服务可复用，或改用 8766。

| 页面 | 用途 |
|---|---|
| 本体图谱 | schema 概念、父类和业务关系；切换实例子图，点击证据 |
| 知识断言 | 实体、关系、属性、规则、候选、冲突的搜索分页 |
| 实验对比 | 相同快照下不同运行的规模、失败与成本；不自动排语义质量 |
| 阶段追踪 | 阶段输入输出与已存在产物摘要 |
| 人工评审 | 在浏览器保存标签并导出评审 JSON |

界面不调用模型、不触发 parse、不写入流水线正式输出。图只显示有限子图，必须留意上限；完整数据通过断言表查看。浏览器标签存于 localStorage，清理浏览器会丢失未导出记录；交互选择是非随机样本，不能充当总体精确率。

若界面异常，先看 viewer 终端与 API/静态资源是否可访问，再看浏览器错误；不要为修界面改动抽取逻辑。服务器地址已输出不等于已完成浏览器视觉验收，要明确报告验收方式。

## 9. 故障排查与恢复

| 现象 | 优先检查 | 恢复方式 |
|---|---|---|
| 找不到包 | 当前目录、venv、editable 安装 | 激活正确环境，python -m pip install -e . |
| Joern 不存在/CLI 能力不符 | JOERN_HOME、doctor、Java 与发行版 | 修复环境后重跑诊断；不伪造导图 |
| Joern/Scala 失败 | logs/c2cpg.log、export.log | 对照实际版本改适配层，并跑真实 Joern 示例 |
| 401/403、模型名错误 | .env 与环境变量覆盖、接口返回 | 修正配置，不无意义持续重试 |
| 网络/429 | 请求日志、timeout/retries/concurrency | 在服务允许范围内调整，原实验 ID 续跑 |
| 非法或空 JSON | llm_requests 原始回答 | 默认只修复一次；仍失败保留记录，再缩小任务或调整提示词 |
| finish_reason=length | 输出预算、任务复杂度 | 在服务合法容量内增预算或缩小范围，不把截断 JSON 当成功 |
| schema 合并无法缩减 | 候选规模、上下文预算、采样 | 缩小发现范围或调整合法预算，保留旧候选 |
| schema 改提示词后没变 | 是否复用了固定 schema | --update-schema 或新实验；只改 extract 提示词可保留 schema |
| 结果 partial | failed 文件、失败任务、run_manifest | 修正根因，原命令同实验 ID 续跑 |
| 概念/规则漏了 | 纳入文件→图→证据包→采样→局部发现→合并 | 找到首次丢失位置，再做局部修改 |
| 来源指纹不一致 | source 快照是否被改 | 重新通过源码扫描建快照，不篡改证据哈希 |
| 报告请求数突然为 0 | metrics 是否来自缓存续跑 | 报告为缓存恢复成本，别当首次生成成本 |

恢复原则：原命令、原配置、原实验 ID 复用成功任务；需要独立结果就换 ID。模型参数、提示词、预算或输入变化可能使缓存失效。不要先删除 outputs/cache，不要让多个进程写同一快照。

旧输出跨机器改变绝对路径可能影响 CPG/编译数据库；源码经 GitHub 迁移后建议在内网重新解析。不要把外网 .venv 当成可移植环境。

## 10. 测试、验收和交付

```bat
python -m unittest discover -s tests -v
python scripts/check_demo.py --real-joern
```

第一条使用本地模拟模型接口与测试数据；第二条使用真实 Joern + 模拟模型。两者均不等于真实模型抽取质量。修改 Joern 适配要验证真实导图，修改视图/证据预算要检查 prepared，修改界面至少验证 API/资源与实际可行的浏览器交互。

验收清单：

- [ ] 正确的 venv、Joern、Java 和内网模型配置。
- [ ] 示例全链路成功，真实仓库范围和解析限制明确。
- [ ] 快照、图、证据包、schema、实例均落盘可回查。
- [ ] execution、failures、独立 validate 状态已检查。
- [ ] 真实模型、模拟测试与人工检查的结论分别报告。
- [ ] 候选、冲突、源码不支持和条件遗漏有记录。
- [ ] 提示词变化与固定 schema 的更新方式明确。
- [ ] 缓存恢复与独立重复实验区分清楚。
- [ ] 没有密钥进入提交或交付文档。
- [ ] 浏览器验证是否完成如实说明。

给用户的最终报告至少包含：

```text
任务：首次部署 / 模块抽取 / 实验比较 / 排障
环境：Python、Joern/Java、模型（不含密钥）
输入：仓库、文件范围、宏/头文件/编译数据库限制
执行：实际命令、配置和实验 ID
产物：实际 SNAPSHOT、OUTPUT、报告和日志路径
状态：阶段完成、任务失败、修复和缓存情况
质量：结构校验、人工样本分子分母、典型错误与未评审部分
后续：一到三条可执行的下一步
```

## 11. 可直接给内网 Agent 的接手提示词

复制下面文字，并补充输入仓库与实际任务：

> 请先阅读本项目根目录 AGENTS.md 和 docs/INTRANET_AGENT_GUIDE.md，按实际环境接手。先检查已有配置、快照、运行状态和日志，复用可用产物。Windows 下先激活 venv，再用 python；不要混用 CMD 和 PowerShell 语法。不要输出密钥，不自动切换公网模型，不修改输入代码或生成的源码快照。请完成我指定范围内的部署/抽取/排障任务；保留图、证据包、schema、实例、请求日志与失败记录。明确区分流程成功、结构校验和语义质量。结束时给出实际命令、产物路径、检查结果和未解决问题。输入仓库与本次任务如下：……

## 12. 补充文档

- [CMD 完整运行说明](RUNNING_CMD.md)：人工复制命令。
- [跨平台运行说明](RUNNING.md)：PowerShell/Linux 与环境准备。
- [每阶段输入输出](PIPELINE_IO.md)：数据契约和回查路径。
- [实验与质量评估](EXPERIMENTS.md)：公平比较、人工评分与实验顺序。
- [六组基线与人工审核](BASELINE_REVIEW.md)：六组清单、CMD 命令、逐类判据与评审分工。
- [空白评审报告模板](templates/BASELINE_REVIEW_REPORT.md)：记录分子分母、六组覆盖与典型错误。
- [实现说明](ARCHITECTURE.md)：模块边界、格式、预算与缓存。
- [排障说明](TROUBLESHOOTING.md)：常见环境与运行故障。
- [历史验证记录](VALIDATION.md)：外网示例验证，不能代替内网验收。
