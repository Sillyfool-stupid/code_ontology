# 内网逐步测试与独立 Schema 合并

更新：2026-11-24。常用配置集中在 `.env`；新增阶段已实现，旧 CLI 和 YAML 保留兼容。默认中英文混合，源码标识符不强制翻译。无需为每个函数单独调用模型生成说明。

## 1. 配置一次

首次部署先创建本机虚拟环境并安装依赖，不跨机器复制 `.venv`。在 Windows CMD 中：

```bat
cd /d "D:\code\code-ontology"
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.lock.txt
python -m pip install -e .
```

已经完成环境准备则跳过以上安装。没有 `.env` 时才从 `.env.example` 复制；已有 `.env` 保留密钥和端点，参照模板补充 `CO_*` 配置。不要打印或提交真实环境文件。

主要填写：

```dotenv
CO_REPO=D:/repos/my-project
CO_OUTPUT=D:/ontology-results/my-project
CO_EXPERIMENT=joern-ast-01
CO_PARSER=joern
CO_VIEW=ast
CO_PROFILE=general
CO_OUTPUT_LANGUAGE=mixed
CO_DISCOVERY_MODE=full
JOERN_HOME=D:/joern/joern-cli
OPENAI_BASE_URL=http://your-intranet-model/v1
OPENAI_API_KEY=unused
OPENAI_MODEL=your-intranet-model
```

`JOERN_HOME` 指向包含 `joern.bat` 和 `c2cpg.bat` 的目录。Tree-sitter 使用 `CO_PARSER=tree-sitter`、`CO_VIEW=ast`，不要求 Java 或 Joern。路径相对于所选 `.env` 目录解析；启动器从项目根执行。`CO_OUTPUT` 不能是输入仓库或其祖先，建议放在仓库外。

列表采用 JSON 数组，例如 `CO_INCLUDE_DIRS=["include"]`、`CO_SCAN_EXCLUDE=[".git","build","outputs"]`。`CO_SCAN_MAX_FILES=0` 为不限文件数；`CO_DISCOVERY_MODE=full` 为全部准备包参与发现；`sample` 使用 `CO_DISCOVERY_SAMPLE_LIMIT`。支持的源码/文本扩展名、扫描排除和文件大小上限仍生效，不等于扫描磁盘上任意文件。

配置优先级：显式 CLI 参数 > 系统环境变量 > `.env` > 显式 YAML > 内置默认。旧 CLI 的 `--exclude` 保留追加语义；环境列表整体替换。网页任务提交时冻结配置和提示词。脚本每次执行单步时解析一次配置、保存脱敏副本；下次命令可使用新预算，不影响已启动的当前阶段。

模型连接、密钥、模型名仅配置一次。`LLM_*` 为全局默认，下面三组允许分别覆盖 `CONTEXT_TOKENS`、`MAX_OUTPUT_TOKENS`、`TIMEOUT_SECONDS`、`MAX_RETRIES`、`CONCURRENCY`，留空继承：

```dotenv
LLM_DISCOVER_MAX_OUTPUT_TOKENS=4096
LLM_MERGE_MAX_OUTPUT_TOKENS=8192
LLM_MERGE_TIMEOUT_SECONDS=1200
LLM_MERGE_CONCURRENCY=1
LLM_EXTRACT_MAX_OUTPUT_TOKENS=4096
```

以上输出值只是写法示例，必须满足内网服务容量和输入余量。提高合并输出不再影响局部提取或实例请求。`CO_MERGE_BATCH_ITEMS` 为合并批次条数上限，实际还受输入预算限制；合并当前逐批执行，`LLM_MERGE_CONCURRENCY` 是并发上限，设为大于 1 不会使当前顺序合并自动并行。`CO_DISCOVERY_BATCH_PACKS` 控制局部任务大小；实例默认每包一次，截断后再分片。未引入不生效的实例批量开关。

## 2. Windows 内网短命令

项目根目录下执行，启动器自动使用本项目 `.venv`：

```bat
scripts\intranet.bat config
scripts\intranet.bat check
scripts\intranet.bat smoke
scripts\intranet.bat doctor --with-llm
scripts\intranet.bat init

scripts\intranet.bat scan
scripts\intranet.bat parse
scripts\intranet.bat normalize
scripts\intranet.bat prepare
scripts\intranet.bat discover-local
scripts\intranet.bat status
scripts\intranet.bat merge-schema
scripts\intranet.bat extract
scripts\intranet.bat finalize
scripts\intranet.bat validate
scripts\intranet.bat report
```

