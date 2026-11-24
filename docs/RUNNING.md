# 从零到界面的完整运行步骤

> 无 Joern 时可使用内置 [Tree-sitter AST 后端](TREE_SITTER.md)：`configs/tree-sitter.yaml`。仅支持 AST，实验为两种 profile 共两组；本文 Joern 的 CFG/CPG、六组与 CPG 产物说明只适用于 Joern 后端。

所有命令在项目根目录执行。下面以 Windows PowerShell 为主。当前机器已创建 `.venv`、配置本机 DeepSeek 并有成功示例输出，可以直接从第 2 节开始。

使用 Windows 命令提示符 CMD 时，请改看 **[CMD 完整运行说明](RUNNING_CMD.md)**；两种终端的变量、引号和续行语法不同。

## 1. 首次复制到新机器

```powershell
cd 'D:\code\f47-work\code-ontology'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

当前机器不要再次执行 Copy-Item 覆盖已配置的 `.env`。新机器编辑 `.env`，至少填模型地址、密钥、实际模型名和 JOERN_HOME；配置内网地址，不需要修改 Python 文件。Joern 与兼容 Java 单独安装。

```dotenv
OPENAI_BASE_URL=http://<内网模型地址>:<端口>/v1
OPENAI_API_KEY=<实际密钥或服务允许的占位值>
OPENAI_MODEL=<内网服务的实际模型名称>
JOERN_HOME=D:/joern/joern-cli
LLM_THINKING=auto
LLM_CONTEXT_TOKENS=65536
LLM_MAX_OUTPUT_TOKENS=8192
LLM_CONCURRENCY=2
```

上下文与输出预算需符合实际服务容量；小模型可以缩小输入、发现范围和输出，不要照抄超过服务能力的预算。`thinking=auto` 适合通用兼容接口；本机 DeepSeek 已使用 disabled。

Linux 对应命令：`python3 -m venv .venv`、`.venv/bin/python -m pip ...`、`cp .env.example .env`。后续命令将 Python 路径换成 `.venv/bin/python`。

## 2. 最快查看现有示例结果

```powershell
cd 'D:\code\f47-work\code-ontology'
.\.venv\Scripts\python.exe -m code_ontology.viewer --root outputs --port 8765
```

保持终端运行，在浏览器输入：

```text
http://127.0.0.1:8765
```

选择 `deepseek-smoke / CPG / maintenance`，可直接浏览已完成的真实模型示例；名称中 `[模拟测试]` 的结果只用于验证流程。

页面有本体图谱、知识断言、实验对比、阶段追踪、人工评审五个入口。图谱可以切换本体/实例、搜索、缩放、平移、导出 SVG；点击条目查看详情，再点击 evidence ID 看带行号源码。概念详情中可选择所属属性定义。

若 8765 被占用，可使用 `--port 8766` 并相应修改地址。Ctrl+C 停止服务。界面只监听本机，使用 Python 标准库和本地资源，没有 Node/npm 构建或 CDN 依赖。它不是可直接部署到公网的多用户服务。

## 3. 自己运行一次完整示例抽取

另开一个 PowerShell 终端：

```powershell
cd 'D:\code\f47-work\code-ontology'
$py = '.\.venv\Scripts\python.exe'
& $py -m code_ontology doctor --no-llm
& $py -m code_ontology doctor --env-file .env
& $py -m code_ontology run --repo examples/maintenance --config configs/smoke.yaml --experiment my-smoke-01 --env-file .env
```

第二个 doctor 会探测模型。run 会真实调用已配置模型，按服务计费或消耗配额；先用小示例观察 token 与耗时。命令会依次扫描、解析、准备、发现、合并、抽取并生成报告，不用手动挨个执行。

终端会打印：

```text
SNAPSHOT=<源码快照目录>
RUN cpg/maintenance
STAGE discover: ... tasks
...
OUTPUT=<当前实验目录>
```

把 SNAPSHOT= 后的路径填入下面变量；OUTPUT= 就是 `$runDir`。不要包含等号前的文字：

```powershell
$snapshot = '<SNAPSHOT 后的实际路径>'
$runDir = "$snapshot/runs/my-smoke-01/cpg/maintenance"
& $py -m code_ontology validate --snapshot $snapshot --view cpg --run-dir $runDir
& $py -m code_ontology evaluate --run-dir $runDir --gold examples/maintenance/gold.json
```

然后回到浏览器点击「刷新产物」，选择 my-smoke-01。也可以直接打开 `$runDir/report.md`、`final/schema.json` 和各 JSONL。

同一命令、同一实验 ID 再跑属于缓存恢复；要测试独立生成，换成 my-smoke-02。失败时先看 failures、failed 文件和请求日志，再用原命令续跑。

## 4. 跑六组实验

```powershell
& $py -m code_ontology experiment --repo examples/maintenance --config configs/baseline.yaml --experiment my-six-01 --env-file .env
```

三种视图 × 两个 profile，真实调用次数比 smoke 多；只有准备比较时才运行。原始解析和可复用证据缓存会复用，但新实验 ID 会产生新的模型请求。

完成后查看 `$snapshot/runs/my-six-01/comparison.md`；前端「实验对比」可以显示同快照下各实验。本体质量需要人工评审，不能按条目数自动判优。

## 5. 做一次人工评估

```powershell
& $py -m code_ontology.review sample --run-dir $runDir --out reviews/my-smoke-01.csv --per-kind 20 --seed 42
```

打开 CSV，只填评分列和 notes；查源码后再判断。保存为 UTF-8 CSV，保留旁边的 manifest。

```powershell
& $py -m code_ontology.review summarize --reviews reviews/my-smoke-01.csv --out reviews/my-smoke-01-summary.json
```

也可先在页面选择条目、保存判断，在「人工评审」导出 JSON，然后 summarize 该文件。页面评审是非随机样本；正式实验建议采用分层抽样 CSV。

具体评分口径、实验设计、建议门槛与错误分析见 `EXPERIMENTS.md`。当前 `evaluate` 的别名召回不能替代人工精确率。

## 6. 切换到内网真实仓库

把 --repo 改为实际根目录。先固定小模块：

```powershell
& $py -m code_ontology run --repo 'D:/internal-repo' --include 'src/monitor/*' --include 'include/*' --config configs/smoke.yaml --experiment internal-module-01 --out outputs/internal-pilot --env-file .env
```

把配置、CMake 和必要头文件也按实际需要纳入；需要解析宏或外部头文件时编辑 YAML 的 defines/includes/compile_commands。人工参考答案放在模型输入范围外。

前端仍使用 --root outputs，可以看到新增目录；若输出位于其他路径，则重新启动 viewer 并将 --root 指向相应输出根目录或源码快照目录。

## 7. 检查程序与恢复

```powershell
& $py -m unittest discover -s tests -v
& $py scripts/check_demo.py --real-joern
```

两条均不调用真实模型：第一条使用测试数据与本地模拟接口，第二条还需要真实 Joern。已有生产输出不依赖浏览器；关闭 viewer 不影响抽取。

GitHub 上传源码和 `.env.example` 即可，`.env`、`.venv`、outputs 和 reviews 默认忽略。内网拉取后重建 venv，填写自己的 `.env`。不要直接搬运 Windows 虚拟环境到其他机器。

