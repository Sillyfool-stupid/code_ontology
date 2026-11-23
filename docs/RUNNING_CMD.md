# Windows CMD 完整运行说明

适用于命令提示符 CMD。所有命令按行执行，不要复制 PowerShell 的 `$py`、`&` 或反引号。先激活虚拟环境，后续统一使用 `python`。每次新开 CMD 都需要重新激活。项目在入口处设置 UTF-8 终端输出，文件读写也显式指定编码，无需每次加编码参数。

## 1. 进入项目并检查环境

```bat
cd /d "D:\code\f47-work\code-ontology"
call .venv\Scripts\activate.bat
python -m code_ontology doctor --no-llm
python -m code_ontology doctor --env-file .env
```

激活后命令行通常出现 `(.venv)` 前缀；可用 `where python` 检查首个路径是否属于项目的 `.venv`，用 `deactivate` 退出。当前机器已配置 `.venv` 和 `.env`，不要覆盖已有 `.env`。第二条 doctor 会真实调用模型探测接口。

仅在首次复制到新机器时安装：

```bat
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.lock.txt
python -m pip install -e .
if not exist .env copy .env.example .env
notepad .env
```

在新机器填写内网模型地址、密钥、模型名、合法的上下文与输出预算、JOERN_HOME；Joern 与兼容 Java 单独安装。配置含义见 RUNNING.md 与 README.md。

## 2. 运行一个完整示例

```bat
python -m code_ontology run --repo examples/maintenance --config configs/smoke.yaml --experiment my-smoke-01 --env-file .env
```

一次完成扫描、解析、准备证据、schema 发现与合并、实例抽取、归并与报告。这会调用配置的真实模型。相同参数和实验 ID 重跑复用缓存；独立试验换成 my-smoke-02 等新 ID。

## 3. 校验与概念参考匹配

从终端 `SNAPSHOT=...` 复制实际目录，替换下面 set 命令中的示例路径。变量值不包含 `SNAPSHOT=` 前缀，也不要保留尖括号占位符。

```bat
set "SNAPSHOT=D:\code\f47-work\code-ontology\outputs\maintenance-09fb19ac\711dc9d4bca4fba1d07f"
set "RUN_DIR=%SNAPSHOT%\runs\my-smoke-01\cpg\maintenance"
python -m code_ontology validate --snapshot "%SNAPSHOT%" --view cpg --run-dir "%RUN_DIR%"
python -m code_ontology evaluate --run-dir "%RUN_DIR%" --gold examples/maintenance/gold.json
```

示例快照路径是本机现有结果的位置；实际打印路径优先。validate 检查结构和源码位置；evaluate 仅做概念别名匹配，不代表语义精确率。

## 4. 打开可视化页面

另开一个 CMD：

```bat
cd /d "D:\code\f47-work\code-ontology"
call .venv\Scripts\activate.bat
python -m code_ontology.viewer --root outputs --port 8765
```

保持终端运行，在浏览器地址栏输入：

```text
http://127.0.0.1:8765
```

若已有服务，可以直接访问；若端口占用而需要另启一个服务，把端口和浏览器地址同时改为 8766。Ctrl+C 停止当前服务。

选择自己的 my-smoke-01，或者已有真实模型实验 deepseek-smoke。界面支持本体/实例图谱、断言搜索与分页、源码证据、实验对比、阶段追踪与人工评审。标记为模拟测试的结果仅验证流程。

## 5. 六组实验和人工评审

```bat
python -m code_ontology experiment --repo examples/maintenance --config configs/baseline.yaml --experiment my-six-01 --env-file .env
```

六组实验会真实调用模型。完成后在页面刷新产物，并查看快照内的 runs/my-six-01/comparison.md。

使用第 3 节设置的 RUN_DIR，生成一份人工抽样表：

```bat
python -m code_ontology.review sample --run-dir "%RUN_DIR%" --out reviews/my-smoke-01.csv --per-kind 20 --seed 42
```

填写评分列与 notes 后，以 UTF-8 CSV 保存，保留旁边的 manifest；然后汇总：

```bat
python -m code_ontology.review summarize --reviews reviews/my-smoke-01.csv --out reviews/my-smoke-01-summary.json
```

抽样文件已有时，使用新的文件名，避免覆盖已填写的评审。评分口径与实验设计见 EXPERIMENTS.md。

## 6. 使用真实仓库

```bat
python -m code_ontology run --repo "D:\internal-repo" --include "src/monitor/*" --include "include/*" --config configs/smoke.yaml --experiment internal-module-01 --out outputs/internal-pilot --env-file .env
```

调整为实际模块，纳入必要头文件、配置和 CMake；YAML 中配置宏、头文件目录与编译数据库。先做小模块，再扩展。

## 7. 测试程序

```bat
python -m unittest discover -s tests -v
python scripts/check_demo.py --real-joern
```

两者不使用真实模型接口，第二条需要真实 Joern。各阶段输入输出见 PIPELINE_IO.md。