逐条执行，检查成功后继续。`check`/默认 `doctor` 不调用真实模型；`doctor --with-llm` 才测试配置的真实端点。`smoke` 使用真实 Tree-sitter + 内置本地模拟模型接口，不需要真实密钥、不读取用户模型参数，不评估语义质量。自检报告为项目 `outputs/intranet-smoke/verification.json`，用 `--out PATH` 可另选输出根。

也可直接使用 Python：

```bat
call .venv\Scripts\activate.bat
python scripts/intranet.py init
python scripts/intranet.py merge-schema
```

Linux/Git Bash 使用 `sh scripts/intranet.sh merge-schema`，普通 CMD 不直接运行 `.sh`。所有脚本使用同一 Python 入口，不复制阶段实现。

自定义环境文件统一加 `--env-file D:/configs/project.env`；可选 `--config PATH` 使用历史 YAML。同一会话的 `--out`、`--experiment` 和 `--env-file` 应保持一致。`init --repo PATH` 支持覆盖仓库路径；后续 `.env` 中如设置了不同的 CO_REPO，会明确报身份不符。

会话保存实际快照路径，不选择“最新目录”。修改仓库/view/profile 时使用新实验 ID 并 init。`scan` 后输入变化，parse 拒绝使用旧快照，要求重新 scan 检查。脚本与工作台共用输出根的 OS 写锁；不要在它们运行时用历史 CLI 并发写同一产物，历史 CLI 为兼容保留，未纳入启动器的父进程锁。

显式串联流程可以使用：

```bat
scripts\intranet.bat run --to discover-local
scripts\intranet.bat merge-schema
scripts\intranet.bat run --to report
```

`run` 从 scan 开始核对并复用已有结果，失败即停止。普通单步只运行指定阶段，缺少依赖时提示，不暗中发现或调用模型。重新 parse 原始图仍会检查缓存；normalize 是独立步骤。

## 3. 阶段边界和产物

设 S 为 status 显示的快照，R 为 `S/runs/实验ID/视图/profile`：

| 阶段 | 输入 | 主要输出 | 模型 |
|---|---|---|---|
| scan | 输入仓库、扫描/解析配置 | S/manifest.json、source、policy.json | 否 |
| parse | 已扫描快照，核对当前仓库未变 | raw/cpg.bin 与 raw/cpg；或 raw/tree-sitter | 否 |
| normalize | 完整原始图 | normalized/视图、graph.sqlite、symbols.jsonl | 否 |
| prepare | 规范图、源码、预算 | prepared/视图/packs.jsonl、index.json | 否 |
| discover-local | 当前证据包、发现参数/提示词 | R/schema/local/、local-manifest.json、selection.json | 是 |
| merge-schema | 当前且完整的局部发现清单 | R/schema/merge/、rounds/、merge-status.json、schema.json | 是 |
| extract | 固定 Schema、准备包 | R/extraction/manifest.json、selection.json、每批 JSON | 是 |
| finalize | 成功且指纹匹配的实例批次 | R/final/各类 JSONL、metrics.json、report.md | 否 |
| validate | final、源码证据 | R/validation.json | 否 |
| report | 最终指标和产物 | R/report.md | 否 |

Joern parse 仍包含从 cpg.bin 导出原始节点边，这是一项解析器适配操作；不再隐式规范化。旧 `python -m code_ontology parse` 默认保留“扫描+解析+规范化”兼容行为，用 `--raw-only` 可跳过规范化。旧 discover 仍为发现+合并；新增 `discover-local` 与 `merge-schema` 独立入口。旧 extract 默认归并，`--defer-finalize` 可仅生成批次。新内网脚本默认按表格边界执行。

final 包含 schema.json、entities.jsonl、relations.jsonl、attributes.jsonl、rules.jsonl、candidates.jsonl、conflicts.jsonl。它们不是每个函数的功能文档。业务知识保留 evidence_ids；不强制把所有工具类变成业务概念。

会话及逐阶段日志：`CO_OUTPUT/sessions/实验ID.json`、`sessions/实验ID/阶段-时间戳.log` 和 `.config.json`。模型原始回答、finish_reason、参数在 R/llm_requests。日志记录北京时间并带 +08:00；历史 UTC 记录保持原值，网页任务展示时转换为北京时间。

## 4. 合并耗时长时单独恢复

局部发现结束后可以退出，再单独执行 merge-schema。合并检查局部清单、全部包、采样范围、提示词及发现参数指纹；不匹配或部分完成时要求重跑 discover-local，不混用目录里残留的旧 JSON。

