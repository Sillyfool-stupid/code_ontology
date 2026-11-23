# 代码业务本体抽取项目：Agent 入口

本子目录是独立的 Python / Joern / LLM 工具，不是上级目录的课程 Notebook/Word 报告作业。用户的具体任务与约束优先。

开始工作先阅读 [内网 Agent 使用与接手手册](docs/INTRANET_AGENT_GUIDE.md)，按当前任务选择对应章节。人工操作的简版命令在 [CMD 运行说明](docs/RUNNING_CMD.md)。

执行六组基线与组织人工审核时，按 [六组基线与人工审核规程](docs/BASELINE_REVIEW.md) 固定输入、运行矩阵、采样与评分口径；交付使用 [评审报告模板](docs/templates/BASELINE_REVIEW_REPORT.md)。人工标签不能由 Agent 自评代填。

## 接手原则

- 从实际工作目录识别项目根路径，不把外网机器的 `D:\code\f47-work\code-ontology` 当成内网路径。
- Windows 用户偏好：先激活 `.venv`，之后使用 `python`；无需 `-X utf8`。识别 CMD、PowerShell、Linux shell，不能混用变量和激活语法。
- Agent 的不同工具调用可能不共享 shell 环境；每个需要运行 Python 的新 shell 先激活环境。不要仅凭 `(.venv)` 字样判断解释器，检查 `sys.executable`。
- 模型连接读取 `.env`，策略读取 YAML，提示词读取 `code_ontology/resources/prompts/`。系统环境变量会覆盖 `.env`。
- 密钥不写入回答、文档、测试样例或提交；不要把整个 `.env` 打印到日志。使用用户指定的内网模型，不自动切换外网端点。
- 输入仓库与已生成的 `source/` 快照保持原样；调整范围、配置或程序后由正常入口生成产物。不要手改正式结果来伪造通过。
- 现有抽取流程与只读界面分离。用户仅要求展示、说明或排障时，不顺带改 schema/实例抽取逻辑。
- 同一快照的解析和同一实验目录不要由多个进程并发写入。原命令原实验 ID 用于续跑；独立实验使用新 ID。
- 不因效果不佳直接删除缓存、强制重解析或运行六组真实模型实验；先定位对应阶段，并在用户已授权范围内执行。
- 自动化校验、模拟 API 测试和人工语义评审应分开报告；不能把结构合法或模拟测试通过解释成抽取质量优秀。
- 不将普通 `run` 误当成已经执行了独立 `validate`、`evaluate` 或人工评审。完成后检查各自产物。
- 正式评审标签由人工填写。Agent 可以提出核查建议，不能把自行推断的标签填成“已人工验证”。

## 代码导航

| 模块 | 职责 |
|---|---|
| `repository.py` | 文件扫描、排除、源码快照、编码、解析配置指纹 |
| `joern.py`、`resources/export.sc` | Joern 调用、流式导图、SQLite 规范化与视图 |
| `prepare.py` | 类型/函数证据包、图上下文、预算与裁剪 |
| `config.py` | `.env` / YAML 读取与参数校验 |
| `llm.py` | 模型请求、JSON 修复、重试、日志、缓存 |
| `models.py` | schema、实例和规则的数据契约 |
| `pipeline.py` | 发现、合并、固定 schema、抽取、归并与报告 |
| `cli.py` | 主命令入口 |
| `viewer.py`、`resources/viewer/` | 只读本地界面与证据回查 |
| `review.py` | 人工分层抽样与评分汇总 |
| `console.py` | 显式 UTF-8 控制台输出 |

修改行为后运行相关测试；完整检查用 `python -m unittest discover -s tests -v`。Joern 适配变更还应运行 `python scripts/check_demo.py --real-joern`；后者使用模拟模型接口，不代表真实模型语义质量。

向用户交付时说明：处理范围、实际运行命令、快照与实验路径、成功/失败、验证方式、尚未人工确认的语义问题，以及下一步可执行命令。
