# 内网运行与排障

## 首次运行顺序

1. 重新创建 venv，安装锁定依赖和本项目。
2. 填 `.env`，先 `doctor --no-llm` 检查 Joern。
3. `doctor` 检查真实模型接口。
4. 在 `examples/maintenance` 跑 `configs/smoke.yaml`。
5. 检查 schema、证据和候选，再跑六组 baseline。
6. 真实仓库先限定一个模块，保留 include/defines/编译数据库信息。

## 常见情况

| 现象 | 处理 |
|---|---|
| `Set OPENAI_BASE_URL and OPENAI_MODEL` | 填 `.env`；检查系统环境变量是否覆盖文件；指定 `--env-file` |
| 401/403 | 检查密钥、服务鉴权方式，不要反复重试同一配置 |
| 404 或 model not found | BASE_URL 通常以 `/v1` 结束，不能填完整 `/chat/completions`；模型名取服务实际值 |
| DeepSeek 拒绝 JSON 模式 | `LLM_JSON_MODE=auto` 仅在明确不支持时降级；也可设 `off`，输出仍做 JSON 校验 |
| 温度等参数被拒绝 | 默认发送 temperature/max_tokens；`LLM_THINKING=enabled` 时省略 temperature。通用接口设 thinking 为 auto；新增适配集中修改 `llm.py` |
| 空内容/非法 JSON | 自动修复一次；仍失败时查看 `llm_requests` 与任务 `.failed.json` |
| 输出 length/截断 | 减小 `pack_tokens`，提高合理的输出/上下文预算；更新 schema 提示词限制粒度；再重跑失败任务 |
| 请求预算超限 | UTF-8 上界比较保守；降低单包预算/发现批量，或按服务真实窗口增加 `LLM_CONTEXT_TOKENS` |
| schema 合并无法缩减 | 增大上下文，减小发现范围；不能把大 schema 静默截成不完整结构 |
| 找不到 Joern | 填 `JOERN_HOME` 为包含 `joern(.bat)` 和 `c2cpg(.bat)` 的目录 |
| Scala 脚本编译失败 | 查看 `logs/export.log`；基线使用 Joern 4 flatgraph API，版本升级后先跑真实样例 |
| 旧 Joern 参数 `--params` | 当前基线使用 `--param key=value`；doctor 不通过时应对适配层做明确修改 |
| Windows 路径参数失败 | 已处理空格、中文、等号；批处理参数中的 `& | < > ^ % ! "` 被明确拒绝，请换无这些字符的工具/输出路径 |
| CFG 为空 | 检查是否只解析了声明、函数体被宏排除、解析失败或输入范围不含实现；不要人工捏造 CFG |
| 源码快照被编辑 | 快照不可变，重新 scan/parse 产生新快照 |
| 缺少头文件/宏 | 配置 includes、defines 或 compile_commands；只读取现有编译数据库，不自动运行项目脚本 |
| 修改提示词但 schema 没变 | schema 固定是预期行为，使用 `--update-schema` 或新实验 ID |
| 程序中断 | 使用原命令、原输出与实验 ID 重跑，成功模型调用会命中缓存 |
| 结果只有部分成功 | 检查 run_manifest、failures、局部 failed 文件及 comparison 中缺失组合 |

## 迁移注意

复制项目源码、prompts、configs、examples、tests、scripts、文档、pyproject 和 requirements.lock；不要把 `.venv` 当作可迁移环境。真实源码和模型请求都留在内网。若复制已有输出作为续跑快照，应保持原路径；跨机器换路径建议重新解析，以免编译数据库和 CPG 中绝对路径失效。

`source/` 是 UTF-8 证据副本；原始文件编码和原始 SHA256 保留在 manifest。UTF-8 转换不会修改输入仓库。宏配置会影响代码覆盖，单个快照不代表所有构建变体。

不自动执行仓库中的 CMake、Shell、BAT、PowerShell 脚本；它们只作为静态证据。不要同时运行两个进程写同一快照/同一实验目录。

原始请求含代码证据，查看日志时按内网源码资料处理；程序不持久化 API key。

