# 无 Joern 环境：Tree-sitter AST 抽取

更新日期：2026-11-24。输入仍是 C/C++ 源码仓库，后续证据包、Schema 发现、实例抽取、校验、报告、只读界面和工作台沿用同一套流程。

## 1. 选择解析器

| parser_backend | 行为 | 可用视图 |
|---|---|---|
| auto（默认） | 能找到 c2cpg 和 joern 时用 Joern；任一缺失时使用 Tree-sitter | Joern 三视图，Tree-sitter 仅 ast |
| tree-sitter | 强制使用随 Python 安装的 C/C++ 语法包，无需 Java、Joern 或编译器 | ast |
| joern | 强制使用 Joern，缺失或执行失败会报错 | ast / cfg / cpg |

YAML 设置 `parser_backend: tree-sitter`，或在 CLI 增加 `--parser-backend tree-sitter`；CLI 优先。推荐直接使用 `configs/tree-sitter.yaml`。自动选择仅处理工具缺失，不会在 Joern 执行报错后悄悄切换。

Tree-sitter 提供语法树，调用表达式只是语法节点，不代表已经解析了被调用函数。它不产生 CFG、综合 CPG、引用解析、跨文件类型解析或数据流。宏、头文件路径和 compilation database 会记录但不求值，预处理分支可能同时保留。需要这些语义能力或严格六组对比时，使用 Joern。

`parse/run/experiment` 在 Tree-sitter 模式下将请求视图统一为 `ast` 并打印提示，默认 smoke 配置中的 cpg 也会替换为 ast。`experiment` 实际运行 AST × general/maintenance 两组，报告不会把不支持的 CFG/CPG 四组标成失败。独立 `prepare/discover/extract/validate` 应显式写 `--view ast`，错误视图会报错。

## 2. 安装与诊断（Windows CMD）

在实际项目目录运行；已有 `.venv` 时跳过创建，不要覆盖已有 `.env`。

```bat
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.lock.txt
python -m pip install -e .
python -m pip check
python -m code_ontology doctor --parser-backend tree-sitter --no-llm
```

锁定依赖包含 `tree-sitter==0.26.1`、`tree-sitter-c==0.24.2`、`tree-sitter-cpp==0.23.4`。内网安装使用组织允许的包源或预先准备的 wheel；无需在目标机器编译 grammar。doctor 会实际加载两种语法并解析短代码，不要求 Joern 检查通过。`--no-llm` 不调用模型。

Tree-sitter 缺失或版本不兼容时按错误提示安装依赖，不会返回空图假装成功。Python、平台必须有对应依赖 wheel；跨机器重新创建虚拟环境。

## 3. 先解析检查，再调用模型

```bat
call .venv\Scripts\activate.bat
python -m code_ontology parse --repo examples/maintenance --config configs/tree-sitter.yaml --out outputs/tree-sitter-demo
```

从终端复制真实的 `SNAPSHOT=...` 路径，填入下一条变量。不要直接照抄其他机器的哈希。

```bat
set "SNAPSHOT=D:\实际输出目录\实际快照ID"
python -m code_ontology prepare --snapshot "%SNAPSHOT%" --view ast --config configs/tree-sitter.yaml
python -m code_ontology validate --snapshot "%SNAPSHOT%" --view ast --config configs/tree-sitter.yaml
```

先检查 `manifest.json` 文件范围、`raw/tree-sitter/parse_report.json` 的逐文件语言与语法错误，以及 `prepared/ast/packs.jsonl` 的源码片段。`.c` 用 C 语法，其余 C++ 扩展用 C++；`.h` 若 C++ 有语法错误，会同时尝试 C 并选择错误较少的结果。错误和缺失节点保留在原始树及报告中，预处理包带解析限制与错误告警，错误代码仍可能有部分可用结构，需要核查覆盖。

配置好 `.env` 的内网模型端点与模型名称后运行：

```bat
python -m code_ontology run --repo examples/maintenance --config configs/tree-sitter.yaml --profile maintenance --experiment ts-smoke-01 --out outputs/tree-sitter-demo --env-file .env
```

