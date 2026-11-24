# 分阶段可视化工作台使用说明

2026-11-24 更新：新增独立“局部 Schema 提取”“合并 Schema”“归并实例产物”按钮；网页 extract 生成批次后单独 finalize，再校验。原 discover 组合按钮仍保留。YAML 可留空，常用策略读取 .env；合并参数与发现/实例分开。产物和恢复边界见 [内网逐步脚本](INTRANET_STEPS.md)。下文旧表格中的 extract 包含归并描述适用于历史 CLI，不再是网页独立按钮的行为。

## Tree-sitter 模式（2026-11-24）

没有 Joern 时，YAML 选择 `configs/tree-sitter.yaml` 后保存工作区。内置解析器直接读取源码，后续各阶段使用 AST；界面显示限制并将实验改为 AST 两组，验收不要求 cpg.bin。默认 auto 在 Joern 工具缺失时也会采用此模式。安装、产物和独立命令见 [TREE_SITTER.md](TREE_SITTER.md)。以下 Joern 六组描述只适用于 Joern 后端；外部任意 Tree-sitter JSON 导入仍需适配。


工作台将现有 CLI 包装为阶段按钮。源码扫描、Joern/Tree-sitter 解析、证据包、模型发现、实例抽取和文件契约保持原流程；原只读 viewer 仍可单独启动。该功能随主分支交付。

## 1. 启动：Windows CMD

在项目根目录执行，使用本项目的虚拟环境：

```bat
cd /d "D:\code\f47-work\code-ontology"
call .venv\Scripts\activate.bat
python -c "import sys; print(sys.executable)"
python -m code_ontology.workbench --port 8766
```

解释器应位于 `code-ontology\.venv`。保持终端运行，浏览器地址栏打开 `http://127.0.0.1:8766`。PowerShell 激活写作 `. .\.venv\Scripts\Activate.ps1`，后续也使用 `python`。不用 `-X utf8`，也无需安装 Node/npm。

移植到内网：首次按 [CMD 安装说明](RUNNING_CMD.md) 重建虚拟环境、安装项目依赖，按所选后端准备解析器依赖，配置本机 `.env`。不要复制外网机器的虚拟环境，不覆盖已有环境文件。模型只读取选择的环境文件及系统环境变量；系统变量优先。

## 2. 第一次打开怎么操作

1. 工作区名称自定；实验 ID 如 `ui-smoke-01`。同 ID、同配置用于续跑，新对照使用新 ID。
2. 输入仓库选择 `examples/maintenance` 或自己的仓库。输出根目录可选择已有目录或直接输入新目录，保存时创建。
3. YAML 首次选 `configs/smoke.yaml`；环境文件选自己的 `.env`。选择 `cpg` 与 `maintenance`。
4. 已有快照可留空。保存后先点“扫描仓库”，快照字段自动填入。已有输出则点“查找快照”，选择后保存。
5. 示例可选择 `examples/maintenance/gold.json`。真实仓库需要对应人工参考，不要套用示例 gold。
6. 每个阶段先查看计划，再执行。计划列出阶段真实输入与命令，模型阶段有明确标识。

目录弹层浏览的是运行 Python 的电脑，不会将仓库上传到浏览器。默认允许浏览项目的父目录、已保存工作区的输入/输出/配置目录；其他位置可直接填绝对路径并保存，或启动时添加允许浏览的根目录：

```bat
python -m code_ontology.workbench --port 8766 --allow-root "E:\repos" --allow-root "E:\ontology-results"
```

输出不能是输入仓库自身或祖先。输出在输入仓库子目录时，原扫描流程会排除该输出目录。

## 3. 按钮顺序、输入与输出

以下 `snapshot` 为所选快照，`run` 为 `snapshot/runs/实验ID/视图/profile`。

