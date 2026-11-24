# 代码业务本体抽取基线

输入一个 C/C++ 仓库路径，使用 **Joern（AST/CFG/综合 CPG）或 Tree-sitter（AST）→ 证据包 → LLM 发现 schema → 合并固定 schema → 实例抽取 → 校验和对比报告**。

**没有 Joern 时可直接使用 Tree-sitter，无需 Java 或编译器。** 默认 `parser_backend: auto` 优先 Joern，找不到工具时使用 Tree-sitter AST；也可用 `--parser-backend tree-sitter` 强制选择。安装依赖后最短命令：

```bat
call .venv\Scripts\activate.bat
python -m pip install -r requirements.lock.txt
python -m pip install -e .
python -m code_ontology doctor --parser-backend tree-sitter --no-llm
python -m code_ontology parse --repo examples/maintenance --config configs/tree-sitter.yaml
python -m code_ontology run --repo examples/maintenance --config configs/tree-sitter.yaml --profile maintenance --experiment ts-smoke-01 --env-file .env
```

最后一条使用 `.env` 中的模型；前面的诊断和解析不调用模型。完整安装、独立阶段、AST 两组实验、工作台与产物说明见 **[Tree-sitter 使用方法](docs/TREE_SITTER.md)**。Tree-sitter 只提供语法结构，不产生 CFG、CPG、调用解析或数据流。

100 个 cpp、50 个 h、7 万行（5.6 万有效代码行）的模拟仓库生成、实测结果与大模型方案评估见 **[规模测试与抽取策略评估](docs/SCALE_TEST_150.md)**。测试使用真实 Tree-sitter 和本地模拟模型接口，不代表真实模型的语义准确率。

**内网交给 Agent 接手时：先读 [AGENTS.md](AGENTS.md)，再读 [内网 Agent 使用与接手手册](docs/INTRANET_AGENT_GUIDE.md)。手册末尾附有可直接复制的接手提示词。**

支持通用 `general` 和设备运维 `maintenance` 两种提示词。Joern 三种视图共六组组合；Tree-sitter AST 共两组组合，报告按实际后端列出可用组合。

六组逐项清单、CMD 批量检查命令、人工源码审核判据与指标口径见 **[六组基线与人工审核规程](docs/BASELINE_REVIEW.md)**；结果可填写 **[评审报告模板](docs/templates/BASELINE_REVIEW_REPORT.md)**。

当前版本交付 JSON/JSONL、Markdown 与本地浏览界面；RDF、图数据库、完整 CMake 求值和数据流推理不在基线中。

**分阶段工作台**：选择输入/输出目录，用按钮逐步扫描、Joern/Tree-sitter 抽取、准备模型输入、发现 Schema、抽取实例、校验和审核；可查看每步产物、日志与真实模型请求。使用项目自带虚拟环境，在 CMD 启动：

```bat
cd /d "D:\code\f47-work\code-ontology"
call .venv\Scripts\activate.bat
python -m code_ontology.workbench --port 8766
```

浏览器打开 `http://127.0.0.1:8766`。操作步骤、输入输出、六组实验和恢复方法见 **[工作台使用说明](docs/WORKBENCH.md)**。不需要新增 Python 依赖或 npm 构建。

新增独立的本地只读界面，支持本体 / 实例子图、断言分页、源码证据、实验对比、阶段追踪和人工评审；不改变现有抽取逻辑。图数据库和 RDF/OWL 推理仍未实现。

使用 CMD 请看 **[CMD 完整运行步骤](docs/RUNNING_CMD.md)**，PowerShell/Linux 请看 **[完整运行步骤](docs/RUNNING.md)**；内网实验与判优方法见 **[实验评估手册](docs/EXPERIMENTS.md)**，每阶段输入输出见 **[数据流说明](docs/PIPELINE_IO.md)**。

直接查看本机已有结果：