合并首先按源码目录分组处理，再多轮归并。每批检查点按输入、实际提示词、该阶段生效参数和契约计算键。调用开始、轮次/批次和日志路径均输出；模型等待每 15 秒报告已等待时长。这里目录分组不是编译器确认的业务模块，也不代表已解析跨文件调用。

```bat
scripts\intranet.bat status
scripts\intranet.bat merge-schema
```

原命令重跑复用已成功批次，局部发现请求新增为零。修改 LLM_MERGE_* 只影响合并检查点，不使局部模型缓存失效。仅修改超时或重试值也属于该阶段参数变化，会重新计算该阶段键；原始旧结果仍保留。

截断与超时处理：

- finish_reason=length 仍被检测并保留原始回答。局部发现/实例任务自动拆分，成功子任务可复用；不会接受可解析但已截断的 JSON。
- 合并批次截断时拆成更小批次；若无法继续缩减，保存部分状态，保留完整候选，停止宣称全局成功。单个 Schema 本身太大时不保证可以自动拆成满足所有引用的更小 Schema。
- 预算/恢复次数有界。不能简单拼接续写 JSON，也不能靠无限 timeout/max_tokens 保证完整。
- 客户端超时、HTTP 408/504 标为 unknown，不立即重发。先检查服务端是否仍计算，再显式运行原命令。普通 chat completions 无任务查询能力时，无法保证取回已断开的回答，也无法保证显式重试不会产生重复服务端计算。
- 其他连接错误仍使用有限重试；鉴权/配置错误快速失败。进度只反映已知任务和已等待时间，不伪造模型内部百分比。

合并产物：

```text
schema/local-manifest.json   局部成功范围、参数与候选
schema/merge/*.json          按内容寻址的成功批次
schema/merge/*.task.json     每批任务状态、失败或 unknown
schema/rounds/*.json         各轮中间结果
schema/merge-status.json     当前合并状态
schema/unresolved.jsonl      未按名称/别名匹配到的来源项
schema/module-schemas.json   合并失败时保留的候选集合
schema/schema.json          最近成功合并的全局 Schema
```

`module-schemas.json` 是候选集合，不等于已对齐的全局本体；旧 schema.json 可能仍在磁盘，必须结合 merge-status 和指纹检查，不能仅判断文件存在。遗漏项不静默丢弃，会使本轮合并标记 partial。名称匹配只是保守检查，可能包含真正同义词，需人工核对。

如果模型无法容纳全局 Schema，可由人工选择某个已保存候选中的 `schema` 对象，另存为合法 Ontology JSON，并在独立实验中显式指定：

```bat
scripts\intranet.bat extract --schema D:/ontology-results/reviewed-module-schema.json
```

显式选用的 Schema 必须引用当前快照有效证据；schema-scope.json 标明人为选定范围，不能当作全局合并完成。不要手改原检查点或原始回答。新实验需先 init、scan、parse、normalize、prepare，原始解析缓存可在相同输出根复用。

## 5. 网页和验收

```bat
call .venv\Scripts\activate.bat
python -m code_ontology.workbench --port 8766
```

YAML 可留空，从所选 `.env` 读取常用策略。网页新增“局部 Schema 提取”“合并 Schema（可独立恢复）”“归并实例产物”；原“发现并合并 Schema”作为兼容组合入口保留。网页抽取后需单独归并再 validate。网页 parse 保留已有扫描保护和解析+规范化组合；更细边界使用内网脚本。

验证命令：

```bat
python -m unittest discover -s tests -v
python scripts/check_demo.py --real-joern
python scripts/check_workbench.py --tree-sitter --out outputs/check-workbench-ts
python scripts/check_workbench.py --real-joern --out outputs/check-workbench-joern
```

上述集成模型均为本地模拟接口。测试覆盖阶段恢复、截断拆分、客户端/网关超时、阶段参数隔离、旧 CLI、真实 grammar、网页任务及本机真实 Joern。自动化通过不代表内网真实模型语义准确；真实代码应抽查概念、规则条件、单位、宏限制和跨文件解释。

2026-11-24 本机验收：53 项单元/集成测试通过（74.136 秒）；真实 Joern 六组编排、Tree-sitter 两组编排均通过，重复实例请求新增 0；Windows `.bat smoke` 完整自检通过、合并/实例续跑新增请求 0。记录位于 `outputs/intranet-final-tests.log`、`outputs/intranet-final-workbench-joern/verification.json`、`outputs/intranet-final-workbench-ts/verification.json`、`outputs/intranet-final-smoke/smoke/verification.json`。前端 JS 通过语法检查；未进行浏览器人工点击目测。Linux 启动器未在 Linux 主机实测。