| 按钮 | 主要输入 | 主要输出 | 是否调用模型 |
|---|---|---|---|
| 检查环境 | 环境文件、Joern/Java | 独立诊断目录的 `doctor.json`、工具日志 | 否 |
| 测试模型连接 | 同上及模型参数 | `doctor.json`、`llm_requests/`；同时核对工具 | 是，小型 JSON 探测 |
| 扫描仓库 | 仓库、范围和解析设置 | `manifest.json`、`policy.json`、固定 `source/` | 否 |
| 解析并导出结构 | 仓库、与扫描相同的设置 | `raw/cpg.bin`、原始图、三视图 `normalized/`、`logs/` | 否 |
| 生成模型输入包 | 所选规范图、源码、预算 | `prepared/视图/packs.jsonl`、`index.json` | 否 |
| 发现并合并 Schema | 准备包、提示词、模型设置 | `run/schema/selection.json`、`local/`、`merge/`、`schema.json`、`provenance.json` | 是；固定 Schema 复用时可能零调用 |
| 抽取实例与规则 | 全部准备包、固定 Schema、提示词 | `run/extraction/`、`final/`、`evidence.jsonl`、`metrics.json`、`failures.jsonl`、`report.md` | 是；可命中缓存 |
| 校验结果 | 包、源码、最终 Schema/断言 | `run/validation.json` | 否 |
| 匹配参考概念 | 最终 Schema、所选 gold | `run/evaluation.json` | 否 |
| 生成对比报告 | 实验下已有组指标和状态 | `runs/实验ID/comparison.md`、`comparison.json` | 否 |
| 生成审核表 | 本组最终结果、固定采样种子 42 | `输出根/reviews/实验ID/视图-profile.csv` 及采样 manifest | 否 |
| 汇总人工审核 | 同一 CSV 中人工填写的标签 | 同目录 `视图-profile.summary.json` | 否 |
| 运行六组基线 | 仓库、baseline 策略、模型、两套提示词 | 三视图 × 两个 profile 的六组产物与比较报告 | 是 |

第一版保留真实阶段边界：parse 包含解析/导出/规范化；discover 包含局部发现/合并；extract 包含实例抽取/归并。没有“单独只合并一次”等虚构按钮。每个普通按钮只执行所标阶段；缺依赖会阻塞，不自动补跑上游。六组基线按钮是显式的整条流水线入口。

原 CLI 的 discover/extract 仍会核对 prepare；工作台事先校验证据包指纹，图或预算不一致时要求先点 prepare。解析前重新核对源码：若 scan 后仓库已变化，创建并选中新快照，解析任务失败并说明此次未运行解析器；确认新清单后再次执行解析。

## 4. 在哪里看输入、输出和日志

“分阶段执行”右侧列出实际输入文件路径，快捷按钮打开对应产物。“输入与产物”可以从输入仓库、配置目录、快照、本组实验、诊断、人工审核目录逐层浏览。打开证据包可看源码、图上下文、位置和证据 ID；发现/实例阶段的“实际模型请求”打开真实请求记录，包含渲染消息、有效参数、回答和校验信息。提示词页展示的是模板与契约，须与已执行请求区分。

JSONL 首次访问建立 SQLite 行偏移索引，随后按记录分页；每页最多 20 条（API 上限 100 条），内容预算 128 KiB。搜索每次最多检查 1000 条，点“下一段”继续。超大单条记录只预览片段，搜索也只覆盖片段。普通 JSON、日志、源码按字节分段，不在浏览器整份加载。首次索引大文件可能等待，期间保持页面。完整文件保留在磁盘；网页仅为 `.bin`、`.sqlite` 提供下载，其余文件使用分页预览或从磁盘取得。

任务区持续显示当前阶段、组别、完成/成功计数和退出码。无可靠总量的 Joern/合并过程不显示虚假百分比。终端原输出保存在 `.workbench/jobs/任务ID/console.log`。CLI 自有 Joern 日志位于快照的 `logs/`，模型请求位于本组的 `llm_requests/`。

任务附件按钮可看本次 `task.json`、冻结 `policy.yaml`、实例阶段的 `input-schema.json`、抽取后的 `metrics.json` 副本。提示词副本也在任务目录。控制状态存放 `.workbench/tasks.sqlite`，不会随业务产物重新解析；`.workbench`、输出与密钥均已排除 Git。

## 5. 提示词、旧结果和失败恢复

“提示词与模型”展示脱敏模型设置及策略。编辑 discover/merge/extract 的领域指导并保存，会复制完整提示词目录、保留 common 契约，生成独立策略文件，再切换当前工作区；不会覆盖仓库模板。模型地址、名称、密钥仍在 `.env` 中修改。

任务提交时冻结策略、提示词和有效模型参数；密钥仅保留在任务执行内存和子进程环境中，不写入控制文件。修改磁盘配置不会改变正在执行的任务。任务结束后与当前配置比较，变化显示“配置/输入版本已变化”。源码外部编辑须重新扫描核对快照；不要将状态提示理解为持续监控所有源码文件。