```powershell
.\.venv\Scripts\python.exe -m code_ontology.viewer --root outputs --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

## 1. 环境和安装

Python 3.10+；Tree-sitter 依赖已纳入安装清单。选择 Joern 后端时需独立安装 Joern 4 / flatgraph，并配置 `JOERN_HOME` 或 PATH；外网实测 Joern 4.0.684 与 Java 25.0.5.1。使用与所安装 Joern 发行版兼容的 Java。

Windows PowerShell，在本项目目录运行：

```powershell
# 本次交付已在本机建立 .venv；复制到另一台机器后必须重新创建。
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install -e .
```

若默认 PyPI 不可用，可给 pip 显式传入组织允许的 `--index-url`。锁定清单来自本次 Windows/Python 3.10 的验证环境；其他平台使用同版本可用 wheel，`colorama` 无需平台特性。不要跨系统直接搬运 `.venv`。

## 2. 配置模型

本机 `.env` 已配置并验证 DeepSeek，密钥不写入版本控制或交付包。复制到内网后，从 `.env.example` 创建自己的 `.env`，填写内网接口与实际模型名称。

```dotenv
OPENAI_BASE_URL=http://<内网地址>:<端口>/v1
OPENAI_API_KEY=unused
OPENAI_MODEL=<内网实际 DeepSeek 模型名称>
JOERN_HOME=D:/joern/joern-cli
LLM_CONTEXT_TOKENS=16384
LLM_MAX_OUTPUT_TOKENS=4096
```

接口必须兼容 `POST /chat/completions`。不需要鉴权时使用服务允许的占位密钥；程序没有自动回退到外网服务的逻辑。

优先级：系统环境变量覆盖 `.env`。`--env-file` 可选择另一文件。模型相关限制均在 `.env.example` 中；扫描与分块策略在 YAML 中。提示词在 `code_ontology/resources/prompts/`，可直接编辑，也可通过配置的 `prompt_dir` 指向自定义目录。

`LLM_THINKING=auto` 不额外发送思考参数，适合通用兼容接口。支持该参数的 DeepSeek 可设为 `disabled` 或 `enabled`；启用时不发送 temperature。本机示例使用 `disabled`、65536 上下文预算和 8192 输出预算，避免中文 schema 的保守预算检查阻止合并。迁移时按内网模型的真实容量设置，不能任意扩大。

## 3. 最短运行路径

```powershell
# 不调用模型，检查所选解析器（auto 优先 Joern，缺失则 Tree-sitter）
.\.venv\Scripts\python.exe -m code_ontology doctor --no-llm

# 填好 .env 后检查真实模型连接与 JSON 输出
.\.venv\Scripts\python.exe -m code_ontology doctor --env-file .env

# 第一轮只跑综合 CPG + 运维提示词；发现采样 8 包，实例抽取覆盖纳入范围的全部包
.\.venv\Scripts\python.exe -m code_ontology run --repo examples/maintenance --config configs/smoke.yaml --experiment smoke

