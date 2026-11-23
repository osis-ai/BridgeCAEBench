# 五个智能体设计的实现

论文中五个设计（T1–T5）在本仓库的实现细节。

## 1. harness 披露表

| 方法 | 实现与版本 | 循环 | 工具 | 上下文管理 | 步数 / 轮次上限 | 重试 | 系统提示词 | skill 加载 | MCP 接入 | temperature / max_tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| T1 mini-SWE-agent | `mini-swe-agent==2.4.6`（pip），`mini.yaml` 配置，`LitellmModel`（工具调用版） | 单智能体，每步一个 bash 工具调用 | 只有 bash（Git Bash） | 无压缩；单次调用将超 `model.context_tokens`（256k）− 32k 时丢掉最早的轮次，只发系统提示、任务和最近约半个上限的轮次（平台加的，`adapter.py` 的 `Model`；轨迹仍完整）；单条输出超 10000 字时只给首尾各 5000 字（mini.yaml 原样） | 步数、费用不设上限；墙钟 = 任务线时限 − 60 s | mini-swe-agent 内置重试 | mini.yaml 的 system_template；instance_template 换成任务文本（`methods/t1_miniswe/adapter.py` 的 `task_text`） | skill 快照是普通文件，bash 读 | 命令行 `osis-mcp`（`methods/mcp_cli.py`）→ 平台代理 | 网关统一设置（`configs/platform.yaml` 的 `model`） |
| T2 OpenCode（裸配置） | 上游原版 OpenCode 1.19.0（npm `opencode-ai`，路径见 `configs/platform.yaml` 的 `osisai.vanilla_opencode`） | OpenCode 默认 build agent | OpenCode 内置工具（bash、read、edit、glob、grep、write、task、todowrite、webfetch、question、skill、MCP 资源三件）+ MCP 工具；与 T5 相同 | OpenCode 自带压缩，`limit.context` 填 `model.context_tokens`（256k） | 不设 | OpenCode 默认 | OpenCode 默认，无 AGENTS.md | OpenCode 原生 `skill` 工具，快照放在配置目录 `skills/` | opencode.json `mcp`（remote）→ 平台代理 | 同上 |
| T3 领域流水线 | 普通 Python（`methods/t3_pipeline/adapter.py`），httpx 直调网关 | 固定流程：解析 → 检索 → 计划 → 编写 → 执行 → 复核 → 编写 … → 报告 | 无自由工具；编排器调 MCP：`weknora.list_knowledge_bases / hybrid_search`、`osis.api_grep / execute_python` | 每个角色单轮调用；文件、参考资料截断（单文件 12000 字，参考 30000 字） | 修复环 ≤ 15 轮；报告阶段只读查询 ≤ 3 次 | 每次模型请求最多 3 次 | 各角色一句固定提示（文件内 `ROLES`） | 解析后由架构师从快照目录选 ≤ 8 个文件读入 | 编排器经 `methods/mcp_cli.Session` → 平台代理 | 同上 |
| T4 验证门控 | T1 原样 + 门控（`methods/t4_gated/adapter.py`） | T1 的循环；提交后编排器查门，不过就把违规清单作为新用户消息追加到同一会话 | 同 T1；门控探查经 osis MCP 代理跑只读 pyosis 脚本 | 同 T1 | 重开 ≤ 5 轮，仍不过记 `deferred` | 同 T1 | 同 T1 | 同 T1 | 同 T1 | 同上 |
| T5 OSIS-AI | OSIS 安装包（`configs/platform.yaml` 的 `osisai.install_dir`）里的 opencode.exe（OSIS 分支） | OSIS-AI 会话 | 同 T2，另加厂商插件提供的能力（见 §2） | 同 T2，另有长期记忆插件 | 不设 | OpenCode 默认 | 安装包 AGENTS.md | 同 T2 | 同 T2 | 同上 |

所有方法：同一份 skill 快照（默认 `templates: without`）、同一组 MCP（osis、weknora，经同一代理，隐藏界面工具，`instance_id` 必须是 solver）、
同一个网关和模型、同一时限（knowledge 900 s，其余 7200 s，大桥共享 2 个求解名额）。会提问的 T2、T5 一律得到“按默认参数，你自己判断”。
T1、T4 共用最终回复格式：首行 `status: done | declined`。

## 2. T5 相对 T2 的配置差异

T2 用上游原版 1.19.0，T5 用安装包里的 OSIS 分支（据用户说也是基于 1.19 改的；分支自报 `--version` 1.2.0）。
运行环境相同（Git Bash、PATH 里的 OSIS-AI `.venv`、配置 / 数据 / 状态目录放在运行目录、关闭自动更新与 Claude Code 兼容），
只有包缓存不同：T5 用 `~/.osisai/cache`，T2 用原版默认缓存。T5 多出的：