已有 Schema 默认复用。改 discover/merge 提示词后，执行计划中勾选“显式更新已有 Schema”，或换新实验 ID。若复用的 Schema 与当前发现提示词不符，任务历史明确提示。更新 Schema 后重新抽取实例，再校验和审核；旧审核标签不可直接套用。

任务退出码、必要输出文件和报告状态共同决定成功。失败、部分成功、取消时旧产物仍保留供核查，不代表本次成功。修改提示词不要求重新解析；预算变更重做 prepare；同配置重试失败阶段可复用已有成功模型请求。

一个工作台一次只执行一个任务；重复点同阶段返回同任务。共享输出根目录的工作台执行时还有写锁。**不要同时用独立 CLI 向相同快照/实验写入**，普通 CLI 不持有工作台锁。

“停止任务”会终止 Python/Joern/Java 进程树，待进程真正退出才释放任务锁。若系统权限阻止终止，显示错误并保留停止中状态，可重试。关闭网页不停止后台任务；用终端 Ctrl+C 停止服务会尝试取消任务。

服务异常退出后旧任务显示 interrupted。先由人工或内网 Agent 检查并结束确定属于该任务的残留进程，再点击“确认已检查残留进程”，然后重试原阶段。不要仅凭历史 PID 杀进程；服务不会盲目终止可能已经复用的 PID。

## 6. 六组实验与人工审核

单组闭环后，在设置中选择 `configs/baseline.yaml` 或对应真实范围的 baseline 配置（发现上限 24），换一个新实验 ID，保存，再执行“运行六组基线”。实际执行顺序与原 experiment 一致，父任务日志显示组别；比较内容见 [六组基线与人工审核规程](BASELINE_REVIEW.md)。

六组完成后，更改工作区的视图/profile 并保存，依次为各组执行独立校验、适用的 gold 匹配、生成审核表。工作台不把整条 experiment 自动当作六组均已人工审核。所有指标以“输入范围一致、源码真正支持、业务意义、同义归并、条件完整、实例范围准确”等判据分析，不能只比较概念数量。

“本体与人工审核”复用原 viewer。上方可以选择“当前发现 Schema”或“最终本体与实例”：前者直接读取 `schema/schema.json`，完成 discover 即可使用，不会加载旧实例；后者展示与已有实例配套的 `final/schema.json`，发现版本不同会提示。当前工作区的实验优先显示，仍需核对 viewer 内选中的实验 ID。discover 阶段还有“可视化当前 Schema”快捷按钮。

概念为图节点，业务关系和继承为边；点击节点查看定义、别名、属性、父类与证据，点击关系查看关系定义和源码。可滚轮缩放、拖动平移、搜索筛选、导出 SVG。为保证可用性，每张子图最多 80 个节点、160 条边；完整 JSON 保留在产物目录。最终模式可切到抽取实例，查看实体关联、知识断言、对比报告和源码。没有出现最新产物时，点击“重新加载产物”。

页面交互评审保存在浏览器本地，及时导出；正式随机评审建议使用“生成审核表”的分层 CSV，人工填后再汇总。Agent 不得代填“人工正确”。不同采样方法不可混为同一质量指标。

## 7. 当前边界与验证

任意 tree-sitter JSON 的字段尚未适配。工作台支持内置 Joern/Tree-sitter 解析及项目自身文件契约；已有规范图可以先使用 CLI `import-graph` 导入，再选快照继续。内网的 30 万行 tree-sitter 数据需要先对齐源码路径、类型/函数、节点边和证据映射；没有真实 CFG 时不能伪造 CFG 对照。分页浏览支持不等于已支持任意 JSON 转换。

本次不新增“自动运行到某步”的依赖链、通用导入按钮、独立合并按钮或页面密钥编辑。核心 prepare/Pipeline 仍有原有内存使用边界；前端分页只保证预览不整份发送大文件，不声称核心可以无限流式处理。

验证命令（已激活 `.venv`）：

```bat
python -m unittest discover -s tests -v
python scripts/check_workbench.py --real-joern
```

第二条使用真实 Joern、本地模拟模型，执行独立阶段、缓存续跑与六组基线；证据在 `outputs/workbench-verification/verification.json`。模拟测试只验证流程与契约，不证明真实模型语义质量。Windows 取消测试需要允许终止自己创建的子进程，受限沙箱可能阻止 taskkill，应在正常终端执行。

浏览器预览授权在本次会话被拒绝，因此本次不宣称完成页面目测和浏览器点击验收；自动检查覆盖接口、静态资源、真实子进程和阶段产物。可在本机启动后按第 2–6 节进行实际页面验收。