# 六组比较：一次解析，三种视图 × 两套提示词
.\.venv\Scripts\python.exe -m code_ontology experiment --repo examples/maintenance --config configs/baseline.yaml --experiment baseline
```

也可运行 `scripts/run-demo.ps1 -Mode smoke`；Linux 用 `bash scripts/run-demo.sh`。

更换成真实仓库：把 `--repo` 改为仓库根目录。可指定 `--out D:/ontology-results`；程序打印 `SNAPSHOT=...` 和各组 `OUTPUT=...`。`--out` 是输出根目录，其下仍建立源码快照子目录。

仓库较大时先限制范围：

```powershell
.\.venv\Scripts\python.exe -m code_ontology run --repo D:/repo --include 'src/maintenance/*' --include 'include/*' --config configs/smoke.yaml --out D:/ontology-results
```

不要把输出根目录设为仓库自身或仓库祖先。输出可以位于仓库的一个子目录，该目录自动排除。

## 4. 每阶段独立运行

以下 `$snapshot` 替换为程序打印的实际快照路径：

```powershell
.\.venv\Scripts\python.exe -m code_ontology parse --repo examples/maintenance --views ast,cfg,cpg --out outputs/demo
$snapshot = '<打印的快照路径>'
.\.venv\Scripts\python.exe -m code_ontology prepare --snapshot $snapshot --view cfg
.\.venv\Scripts\python.exe -m code_ontology discover --snapshot $snapshot --view cfg --profile maintenance
.\.venv\Scripts\python.exe -m code_ontology extract --snapshot $snapshot --view cfg --profile maintenance --schema "$snapshot/runs/baseline/cfg/maintenance/schema/schema.json"
.\.venv\Scripts\python.exe -m code_ontology validate --snapshot $snapshot --view cfg --run-dir "$snapshot/runs/baseline/cfg/maintenance"
.\.venv\Scripts\python.exe -m code_ontology report --snapshot $snapshot --experiment baseline
.\.venv\Scripts\python.exe -m code_ontology evaluate --run-dir "$snapshot/runs/baseline/cfg/maintenance" --gold examples/maintenance/gold.json
```

`discover` 默认复用已有 schema；更改发现/合并提示词后，加 `--update-schema`。更改实例抽取提示词后，重新运行 `extract` 即可：模型缓存会根据提示词变化失效。

独立重复实验更换 `--experiment`，例如 `repeat-02`；实验 ID 进入缓存键，防止把同一回答当成新的重复实验。同一实验 ID 重跑则复用成功请求。

## 5. 产物导航

```text
outputs/<仓库名-路径哈希>/<源码快照ID>/
  manifest.json             文件清单、编码、排除原因、解析配置、阶段状态
  policy.json               首次扫描使用的策略
  source/                   固定的 UTF-8 源码快照；不要编辑
  raw/cpg.bin               Joern 基础 CPG
  raw/cpg/{nodes,edges}.jsonl 完整原始图；export.json 是成功标记
  raw/{ast,cfg}/            从同一图导出的原始视图（保留原属性）
  normalized/graph.sqlite   磁盘索引，不将原始大图全部加载到 Python 内存
  normalized/symbols.jsonl  类型/函数与声明定义语义标识
  normalized/{ast,cfg,cpg}/ 节点、边、索引和图指纹
  prepared/<视图>/packs.jsonl 真正进入模型的证据包
  prepared/<视图>/index.json 包数量、裁剪情况、未选中的系统/合成符号
  cache/llm/               SQLite 任务状态、已校验模型响应缓存
  runs/<实验ID>/<视图>/<profile>/
    run_manifest.json      当前尝试、设置、阶段状态
    evidence.jsonl         evidence_id → 文件、行号、指纹
    schema/local/          每个发现任务的局部 schema
    schema/merge/          合并轮次与未匹配的原始名称
    schema/schema.json     固定 schema
    schema/selection.json  已选和未选发现证据包
    schema/provenance.json schema/提示词/输入指纹
    extraction/           分批实例输出、失败记录、任务选择清单
    final/                正式实体、关系、属性、规则、候选与冲突
    llm_requests/          请求、原始回答、实际参数、修复和缓存命中记录
    metrics.json           数量、失败、耗时、调用和 token 用量
    failures.jsonl         当前尝试失败的任务
    report.md              单组报告
  runs/<实验ID>/comparison.md 六组对比与缺失组合
  logs/                    Joern 日志和独立工作区
```

## 6. 怎样理解输出

schema 的类、父类、关系和属性是概念层；实体是代码定义、配置记录或示例对象层。程序条件形成的是代码规则，不是现场事件。每个正式条目必须引用当前任务提供的有效证据 ID。

`evidence_locatable_rate` 只表示证据可回查，不能代替语义正确率。`evaluate` 仅做人工别名表的概念召回匹配；未匹配预测需要人工评审，不能直接算错，不能当成断言精确率。

三种视图有相同源码基础证据：AST 只加结构边；CFG 只加真实控制流边；CPG 加调用、引用、类型等上下文。它们是 **上下文增强方式对比**，不是互相完全隔离的语言表达能力对比。默认预算有限，节点与一跳上下文可能裁剪，裁剪会记录。

## 7. 测试与当前验证范围

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 真实 Joern + 本地模拟 API，六组端到端，验证接口、产物和续跑
.\.venv\Scripts\python.exe scripts/check_demo.py --real-joern
```

模拟 API 不具备语义理解能力，其输出均为人为构造的测试数据。它不会使用 `.env` 中的模型地址；生成结果放在 `fixture-integration` 下并显式标记。真实抽取质量必须使用已配置模型另行运行 `smoke`/`baseline`。

详见 `docs/ARCHITECTURE.md`、`docs/TROUBLESHOOTING.md` 和 `docs/VALIDATION.md`。

## 8. GitHub 与内网迁移

上传本项目目录到 GitHub 即可；`.gitignore` 已排除 `.env`、`.venv`、缓存和运行输出。保留 `.env.example`、源码、提示词、配置、测试、示例和文档。拉取到内网后按上述步骤创建 venv，并另行创建 `.env`。

若内网不能访问包源，可在与目标系统/Python 版本一致的机器上预下载依赖：

```powershell
.\.venv\Scripts\python.exe -m pip download -r requirements.lock.txt setuptools wheel -d wheelhouse
# 内网目标机器：
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse -r requirements.lock.txt setuptools wheel
.\.venv\Scripts\python.exe -m pip install --no-index --no-build-isolation --no-deps -e .
```

`wheelhouse` 另行迁移，不随源码包生成；Windows wheel 不能直接用于 Linux。Joern 和 Java 也需要单独准备。