真实仓库替换 `--repo`，用自己的 YAML 固定 `include/exclude/max_files` 等范围。先使用小模块；普通 run 不自动执行独立 validate/evaluate。完整两组实验：

```bat
python -m code_ontology experiment --repo examples/maintenance --config configs/tree-sitter.yaml --experiment ts-two-01 --out outputs/tree-sitter-demo --env-file .env
```

独立模型阶段也可运行：

```bat
python -m code_ontology discover --snapshot "%SNAPSHOT%" --view ast --profile maintenance --experiment ts-stage-01 --config configs/tree-sitter.yaml --env-file .env
python -m code_ontology extract --snapshot "%SNAPSHOT%" --view ast --profile maintenance --experiment ts-stage-01 --schema "%SNAPSHOT%\runs\ts-stage-01\ast\maintenance\schema\schema.json" --config configs/tree-sitter.yaml --env-file .env
python -m code_ontology validate --snapshot "%SNAPSHOT%" --view ast --run-dir "%SNAPSHOT%\runs\ts-stage-01\ast\maintenance" --config configs/tree-sitter.yaml
```

## 4. 产物与缓存

```text
<SNAPSHOT>/
  manifest.json                    parser 后端、版本、能力限制、源码哈希与阶段状态
  source/                          固定 UTF-8 源码，禁止手改
  raw/tree-sitter/nodes.jsonl       命名语法节点、范围、语法错误属性
  raw/tree-sitter/edges.jsonl       真实 AST 父子边
  raw/tree-sitter/parse_report.json 逐文件语言、错误数量与位置样本
  raw/tree-sitter/export.json       解析缓存标记与图统计
  raw/ast/                         AST 投影视图
  normalized/graph.sqlite          与原流程兼容的磁盘图
  normalized/ast/                  AST 节点、边与带 parser 信息的 index
  normalized/symbols.jsonl         类型、函数声明和定义，不把函数指针当方法
  prepared/ast/                    带源码证据、结构上下文和限制说明的输入包
  runs/<实验ID>/ast/<profile>/      Schema、实例、指标、请求记录和报告
  runs/<实验ID>/comparison.json    实际两组结果与解析器信息
```

不会产生伪造的 `raw/cpg.bin` 或 CFG/CPG。符号之外的全局声明、宏和错误区域也以文件片段保留为证据。图节点 CODE 仅保存预览，完整文本始终来自 source 快照及证据包。

后端、grammar 版本和 Tree-sitter 适配器源码参与快照标识；Joern 与 Tree-sitter 不会共用解析快照。重复同命令、配置与实验 ID 复用解析与模型缓存。`--force-parse` 重新解析固定源码；更改源文件、后端或适配器后通过正常入口生成新快照。升级依赖后先重新 doctor。

## 5. 工作台

```bat
python -m code_ontology.workbench --port 8766
```

浏览器打开 `http://127.0.0.1:8766`，YAML 选择 `configs/tree-sitter.yaml` 后保存。也可用默认 auto 策略，在缺少 Joern 时自动选择。保存/扫描会将视图设为 AST，页面显示后端限制并禁用不支持的视图；实验按钮为“运行 AST 两组基线”。先扫描核对范围，再解析、生成模型输入包和运行模型阶段。选择已有快照时，以该快照记录的后端为准；换解析器要重新扫描。

## 6. 可复现检查

```bat
python -m unittest discover -s tests -v
python scripts/check_workbench.py --tree-sitter --out outputs/tree-sitter-verification
```

第二条实际使用 C/C++ grammar，完整经过 doctor、扫描、解析、准备、发现、抽取、独立校验、参考匹配、报告、审核表、缓存续跑和 AST 两组实验。模型由本地测试 API 替代，不访问真实模型，不评价业务抽取语义质量。查看输出根目录 `verification.json`，缓存续跑应为 0 次额外 API 请求。真实 Joern 回归仍用 `python scripts/check_demo.py --real-joern`。
