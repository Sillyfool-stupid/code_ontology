# 验证记录

验证日期：2026-11-23。以下区分真实模型抽取与模拟接口测试；当前结果未经完整人工语义评审。

## 环境

- Windows，Python 3.10.20，项目内 `.venv`。
- Joern 4.0.684 / flatgraph；Java 25.0.5.1。
- `pip check` 通过；依赖版本见 `requirements.lock.txt`。
- 本机 DeepSeek 接口和 JSON 模式通过 `doctor`；实际返回模型名称为 `deepseek-flash`。
- `.env` 存放本机密钥，`.gitignore` 排除该文件；源码检查未发现密钥副本。

## 自动化测试

`python -m unittest discover -s tests -v`：**25 项通过**（其中 20 项为原基线，5 项为新增只读界面与评审工具）。

覆盖扫描排除与快照指纹、输出位于仓库子目录、GB18030 转码、中文与长行分包、三视图差异、边引用、源码篡改、类型范围、schema 引用与循环、证据伪造、类型继承与属性约束、冲突保留、非法或空 JSON 修复、JSON 模式降级、鉴权失败、限流重试、输出截断、上下文预算、提示词缓存失效、固定 schema 更新、部分失败续跑、独立 CLI 阶段、流式 JSON 数组导入、思考参数兼容和日志密钥检查。

新增覆盖：只读访问前后产物指纹不变、实例分页与搜索、证据源码和哈希检查、失败状态展示、文件路径边界、静态页面与 API、拒绝写入路由、分层抽样、人工评分汇总、原始抽样内容被编辑时拒绝汇总、交互样本标为非随机。JavaScript 通过 `node --check` 静态语法检查。

真实示例 API 检查可读取 7 组运行（真实模型 1 组、模拟接口 6 组）、14 个概念、39 条规则，并回查源码指纹。浏览器访问本机页面的权限请求被拒绝，因此本次未完成真实浏览器渲染和交互视觉验收；没有据此宣称前端已完成视觉测试。

## 真实 Joern + 模拟模型接口

命令：

```powershell
.\.venv\Scripts\python.exe scripts/check_demo.py --real-joern
```

人工示例仓库 `examples/maintenance` 经一次解析得到 770 个节点、3536 条边。

| 规范视图 | 节点 | 边 | 证据包 | 悬空边 |
|---|---:|---:|---:|---:|
| AST | 668 | 662 | 47 | 0 |
| CFG | 382 | 276 | 47 | 0 |
| 综合 CPG | 756 | 1910 | 47 | 0 |

三种视图 × general/maintenance 共六种组合全部完成，源码定位校验通过。模拟 API 共收到 356 次请求；同配置续跑新增请求为 0。模拟输出带有明确标识，只验证流程与接口，不能说明本体质量。

产物位置：`outputs/real-joern/711dc9d4bca4fba1d07f/`，汇总见该目录的 `fixture_validation.json`。另实测中文和空格输出路径可完成解析。

## 真实 DeepSeek 示例抽取

命令：

```powershell
.\.venv\Scripts\python.exe -m code_ontology run --repo examples/maintenance --config configs/smoke.yaml --experiment deepseek-smoke --env-file .env
```

采用综合 CPG + maintenance；局部 schema 发现显式采样 8/47 包，实例抽取使用纳入范围的全部 47 包。排除测试目录与人工参考答案。真实源码仓库未用于本次外网测试。

本机设置：`LLM_THINKING=disabled`、上下文预算 65536、输出预算 8192、并发 2。最初以 16384/4096 运行时，中文候选 schema 在保守 UTF-8 预算下无法两两合并，程序明确报错并保留局部产物；调整预算后重新完成。预算是客户端限制，迁移时应遵守内网服务的实际容量。

| 产物/指标 | 首次成功运行 |
|---|---:|
| 概念 / 关系类型 / 属性类型 | 14 / 11 / 8 |
| 实体 | 78 |
| 实体关系 | 39 |
| 属性断言 | 16 |
| 代码规则 | 39 |
| 候选 | 84 |
| 属性冲突组 | 4 |
| 失败任务 | 0 |
| 证据可定位率 | 100% |
| 模型请求 / 局部修复 | 53 / 1 |
| 输入 / 输出 token | 218241 / 27148 |
| 模型流水线耗时 | 85.14 秒 |

这些调用数和 token 数来自成功尝试的指标，不含早先失败尝试与连接探测。完整请求记录保留在本机输出目录。

对同一实验再次执行相同命令：47 次缓存命中，**新增模型请求为 0**。`metrics.json` 记录最近一次执行；首次成功运行的指标另保存为 `first-success.metrics.json`。

输出目录：

```text
outputs/maintenance-09fb19ac/711dc9d4bca4fba1d07f/
  runs/deepseek-smoke/cpg/maintenance/
    report.md
    first-success.metrics.json
    metrics.json
    validation.json
    evaluation.json
    final/schema.json
    final/{entities,relations,attributes,rules,candidates,conflicts}.jsonl
```

`validate` 对 47 个证据包及正式结果检查通过；这保证引用与源码位置有效，不保证业务解释正确。

参考概念别名匹配为 6/8，召回值 0.75。“泵”“监测读数”未命中预定义别名；结果含“监测值”等近义名称，因此该数值同时受采样和别名表限制。没有据此计算实例精确率。

初步检查发现：schema 含软件模块概念，部分属性值是代码表达式名称，同一状态属性存在多值冲突，规则仍可能重复或描述不足。84 条候选、4 组冲突及所有机器生成断言都需复核，当前结果不作为已验证的生产知识库。正式现场事件不能从示例或代码定义推断。

## 实际能力边界

- 六组真实 Joern + 模拟接口已经验证；真实 DeepSeek 目前只验证综合 CPG + maintenance 一组。其余五组真实模型效果由 `experiment` 命令生成后再比较。
- 原始大图通过 JSONL/SQLite 处理，但选定运行的证据包列表会进入内存；未做超大工业仓库容量评测。
- schema 合并有预算与轮次上限；无法缩减、输出截断会明确失败，需要缩小范围或调整合法预算。
- 一跳上下文与大 schema 子集使用确定性规则选择，可能漏掉语义关联；省略清单落盘。
- CMake 仅静态抽取明确目标和依赖，不执行构建脚本；编译宏和头文件不完整会影响解析覆盖。
- 无数据流推理、完整 CMake 解释、RDF/OWL 或图数据库；断言证据的语义真实性需要人工评审。
- `.venv`、`.env`、输出与缓存均留在本机；GitHub 克隆后重建环境并填写内网 `.env`。