| 项 | T5（OSIS-AI） | T2（裸配置） |
|---|---|---|
| 配置加载方式 | 安装包 `.agents` 作为 `OSIS_EXTRA_CONFIG_DIR`（OSIS 分支特有的额外配置层） | `OPENCODE_CONFIG_DIR`（上游标准方式），只放 opencode.json 和 skills |
| `AGENTS.md` | 有：身份设定（“中交公规院自研 OSIS-AI”），“任何 OSIS 任务先加载 osis-engine”等路由规则 | 无 |
| `commands/` | `feedback.md`（/feedback） | 无 |
| `prompts/` | `osis-chat-summarize.txt`、`osis-recorder-extract.txt` | 无 |
| `agent`（子智能体） | `osis-summarizer`、`osis-recorder`（隐藏子智能体，供记忆插件用） | 无 |
| 插件（生效的） | `y-osis-memory.js`（长期记忆，`~/.osisai/memory`）、`x-attachment.js`、`x-docx.ts` | 无 |
| 插件（安装包自己停用的） | `f-feedback.off`、`u-osis-load-engine.off` | — |
| 插件（平台去掉的） | `w-osis-project-dir.ts`（注入界面里打开的工程，不是本次运行的工程） | — |
| `permission` | 安装包是 `*: allow` 加一组 weknora 规则；平台去掉 weknora 规则后与 T2 相同，都是 `*: allow`，另外两边都禁止 bash 执行 taskkill/Stop-Process/kill（智能体两次按 PID 杀掉过评测平台进程） | 同左 |
| 运行方式 | 串行；每次运行前清空记忆（`reset_dirs.json` 留证据），一个会话内的多轮（project 线）仍共用记忆 | 可并行 |
| 相同项 | `share: disabled`、模型与 provider、MCP、skill 快照、时限、提问作答 | 同左 |

注意：T5 − T2 除了上表的配置层，还含 OSIS 分支在二进制里的改动（`start.bat`：“OSIS_EXTRA_CONFIG_DIR requires the OSIS fork build”）。
两者给模型的工具清单相同（冒烟时逐个对过，29 个）。

## 3. 锁定的版本

- mini-swe-agent 2.4.6（`pyproject.toml` 的 `miniswe` extra，`uv.lock`）。
- OpenCode：T2 上游原版 1.19.0（npm `opencode-ai`）；T5 OSIS 分支（`--version` 1.2.0）。
- OSIS-AI 安装包：`osis_BITFEA-5.1`（路径见 `configs/platform.yaml` 的 `osisai.install_dir`）。
- 模型：论文用 `deepseek-v4.1-flash` 和 `gpt-6.1-sol`（`configs/platform.yaml` 的 `models`）。

## 4. 偏离规格的地方

1. **T1 的 instance_template**：mini.yaml 原文是 SWE-bench 的“修 issue”流程，换成任务文本加一段接入说明（bash、skill 快照路径、`osis-mcp` 用法、最终回复格式）。system_template、观测模板、格式错误模板沿用 mini.yaml。
2. **T1 在 Windows 上用 Git Bash**：mini-SWE-agent 本地环境用 `shell=True`，在 Windows 上是 cmd.exe；改成 `bash -c`（替换 `minisweagent.environments.local._run`）。单条命令超时设为任务线时限。
3. **T1 的平台提示词**里“skill 只通过提供的工具读取，不要在磁盘上搜索文件”保留，接入说明写明“提供的工具”就是在快照目录上用 bash。
4. **T2 与 T5 二进制不同**（原版 1.19.0 / OSIS 分支），T5 − T2 含二进制差异，见 §2。
5. **T3 编写员按文件输出**：每个要改的文件一次调用，输出完整的代码块；规格要的是一次输出 `{path: 内容}` 的 JSON，大文件塞进 JSON 字符串转义容易出错。合起来仍是 `{path: 内容}`。
6. **T3 执行员多跑一步求解**：模板的 `prep/main.py` 只重建不求解，执行员在它之后跑一个求解脚本（与造题用的 `bench/osis.py` 的 `_SOLVE` 相同）。需要验算时再跑 `post/main.py`：diagnose 线简报里提到验算，或简报写了“运行 py/post/main.py”（analyze 的验算类题）。
7. **T3 复核没给出可写的改动就停**，不再空跑剩下的轮次。检索里多了 pyosis API 检索（`osis.api_grep`），解析员判定拒做时跳过检索和建模，直接由报告员写 pushback。
8. **T4 的 G3 只查任务说明里点名的验算项**（「…验算」）。规格还要求“没点名就要求验算表无 NG”，这条没做：很多模板原样就有 NG 项（例如冒烟用的空心板 10m 边梁模板有 5 项 NG），change 题不点名验算项时会被逼着去改无关的设计。
9. **T4 的门控探查经 osis MCP 代理执行**（脚本 `py/_t4_gate.py`，与评分端 `bench.osis.inspect` 读同样的内容），因此会记进 `mcp_calls.jsonl`，行为指标 `osis_runs` 里含这些探查；按脚本名可以剔除。
10. **退役方法**留在 `methods/legacy_*`，编号改为 L1–L5，`legacy: true`，默认不跑，`--methods L2` 点名时才跑。框架 venv 改成按 extra 命名（`.venvs/miniswe`、`.venvs/langgraph` 等），原来的 `.venvs/t2`–`t5` 要重新 `uv run osis-bench env` 或改名。
