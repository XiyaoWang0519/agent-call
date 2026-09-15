# Agent Call：代码审查与渐进式重构计划

> **状态：工作文档，不改变任何运行时行为。**
> 本次会话只做只读审查、基线实测和文档交付：未修改 `app/`、`tests/`、`scripts/`、`.github/` 中的任何代码，未创建 PR，未部署，未使用生产密钥，未拨打真实电话。
> 结构：**Part I** 是本次新增的验证与基线证据；**Part II** 完整保留原始审查与重构计划（R00–R23）正文，未改一字。

---

# Part I · 本次验证补充

## I.1 验证状态更新（取代 Part II 头部的"验证限制"）

Part II 的原始记录声明"运行环境未能克隆仓库；未运行 pytest / Ruff / mypy / 构建 / live-phone / 性能测试"。

本次会话的工作区**就是该仓库的本地克隆**，`HEAD` 精确等于固定基线 `7de5e69be9083a6fb484845645f1b05b4bc67254`，工作树干净（`git status --porcelain` 无输出）。因此该限制中属于静态检查与离线测试的部分已经真实解除：

| 原记录中的限制 | 本次状态 |
|---|---|
| 未运行 Ruff | ✅ 已运行，pass |
| 未运行 mypy | ✅ 已运行，pass |
| 未运行 pytest | ✅ 已运行，662 passed / 2 skipped，覆盖率 88.43% |
| 未运行构建 | ❌ 仍未运行（R22 范围：wheel + 源码目录外干净安装） |
| 未运行 live-phone | ❌ 仍未运行（需要显式实例确认与预算授权） |
| 未运行性能测试 | ❌ 仍未运行（无实测，因此仍然不承诺任何延迟改善百分比） |
| 未能克隆仓库 | ✅ 已解除；但不能据此主张"所有 PR 都已审查" |

仍属未验证、**不得当作已通过**的部分：

- 任何真实电话 / SIP canary / live-phone run / 端到端延迟实测；
- 生产部署工作流、Fly 平台行为、`production` environment 是否配置了人工审批（GitHub 环境保护规则不在版本控制内）；
- Python 3.12 运行（本机 3.13.12，CI 与部署也都是 3.13）；
- 数据迁移与"旧版本读新数据"（R07 范围，需要上一已发布 artifact）；
- 原记录中全部标注为"静态风险，需要复现"的竞态：本次只确认了它们的**结构性前提**（见 I.3），**没有做故障注入复现**。

## I.2 R01 第一步：基线实测（已执行）

运行环境：macOS 本机，`env -i` 清除全部继承环境变量，使用仓库内 `.venv`。
`Settings` 的 dotenv 文件（`.env` / `.env.local`）只在**无参数构造** `Settings()` 时被读取；测试 fixture 显式传参，`Settings.from_environ({...})` 显式传映射并忽略 dotenv（由 `tests/test_doctor.py::test_from_environ_ignores_dotenv_files` 固定）。因此本次运行**没有读取任何真实凭据文件**。

| 项目 | 值 |
|---|---|
| commit | `7de5e69be9083a6fb484845645f1b05b4bc67254` |
| 工作树 | clean |
| Python | 3.13.12 |
| uv | 0.9.27 |
| pytest | 9.1.1 |
| mypy | 2.3.1 |
| ruff | 0.16.4 |
| `uv.lock` sha256 | `e59d952bff44081661d41dfe11af95c118d59f74f8c666900823fc3898ed6e99` |
| 测试模块数 | 36 |

命令与结果（**全部 exit 0**）：

```bash
uv run ruff format --check app tests scripts   # 109 files already formatted
uv run ruff check app tests scripts            # All checks passed!
uv run mypy app                                # Success: no issues found in 55 source files
uv run pytest -q --cov=app                     # 662 passed, 2 skipped in 64.09s
                                               # TOTAL 88.43%，Required coverage of 85.0% reached
```

**关键竞态模块重复运行 3 次**（`test_activation`、`test_voice_closing`、`test_tool_transfer_safety`、`test_teardown_recovery`、`test_lifespan_cleanup`、`test_fork_safety`、`test_ask_agent`、`test_live_dispatch`、`test_send_dtmf`、`test_hold`、`test_carrier_monitor`、`test_call_audio`）：

| 轮次 | 结果 | 耗时 |
|---|---|---|
| 1 | 220 passed | 46.14s |
| 2 | 220 passed | 46.43s |
| 3 | 220 passed | 46.19s |

本机 3 轮**未观察到 flaky**。这不构成"这些测试没有 flake"的证明：原记录指出的 sleep 等待、内部字典断言、替换私有属性依然存在（见 `tests/conftest.py:384-398`），只是在本机负载下没有暴露。

**覆盖率热点（R01 应登记为回归风险，而不是被 88% 的全局数字掩盖）**：

| 模块 | 覆盖率 | 备注 |
|---|---|---|
| `app/db/protocols.py` | 0% | 纯 Protocol 声明，可接受 |
| `app/__main__.py` | 0% | 入口 shim |
| `app/twilio_bridge.py` | 68% | provider adapter，R04 相关 |
| `app/mcp_tools.py` | 68% | 工具入口，R11/R13 相关 |
| `app/mcp_oauth/registration.py` | 67% | R21b 相关 |
| `app/owner_transfer.py` | 81% | saga 补偿分支未覆盖，R18 相关 |
| `app/smoke_prepare.py` | 80% | R22 相关 |
| `app/call_state.py` | 87% | **197 行未覆盖**，正是 R03–R18 要拆的路径 |

`call_state.py` 的未覆盖行集中在 482–3390 的中间段，与 R14–R18 的目标区域高度重叠；拆分这些路径时必须同步补齐，而不是把"覆盖率仍然 ≥85%"当作等价性证明。

## I.3 F01–F13 逐条代码定位复核

判定口径：**已确认**＝代码可直接读出该事实；**结构确认**＝结构事实成立、但故障后果仍需注入复现；**未复现**＝本次既未确认也未否定。

| 编号 | 基线代码定位 | 判定 |
|---|---|---|
| F01 | `app/call_state.py` 144,503 bytes（1545 statements，87% cov）；`CallService.__init__`（`app/call_state.py:132-206`）同时持有 **9 个协作者属性**（`settings`/`db`/`openai`/`exa`/`twilio`/`_activity`/`live`/`finalizer`/`_owner_transfer`）、**3 个任务集合**（`_background`、`_must_finish_background`、`_conference_retry_tasks`）与 **15 个 per-call 运行态收集**（`_activation_locks`、`_voice_end_pending`、`_voice_end_reply_waits`、`_callee_speech_epochs`、`_callee_speaking`、`_live_conversations`、`_call_audio`、`_media_tokens`、`_media_ready`、`_tool_seen_calls`、`_queued_latency_events`、`_pending_questions`、`_hold_state`、`_undelivered_answers`、`_event_notifiers`）；`tests/conftest.py:384-398` 在构造**之后**赋值 `svc.live` / `svc.finalizer` / `_test_twilio` / `_test_live` / `_test_finalizer` / `_test_exa`；`OwnerTransferCoordinator` 通过 `live=lambda: self.live` 等晚绑定回调反向读取（`app/call_state.py:170-188`） | **已确认** |
| F02 | `app/db/plans.py:36-81`：同一 `BEGIN IMMEDIATE` 内只做 `deployment_control` 锁检查 + `plans` 单次消费 CAS + `calls` INSERT；**没有对其他非终态 calls 的任何容量查询**。同 plan 重复启动确实被挡住（`rowcount != 1 → return False`），但两个不同 plan 可并发进入 | **已确认** |
| F03 | `app/models.py:39-44`：`TERMINAL_STATES` 只含 COMPLETED/FAILED/TIMED_OUT/TRANSFERRED，**不含 TERMINATING**；`app/call_state.py:662-667`：先 `get_call` 判 state，再**单独**调用 `set_flag_once(call_id,"callee_dialed")`，读与 CAS 不在同一事务；`app/db/calls.py:253-268`：`set_flag_once` 的 UPDATE 谓词只有 `WHERE call_id=? AND flag=0`，没有生命周期谓词 | **已确认**（结构性竞态窗口；未注入复现） |
| F04 | `app/twilio_bridge.py:76,113,184`（participant create）/`:153`（stream create）全部经 `await asyncio.to_thread(create)`；调用方 `CallService` 直接 await 这些 facade 方法。线程内 SDK 调用在协程取消后仍会继续，SID 结果无人接收 | **结构确认** |
| F05 | `app/call_state.py:3284-3287`：`_watchdog` = `while True: await asyncio.sleep(5); await self._watchdog_once()`，**无 try/except**；`_watchdog_once`（3289-3299）首步即 `self._activity.flush()` 与 `self.db.list_nonterminal_calls()`；`start_watchdog`（3226-3228）只在启动时调用一次，任务死亡后无重启。`app/main.py:156-158`：`/healthz` 返回常量 `{"status":"ok"}`，不反映监督状态 | **已确认** |
| F06 | `app/models.py:402-439`：`AnswerCallQuestionRequest` 在入口校验 `resolution`/`sources_checked`（`not_found` 必须包含 `agent_memory` + `conversation_history`）；`app/mcp_tools.py:311-315`：通过校验后只把 `call_id/question_id/answer` 传给 `CallService.answer_call_question`；`app/call_state.py:2251-2256`：核心签名只有这三个参数；`app/db/questions.py:115-141`：`claim_question_answer` 的 UPDATE 只写 `answer` + `resolved_at`，表内无来源字段 | **已确认（参数在适配层被丢弃）** |
| F07 | `app/openai_live.py` 42,194 bytes；快速 activity 观察与 FIFO 业务派发分离的时序边界位于 `app/call_state.py` + `app/openai_live.py` + `tests/test_voice_closing.py` | **结构确认**（时序故障未复现） |
| F08 | `app/models.py:190-191`：`StoredCallResult` `model_config = ConfigDict(extra="forbid")`（`app/` 内共 10 处 `extra="forbid"`）；`app/db/engine.py`（186 statements，87% cov）使用探测/补列式初始化，284–483 存在未覆盖的迁移分支；无 migration ledger 表 | **已确认**（旧读新的实际失败路径未构造） |
| F09 | `app/finalizer.py:198,287` 读取 `extracted.objective_assessments`，`app/models.py:180` 定义 `ObjectiveAssessment`，但 `StoredCallResult`（`app/models.py:190+`）不含该字段 → 目标评估不落盘；`app/finalizer.py:82` `self._locks: dict[str, asyncio.Lock] = {}`，全文件只有 `:103` 的 `setdefault`，**无 pop / 清理**；`app/costs.py:16` `compute_call_cost(call, settings)` 用**当前** `Settings` 价格字段重算，调用点在 `app/call_state.py:3001,3015` | **已确认**（三项事实均成立） |
| F10 | `app/routes/mcp_oauth.py:38`（`async def mcp_oauth_consent_submit`）→ `:63` 直接调用 `provider.owner_secret_matches(owner_secret)`；`app/mcp_oauth/provider.py:312-313` → `app/mcp_oauth/crypto.py:45-51` `password_hasher.verify(...)`；`crypto.py:26-32` 参数 `time_cost=3, memory_cost=65536, parallelism=4`。即 64 MiB / 3 轮的 Argon2id 校验**同步跑在事件循环上** | **已确认**（对语音延迟的实际影响量级未测） |
| F11 | `app/routes/openai_webhooks.py:21-23`：签名校验后**先** `record_webhook_once(webhook_id)`，失败即 400 "replayed or missing webhook-id"；随后才做 event type 判断与 `LiveIncomingEvent.model_validate`，业务异常同样转 400。即"已收下 receipt 但没处理完"的事件在重投时被当成 replay 拒绝 | **已确认（顺序）**；重试语义的端到端影响未做故障测试 |
| F12 | `.github/workflows/ci.yml`：`ruff format --check app tests scripts` + `ruff check app tests scripts` + `mypy app` + `pytest -q --cov=app`（85% 门槛）。`.github/workflows/fly-deploy.yml`：`ruff format --check app tests` + `ruff check app tests` + `mypy app` + `pytest -q`（**无 scripts、无覆盖率门槛**）。部署 job 声明 `environment: production`，且**先**取得租约再执行 `flyctl deploy --remote-only`（远程构建）。`app/db/deployment.py:13` `DEPLOYMENT_LOCK_TTL = timedelta(minutes=15)` | **已确认** |
| F13 | 直接读 `os.environ` 的位置：`app/cli.py:104`、`app/doctor.py:132`、`app/local_start.py:69`、`app/smoke_prepare.py:44`、`app/tunnel.py:214`；`Settings.model_config` 的 `env_file=(".env", ".env.local")`（`app/settings.py:122-128`）；`Settings.from_environ` 走显式映射并忽略 dotenv（`tests/test_doctor.py:297-312`）。即存在多条配置入口，语义由调用点各自决定 | **已确认** |

## I.4 基于实测对原计划的修订

1. **R01 第一步已完成**，其"未运行 pytest / Ruff / mypy"的前提不再成立；实施时该工作包应改为"重复并固化本基线"，而不是从头建立。
2. **F12 的差异是具体且可量化的**（见 I.3）：部署工作流比 CI 少检查 `scripts/`、且不带覆盖率门槛。R00 的"抽出 CI 与部署共同调用的验证入口"因此有了明确的最小内容：让部署 job 复用与 CI 完全相同的 4 条命令。
3. **部署租约时序确认成立**：`fly-deploy.yml` 在 `Wait for calls and acquire deployment lease` 之后才执行 `flyctl deploy --remote-only` 的远程构建，而 TTL 只有 15 分钟。R23a 的动机（构建窗口吃掉租约）成立。
4. **`environment: production` 已声明，但是否配置 required reviewers 无法从仓库判定**。R00 第一步必须由操作者确认，不能由代码审查推断。
5. **门槛基线应锁定为实测值**：当前真实覆盖率是 **88.43%**，不是 85%。R00/R01 应把 88% 登记为回归基线，防止后续拆分把 88% 静默耗到 85% 才报警。
6. **`call_state.py` 未覆盖的 197 行**是拆分风险的实际分布区，应作为 R14–R18 每个切片的验收附件，而不是只看全局覆盖率。

## I.5 复现命令

```bash
# 固定在基线提交
git rev-parse HEAD   # 7de5e69be9083a6fb484845645f1b05b4bc67254

# 静态检查 + 完整离线测试（无生产环境变量）
env -i PATH="$PATH" HOME="$HOME" .venv/bin/python -m ruff format --check app tests scripts
env -i PATH="$PATH" HOME="$HOME" .venv/bin/python -m ruff check app tests scripts
env -i PATH="$PATH" HOME="$HOME" .venv/bin/python -m mypy app
env -i PATH="$PATH" HOME="$HOME" .venv/bin/python -m pytest -q --cov=app

# 关键竞态模块重复 3 次
env -i PATH="$PATH" HOME="$HOME" .venv/bin/python -m pytest -q \
  tests/test_activation.py tests/test_voice_closing.py tests/test_tool_transfer_safety.py \
  tests/test_teardown_recovery.py tests/test_lifespan_cleanup.py tests/test_fork_safety.py \
  tests/test_ask_agent.py tests/test_live_dispatch.py tests/test_send_dtmf.py \
  tests/test_hold.py tests/test_carrier_monitor.py tests/test_call_audio.py
```

`env -i` 的作用是保证不继承任何生产凭据；测试 fixture 与 `from_environ` 都显式传值，因此该方式不影响结果。

---

## I.6 R01 第二步：接口契约快照（静态部分，已执行）

R01 要求"记录 MCP 工具名、输入输出 schema、关键错误码、HTTP 路径、Live payload 和提示词快照"。本次完成其中**可从源码静态枚举的部分**；完整 JSON Schema 与 prompt 字节级快照需要先构造完整 `Settings` 再导入应用，留在 R01 剩余步骤。

### MCP 工具（7 个，全部定义在 `app/mcp_tools.py`）

| 工具 | 注解（readOnly / destructive / openWorld / idempotent） |
|---|---|
| `prepare_phone_call` | False / False / **False** / False |
| `start_phone_call` | False / False / **True** / False |
| `get_call_result` | **False** / False / True / False |
| `end_phone_call` | False / **True** / True / **True** |
| `get_phone_call` | **True** / False / False / True |
| `wait_for_call_event` | **True** / False / False / True |
| `answer_call_question` | False / False / False / False |

注意 `get_call_result` 的 `readOnlyHint=False`：它确实带有副作用（触发 finalization），与 Part II 中 R08"不把 `get_call_result` 伪装成绝对无副作用查询"的判断一致。

### HTTP 路径（挂载于 `app/main.py:151-166`）

| 方法 | 路径 | 鉴权 |
|---|---|---|
| POST | `/webhooks/openai` | OpenAI 签名（`unwrap_openai_webhook`） |
| POST | `/webhooks/twilio/amd` | 见 `app/security.py` |
| POST | `/webhooks/twilio/conference` | 同上 |
| POST | `/webhooks/twilio/participant-status` | 同上 |
| POST | `/webhooks/twilio/announce-dtmf` | 同上 |
| GET | `/diagnostics/live-test` | `require_debug_token` |
| GET | `/calls/{call_id}` | `require_debug_token` |
| GET | `/calls` | `require_debug_token` |
| GET | `/healthz` | 无（常量响应） |
| POST | `/internal/deployment-lock` | `require_deploy_guard_token` |
| DELETE | `/internal/deployment-lock` | `require_deploy_guard_token` |
| GET/POST | `/oauth/consent` | CSRF + owner secret |
| POST | `/internal/mcp-oauth/revoke-all` | `require_debug_token` |
| — | `/mcp`（Streamable HTTP，`MCPAuthMiddleware`） | Bearer + `X-Agent-User-Id` |

OAuth 分支还额外 `mount("/")` 一个独立的 MCP app（`app/main.py:166`），因此 OAuth discovery 位于站点根路径；该分支只在启用 OAuth 时存在（测试中为 404）。

### 业务错误码（工具返回体 `{"code": ...}`）

`call_not_found`、`confirmation_mismatch`、`confirmation_required`、`deployment_in_progress`、`invalid_answer_submission`、`invalid_call_state`、`plan_not_found`、`plan_unavailable`（共 8 个，来自 `app/` 全量 grep）。

此外仍有**非结构化**错误：`ToolError("unknown question")`（`app/mcp_tools.py:321`）与 `ToolError(str(exc))`（`:71`、`:106`）。这正是 R06"集中内部错误码映射，MCP/HTTP 仍输出兼容结果"要收敛的对象；在收敛前，契约测试必须同时固定这两种形态。

### 本次未生成

Live payload 快照与 prompt 字节预算（已有 `tests/test_bridge_payloads.py`、`tests/test_prompt_limits.py` 覆盖，但未导出为基线文件），以及 7 个工具的完整输入/输出 JSON Schema。

---

# Part II · 原始审查与重构计划（正文，保持原文）

# Agent Call：代码审查与渐进式重构计划

- 仓库：`XiyaoWang0519/agent-call`
- 固定基线：`7de5e69be9083a6fb484845645f1b05b4bc67254`
- 审查日期：2026-09-14
- 方式：通过 GitHub 连接器进行只读静态审查。
- 执行状态：本次未修改仓库、未创建 PR、未部署、未使用生产密钥、未拨打真实电话。
- 验证限制：运行环境未能克隆仓库；未运行 pytest / Ruff / mypy / 构建 / live-phone / 性能测试。本文的测试是执行要求，不是通过报告。

## 结论

保留 **单实例、单 owner、SQLite、模块化单体**。重构的目标不是把大文件拆成更多文件，而是让计划、实时媒体、工具交付、问题生命周期、终止补偿、恢复和结果提取各自有明确的数据、任务与副作用所有者。

`CallService` 是优先拆分对象，但不是第一个动刀位置。正确顺序是：发布边界与回归基线 → 构造时注入依赖 → 独立安全修复 → 类型与持久化兼容 → 低风险用例和 Live/工具拆分 → 音频运行态和任务所有权 → 最后拆启动、终止、恢复。OAuth、CLI 与结果服务走独立支线。

下面 R00–R23 是 **24 个工作包，不是要求 24 个巨大 PR**。明确标注 a/b/c 的工作包必须按行为边界拆开。一次只实现一个有清晰验收条件的切片；没有必要完成全部工作包才获得收益。

## 1. 审查范围与证据等级

### 逐段或完整阅读的核心源文件

`app/call_state.py`、`app/openai_live.py`、`app/owner_transfer.py`、`app/main.py`、`app/models.py`、`app/settings.py`、`app/mcp_tools.py`、`app/finalizer.py`、`app/twilio_bridge.py`、`app/call_activity.py`、`app/call_audio.py`、`app/policy.py`、`app/prompts.py`、`app/exa_search.py`、`app/costs.py`、`app/security.py`。

数据层包括 `app/db/engine.py`、`plans.py`、`calls.py`、`questions.py`、`transcripts.py`、`termination.py`、`transfers.py`、`deployment.py` 与 `__init__.py`。

入口/运维包括 `routes/openai_webhooks.py`、`routes/twilio_webhooks.py`、`routes/mcp_oauth.py`、`mcp_oauth/provider.py`、`crypto.py`、`registration.py`、`consent.py`、`cli.py`、`setup.py`、`doctor.py`、`local_start.py`、`tunnel.py`、`pyproject.toml`、`Dockerfile`、主要 CI/生产部署工作流。

### 重点测试与辅助工具审查

完整阅读了 `tests/conftest.py`、`test_activation.py`、`test_voice_closing.py`、`test_teardown_recovery.py`、`test_lifespan_cleanup.py`、`test_fork_safety.py`。`test_tool_transfer_safety.py` 阅读了前部重点场景，非全文件；其余测试按目录和用途盘点，未声称全部逐行审核。

阅读 README、架构说明和仓库指引，并盘点 scripts/live_phone；完整读取其 CLI 入口。该工具已经包含显式实例确认、预算检查、不重试丢失的 start 响应和独立 reaper 入口，建议复用，不重新造一套真实电话测试系统。

### 尚不能给出全面审核结论的部分

未逐段精读全部 `app/db/oauth.py`、telemetry、全部辅助模块、全部部署模板、全部历史迁移文档、全部测试与 live-phone 内部实现；没有完整审查所有未合并 PR；没有运行时日志或生产数据。本计划涵盖这些模块的后续审查/验收要求，不将“未发现”当成“已证明安全”。

### 证据等级

**代码可直接确认**：结构耦合、参数被丢弃、存储字段、配置与工作流内容。  
**静态风险，需要复现**：跨 await 竞态、provider 创建取消后的远端结果、watchdog 失效影响、Argon2 对语音延迟的影响、租约超时窗口。  
**设计建议**：目标模块、默认容量政策、分阶段迁移方案。建议不是已实现特性。

## 2. 当前主调用链

```text
MCP prepare
  → ContextPacket / policy
  → SQLite plan + 确认摘要
MCP start
  → plan / deployment guard / calls row
  → Twilio agent participant → OpenAI SIP incoming
  → accept / sideband / 初始 session 确认
  → Twilio callee participant
  → callee answer + carrier monitor ready
  → unmute / enable conversation
Live 语音与 delegated backend
  → 快速 activity / input observation
  → 有界 FIFO 业务事件派发
  → tool execution / result delivery / ask_agent
终止 / 转人工
  → durable claim + 补偿 / media teardown
  → terminal state
  → transcript checkpoint / extractor / result / owner notification
```

转人工成功之后，AI 流程可以进入终态，而 owner-callee conference 仍然存在。这是产品语义，不是简单的资源泄漏；但其 lifetime ownership 必须清楚。

## 3. 主要发现与处理原则

| 编号 | 代码依据 | 判断 | 优先动作 |
|---|---|---|---|
| F01 | `call_state.py`、`owner_transfer.py`、`tests/conftest.py` | 约 144 KB 的 CallService 兼任多个运行域；已拆 collaborator 仍受 facade/测试猴补丁牵引 | R02、R06、R08–R18 |
| F02 | `db/plans.py::claim_plan_and_create_call`、`CallService.start`、架构文档 | 同 plan 单次消费不等于不同 plan 的单通话容量 | R03；先明确 AI 通话与人工转接容量定义 |
| F03 | `handle_sideband_open`、`db/calls.py::set_flag_once` | 终态过滤不包含 TERMINATING；拨号 claim 缺少生命周期谓词 | R03；竞态复现并修 CAS |
| F04 | `twilio_bridge.py`、`OwnerTransferRun.run`、普通 start | to_thread 创建有取消后的未知结果；transfer 已有迟到 SID 补偿模式 | R04；只修验证过的路径，不盲重试 |
| F05 | watchdog 主循环、`main.py` health | 关键监督退出未必能从健康响应发现 | R05；readiness 不等于重启指令 |
| F06 | `mcp_tools.py`、`AnswerCallQuestionRequest`、`db/questions.py` | resolution/sources_checked 在入口验证后未穿透核心和持久化 | R13；来源声明不是来源查询证明 |
| F07 | `openai_live.py`、`test_voice_closing.py` | 快速观察、FIFO 派发、response 批次和音频播放证据是正确性边界 | R09–R16；不得用并发/新队列破坏 |
| F08 | `db/engine.py`、`StoredCallResult`、`db/transcripts.py` | 迁移缺显式版本账本；extra=forbid 导致新增 result JSON 字段可破坏旧读者 | R07；旧读新数据是回滚门槛 |
| F09 | `finalizer.py`、`models.py`、`costs.py` | 目标评估未保存；keyed lock 可能长期增长；旧费用随当前 rate 变化 | R19–R20 分开处理 |
| F10 | OAuth route / provider / crypto | 密码校验同步执行；完整事务路径需进一步逐段审查 | R21；bounded off-loop + 故障测试 |
| F11 | `routes/openai_webhooks.py` | receipt 去重在处理之前；失败后的重试语义应显式设计 | R23b；不得简单把 dedup 移至副作用之后 |
| F12 | CI / deploy / `db/deployment.py` | 合并触发部署；租约先于远程构建且 TTL 有限；验证范围不完全一致 | R00 + R23a |
| F13 | settings / doctor / setup / local_start | 配置读取、环境优先级和进程管理有多个入口 | R22；保留现有安全措施 |

## 4. 必须保持的系统不变量

1. 没有符合现有授权契约的 prepare / 明确确认，不产生新付费拨号；同一个 plan 不能消费两次。
2. evaluation 模式不拨号。普通离线测试没有真实 provider egress、密钥、账号或电话。
3. OpenAI 入站验证和 call/plan 身份绑定不能因重构被弱化。Twilio 回调继续按配置的可信 public origin 校验。
4. 预热确认、callee answer、monitor readiness 与激活顺序保持；禁止终止获胜后重新获得拨号/激活权。
5. 新输入能让旧请求 epoch 失效。DTMF、转接、告别不能被旧响应恢复执行。
6. 音频 fast path 不等待 DB/HTTP；业务事件保持有界和既有排序语义。
7. 工具执行去重与工具输出交付是两件事；continuation 只能在批次条件允许时出现一次。
8. 问题回答、超时、取消由数据库原子 claim 决胜；晚答不得注入正在拆除的 sideband。
9. 告别必须对应最新真实 transcript，且有当前 carrier playback 证据和连续三秒回复窗口。缺帧、陈旧帧、失联不是“安静了足够久”。carrier 观察不等于物理听筒收到。
10. 终止必须有明确 owner；callee、agent、owner 的不同离开回调不应相互夺走已经完成的转接结果。
11. provider 创建结果未知不能被当作确定失败再创建；已知 SID 应持久化或补偿，清理失败必须可恢复。
12. carrier 停止计费相关清理不能等待慢提取；电话结束、提取成功和目标完成是三个不同状态。
13. transcript 顺序、原始片段和 evidence turn IDs 保留；不可重写证据使提取看上去更成功。
14. 数据迁移必须保持历史 Realtime 数据、旧结果和 OAuth 密文可读；crypto 派生标签不是可随意改名的品牌字符串。
15. 生产发布必须尊重活跃通话和清理阶段；readiness 故障不能被直接等同于自动杀死当前进程。

## 5. 目标结构：先建立边界，再考虑文件路径

下列目录是建议，不是当前仓库结构；不要一次性移动整个 app。

```text
app/
  main.py                       # HTTP/ASGI 入口
  bootstrap.py                  # 真实依赖与生命周期装配
  call_state.py                 # 暂留兼容 CallService facade
  calls/
    contracts.py / ports.py
    plans.py / queries.py
    questions.py / tools.py
    runtime.py / closing.py
    tasks.py / startup.py
    termination.py / recovery.py
  live/
    config.py / events.py
    session.py / tool_batches.py
  openai_live.py                # 暂留 re-export/facade
  owner_transfer.py             # 保留经过竞态设计的 saga，改窄接口
  twilio_bridge.py              # SDK adapter
  finalization/
    service.py / extractor.py / policy.py
  finalizer.py                 # 暂留 facade
  db/
    engine.py                  # 共享连接与事务保障
    plans.py / calls.py / ...
    migrations/
  mcp_oauth/                   # 独立认证边界
  cli.py / setup.py / ...       # 独立运维入口
```

依赖方向：入口 → 应用用例 → 窄接口；provider/SQLite 适配器实现接口；bootstrap 负责装配。不能通过给所有新模块传整个 CallService、整个 Database facade 或整个 Settings 来伪装成解耦。

### 状态与任务所有权

| 信息/资源 | 唯一负责人 | 不能做的事情 |
|---|---|---|
| plan、call 生命周期、claim、transfer outcome | SQLite 业务 repository | 运行态字典成为第二事实来源 |
| WebSocket、接收队列、工具批次输出 | LiveSession / ToolBatchDelivery | 任意其他模块直接改 session 内部字典 |
| 音频活动、request epoch、closing/hold 状态 | 对应 per-call 协调器 | 全塞进一个新的 God Object |
| 外部创建与补偿任务 | TaskSupervisor + 所属用例 | caller 取消后无人观察晚到结果 |
| owner transfer attempt | OwnerTransferRun | 无视已有阶段改成普通 CRUD |
| 结果提取与并发 finalize | FinalizationService | 任意 get 查询无限扩张锁表 |

## 6. 执行顺序与可停靠里程碑

```text
R00 → R01 → R02
              ├─ R03 → R04
              ├─ R05
              └─ R06 → R07
                    ├─ R08
                    ├─ R09 → R10 → R11 → R12 → R13
                    └─ R14 → R15
R04 + R08 + R10 + R15 → R16 → R17 → R18

独立支线：R19 → R20；R21；R22
发布租约 R23a：R00 后即可独立前置
最终文档/兼容层收口 R23c：相关主线完成之后
```

**里程碑 M1：安全地继续迭代。** 完成 R00–R05；不强求 CallService 已显著变短。  
**里程碑 M2：核心边界可维护。** 完成 R06–R18；新增工具或修改一个流程不再牵连整套服务与测试。  
**里程碑 M3：作为开源软件可长期运行。** 按需要完成结果、OAuth、CLI/安装和发布支线。

R07 之后才能引入需要迁移的新数据；R04 等安全补丁可先做无 schema 的版本。没有必须等到全部重构完成才发布的要求，但每次发布须通过对应里程碑门槛。

## 7. 工作包明细

### R00 · 先让合并与生产发布的关系可控

**阶段：** A · 发布边界与基线  
**性质：** 发布流程  
**依赖：** 无  
**涉及：** `.github/workflows/ci.yml`、`.github/workflows/fly-deploy.yml`

**现状与动机：** main 的 push 会触发生产部署工作流。配置中的 production environment 是否已有人工审批，不能仅凭仓库文件确定。

**执行步骤**

- [ ] 确认并记录当前生产审批机制；重构期间采用明确的审批、手动晋级或受控发布分支策略，不默认每次合并立即上线。
- [ ] 抽出 CI 与部署共同调用的验证入口，防止当前 app/tests/scripts 检查范围和覆盖率门槛漂移。
- [ ] 先建立可回退的代码基线；数据库备份必须在实际执行时由操作者确认，不能假定本次审查已经备份。

**必须回归**

- [ ] 工作流静态测试继续确认仅 upstream 可以使用生产部署配置。
- [ ] PR 验证不得使用生产密钥、拨打电话或自动获取真实隧道。

**完成定义：** 代码合并与发布的触发方式有明确记录；后续所有工作包引用同一验证入口。

**回滚约束：** 恢复原工作流配置即可；本包不改变数据库或运行时协议。

**本包不做：** 不部署当前审查产物；不删除已有部署空闲保护。

---

### R01 · 建立行为契约与可重复故障基线

**阶段：** A · 发布边界与基线  
**性质：** 测试  
**依赖：** R00  
**涉及：** `tests/conftest.py`、`tests/test_activation.py`、`tests/test_voice_closing.py`、`tests/test_tool_transfer_safety.py`、`tests/test_teardown_recovery.py`、`tests/test_lifespan_cleanup.py`、`tests/test_fork_safety.py`、`scripts/live_phone/`

**现状与动机：** 已有高价值竞态测试，但有 sleep 等待、内部字典断言和替换私有属性；本次审查没有实际运行它们。

**执行步骤**

- [ ] 在干净、无生产环境变量的环境运行现有基线，保存测试结果、覆盖率、依赖锁和提交 SHA；当前覆盖率配置门槛为 85%，这不是本次测得的覆盖率。
- [ ] 记录 MCP 工具名、输入输出 schema、关键错误码、HTTP 路径、Live payload 和提示词快照。
- [ ] 建立故障点表：外部创建前后、数据库提交前后、取消、重复/乱序回调、重启、音频缺帧、问题截止时间。
- [ ] 增加默认禁止意外外部网络访问的测试边界；需要本地回环或模拟 HTTP 的测试显式放行。

**必须回归**

- [ ] 先重复运行现有关键竞态测试，记录而不是掩盖 flaky case。
- [ ] 异常退出后检查无未归属后台任务、数据库连接泄漏；外部资源通过 fake 断言而非真实拨号检查。

**完成定义：** 有可复现的 baseline、契约快照与已知失败列表；没有为了变绿而降低覆盖率或删除场景。

**回滚约束：** 纯测试提交可独立回退。

**本包不做：** 不在此包更换模型或修改 prompt；不把 live-phone run 接到每次 PR 自动执行。

---

### R02 · 构造时注入依赖，建立 TestHarness

**阶段：** A · 发布边界与基线  
**性质：** 结构  
**依赖：** R01  
**涉及：** `app/call_state.py`、`app/main.py`、`app/owner_transfer.py`、`tests/conftest.py`

**现状与动机：** 测试在服务构造后替换 live/finalizer，生产协作者因此使用晚绑定 lambda 和兼容属性。

**执行步骤**

- [ ] 为 Live、Telephony、Finalization、Clock 和任务创建定义必要的窄接口；不要为所有类都建立抽象基类。
- [ ] 在应用装配位置构造真实依赖，CallService 接收依赖；保留现有公开 facade。
- [ ] 用 TestHarness 显式持有 fake_twilio、fake_live、fake_finalizer 等，替换新增 _test_* 属性。
- [ ] 用事件屏障、可控 future 和 fake clock 逐步替换等待固定时间；每次只迁移一组测试，旧测试仍需通过。

**必须回归**

- [ ] 同一业务测试可在不 monkeypatch CallService 私有状态的情况下驱动 provider 回调。
- [ ] 初始化失败、服务停止、部分依赖创建失败的清理顺序保持。

**完成定义：** 新组件不需要反向读取可替换的 CallService 属性；无需制造公开生产 setter 来服务测试。

**回滚约束：** 保留构造参数兼容适配，旧入口继续可用。

**本包不做：** 不改 await 顺序；不在一个 PR 重写所有测试。

---

### R03 · 原子通话准入与终止中的拨号保护

**阶段：** B · 独立安全加固  
**性质：** 行为修复；必须独立 PR  
**依赖：** R02  
**涉及：** `app/db/plans.py`、`app/db/calls.py`、`app/call_state.py`、`tests/test_activation.py`

**现状与动机：** 同一 plan 的单次使用不等于不同 plan 之间的并发上限；callee_dialed 的通用一次性标记没有附带允许的生命周期谓词。

**执行步骤**

- [ ] 用并发测试确认不同 plan 可同时启动的当前行为，然后明确产品策略：默认仅一个受 AI 控制的非终态通话。
- [ ] 在同一 BEGIN IMMEDIATE 事务内执行容量检查、部署锁检查、plan 消费和 calls 创建。
- [ ] 增加命名操作 claim_callee_dial：只允许合法 setup 状态，要求 termination_claimed=0，并原子设置拨号 claim。
- [ ] 返回稳定的 busy / call_ending 等错误，保持旧成功响应契约；终止后迟到的 handshake 不得重新产生拨号权。

**必须回归**

- [ ] 两个不同 plan 同时启动只有一个获得准入；同一 plan 重复启动仍只有一次。
- [ ] 终止与 session ack 同时到达：终止获胜后不创建 callee。
- [ ] TRANSFERRED 人工会话、cleanup_pending 和恢复历史多通话数据单独测试，不误当成同一容量定义。

**完成定义：** 文档中的并发策略与事务实现一致；明确此 claim 不是远端 exactly-once 保证。

**回滚约束：** 无 schema 变化时可回退；发布前确认是否已有超出新容量策略的非终态历史数据。

**本包不做：** 不使用仅进程内 semaphore 代替事务；不破坏多条 stranded calls 的恢复夹具。

---

### R04 · 普通拨号的取消安全与迟到 SID 补偿

**阶段：** B · 独立安全加固  
**性质：** 行为修复；必须独立 PR  
**依赖：** R02, R03  
**涉及：** `app/call_state.py`、`app/twilio_bridge.py`、`app/owner_transfer.py`、`tests/test_activation.py`、`tests/test_teardown_recovery.py`

**现状与动机：** Twilio SDK 在线程中执行。调用协程取消不意味着远端创建失败；转人工已有更完整的迟到创建补偿，普通启动路径需要同等级验证。

**执行步骤**

- [ ] 借鉴 OwnerTransferRun 的设计，而不是重写它：在外部创建前登记任务与操作意图，明确持有者。
- [ ] 保证调用方取消后，受监督工作仍能取得迟到的 SID，并持久化或执行补偿。
- [ ] 将“创建失败”与“结果未知”区分；未知结果不盲目重试创建。
- [ ] 对无法确认清理完成的情况保留 durable cleanup/reconciliation 记录；实际需要新表时先执行 R07。

**必须回归**

- [ ] 取消发生在 SDK 开始前、远端成功后返回前、SID 返回后写 DB 前。
- [ ] 模拟 DB 已提交但 await 抛错，验证精确状态对账。
- [ ] 补偿失败、重复回调、服务关闭期间创建完成：不得把未知清理报告为成功。

**完成定义：** 每个已知 SID 有明确持有者或清理记录；进程死亡场景依靠持久化和 provider 对账，而不是仅靠 shield。

**回滚约束：** 尽量先无 schema 修复；新增清理字段/表需 additive migration，并让旧读者仍可读取。

**本包不做：** 不宣称分布式 exactly-once；不以无限重试或无限等待掩盖未知状态。

---

### R05 · 监督 watchdog 并暴露真实 readiness

**阶段：** B · 独立安全加固  
**性质：** 行为修复；必须独立 PR  
**依赖：** R02  
**涉及：** `app/call_state.py`、`app/main.py`、`tests/test_teardown_recovery.py`、`tests/test_lifespan_cleanup.py`

**现状与动机：** watchdog 的后续数据库读取失败可能使任务退出；固定 healthz 响应不足以表示安全监督仍在运行。尚未实际注入故障复现。

**执行步骤**

- [ ] 为 watchdog 定义可观察的状态、有限重试和失败升级；活动 flush 自身已有失败回填逻辑，保留。
- [ ] 关键监督不可用时拒绝新通话，并暴露 not-ready；现有健康端点保持向后兼容。
- [ ] 区分进程存活与可接收新业务；正在进行的通话继续保有音频与补偿处理。

**必须回归**

- [ ] 注入 list_nonterminal_calls / DB 连接异常，验证失败可见，不静默丢失监督。
- [ ] 恢复后 readiness 能回到正常；关闭期间不创建重复 watchdog。
- [ ] readiness 失败不直接触发中断现有通话的自动重启策略。

**完成定义：** 从运行状态能判断监督是否存活；错误时不会继续无保护接受新拨号。

**回滚约束：** 可退回原健康端点行为；不要在同一次上线同时改变平台重启策略。

**本包不做：** 不将任意短暂 DB 失败直接转为挂断所有通话；不把 readyz 与 destructive liveness restart 混为一谈。

---

### R06 · 引入窄类型契约与业务操作接口

**阶段：** C · 可演进的类型与持久化边界  
**性质：** 结构  
**依赖：** R02  
**涉及：** `app/models.py`、`app/db/calls.py`、`app/db/plans.py`、`app/db/questions.py`、`app/call_state.py`、`app/mcp_tools.py`

**现状与动机：** 内部广泛传递 dict[str, Any]、字符串错误和跨领域 Settings，严格 mypy 也无法约束这些字典的业务形状。

**执行步骤**

- [ ] 按用例引入 PlanRecord、LifecycleRecord、QuestionRecord、AnswerCommand、ToolExecutionResult 等小型类型，不制造包含所有字段的万能对象。
- [ ] 先在数据库出口和调用入口加适配器，保留原 SQL 和事务边界。
- [ ] 对生命周期操作使用 claim/finish/promote 等业务命名接口；限制新增代码直接 update_call(state=...)。
- [ ] 集中内部错误码映射，MCP/HTTP 仍输出兼容结果。

**必须回归**

- [ ] 旧 dict API 与新 typed adapter 在同一 fixture 上结果一致。
- [ ] 错误码、枚举序列化、nullable provider ID、历史缺失字段不回退。

**完成定义：** 新抽出的应用服务不依赖通用 Any 字典或完整 Provider SDK 类型。

**回滚约束：** 移除适配层即可，数据库和 wire format 未变化。

**本包不做：** 不引入 ORM；不改变状态机或错误文本而不更新契约测试。

---

### R07 · 版本化迁移与旧版本读取兼容测试

**阶段：** C · 可演进的类型与持久化边界  
**性质：** 持久化基础设施  
**依赖：** R06  
**涉及：** `app/db/engine.py`、`app/db/transcripts.py`、`app/models.py`、`app/mcp_oauth/crypto.py`、`tests/test_policy_and_db.py`

**现状与动机：** 现有初始化采用探测/补列和历史迁移逻辑；result_json 的 extra=forbid 意味着添加 JSON 字段也可能破坏旧版本回滚。

**执行步骤**

- [ ] 建立轻量 SQLite migration ledger（版本、校验信息），承接而非删除现有 baseline/历史升级逻辑。
- [ ] 为旧 calls、旧 Realtime usage、旧 result_json、OAuth 已有密文建立人工合成、无真实个人数据的兼容性 fixtures。
- [ ] 所有持久化变化要求新读旧、旧读新的明确结论；不兼容 writer 必须 reader-first 部署或先写 sidecar 表。
- [ ] 保留 WAL、共享写连接、事务取消保护及 SQLite 同步语义；迁移期间不混合 ORM 替换。

**必须回归**

- [ ] 空库初始化、旧库升级、重复启动、迁移中断、只读/损坏库的明确失败。
- [ ] 用上一已发布版本读取新写数据；OAuth key derivation 标签和历史密文保持。

**完成定义：** 迁移顺序可追踪；回滚能力通过数据兼容测试而不是仅凭“新增列”推断。

**回滚约束：** 逐个版本记录兼容矩阵；不能自动降级的变化在上线前明确阻止或采取 reader-first。

**本包不做：** 不改 agent-call-grok-oauth-* 派生标签；不删除旧 Realtime 数据字段。

---

### R08 · 抽出计划管理与查询用例

**阶段：** C · 可演进的类型与持久化边界  
**性质：** 结构  
**依赖：** R06  
**涉及：** `app/call_state.py`、`app/mcp_tools.py`、`app/policy.py`、`app/costs.py`

**现状与动机：** 准备计划、确认、状态读取、结果读取与实时通话控制混在同一个服务。

**执行步骤**

- [ ] 抽出 PlanService 与 CallQueries；CallService 暂时保持代理方法。
- [ ] 保留校验、plan TTL、确认文本与一次性消费契约。
- [ ] 明确 get_call_result 当前可能触发 finalization，不把它伪装成绝对无副作用查询。
- [ ] 费用计算保持纯函数；配置快照改动放 R20 行为子包。

**必须回归**

- [ ] prepare-only、过期 plan、重复确认、缺字段和终态结果查询的兼容性快照。
- [ ] evaluation 模式仍允许 prepare、不允许 start。

**完成定义：** 计划和查询服务可独立使用 fake repository 测试，无需启动 LiveSession。

**回滚约束：** 保留旧 facade 转发，路由与工具名称不变。

**本包不做：** 不在此处新增权限模型；不把 finalization 从查询移除而不做 API 设计。

---

### R09 · 抽出 Live 配置、协议事件解析与 prompt 构建

**阶段：** D · 语音与工具拆分  
**性质：** 结构  
**依赖：** R06  
**涉及：** `app/openai_live.py`、`app/models.py`、`app/prompts.py`、`tests/test_bridge_payloads.py`、`tests/test_prompt_limits.py`

**现状与动机：** provider 特有的配置构建、JSON 事件解析和业务对话控制耦合；手写 schema 容易与请求模型漂移。

**执行步骤**

- [ ] 将 Live payload builder、事件 decoder、provider-specific 类型从核心生命周期隔离。
- [ ] 保持 prompt 文本、模型、工具 schema、事件顺序及字节预算完全不变。
- [ ] 对未知事件采用明确的兼容策略；不在高频音频路径引入昂贵的通用对象验证。

**必须回归**

- [ ] 新旧 payload 完整快照相等；最大 ContextPacket 和可选工具提示仍在限制内。
- [ ] session 配置不匹配必须在不该继续拨号的阶段被拒绝。

**完成定义：** provider 协议变化不再要求修改计划管理、数据库和通话核心的类型文件。

**回滚约束：** 旧模块重新导出相同符号，纯代码回退。

**本包不做：** 不顺便更换 GPT Live/后端模型；不精简 prompt 或改变安全指令。

---

### R10 · 拆出 Live 会话与工具结果交付状态

**阶段：** D · 语音与工具拆分  
**性质：** 高风险结构  
**依赖：** R09, R02  
**涉及：** `app/openai_live.py`、`tests/test_live_dispatch.py`、`tests/test_tool_transfer_safety.py`

**现状与动机：** WebSocket、接收队列、响应状态与工具批次交付共同决定精确时序，不能按函数长度随意拆。

**执行步骤**

- [ ] 由 LiveSession 明确拥有 socket、接收任务、派发任务和有界队列。
- [ ] 由 ToolBatchDelivery 管理 response/delegation 对应的输出、重试和 continuation 条件。
- [ ] 保留同步快速观察与 FIFO 业务派发的双路径；不在快速观察路径等待网络或数据库。
- [ ] 明确每个 response 的 continuation 仅在允许条件满足后发出一次，不等同于单工具执行去重。

**必须回归**

- [ ] 重复 response/tool 事件、迟到输出、发送失败重试、多个工具输出与 response completed 的不同到达顺序。
- [ ] 队列积压时 input 仍能使旧请求失效；关闭必须 drain/close 到既有边界。

**完成定义：** 交付状态归属清晰且可单独测试；业务逻辑无需访问会话内部字典。

**回滚约束：** 不要双运行真实 socket；保留旧 facade，并按单会话选择单一实现。

**本包不做：** 不使用无界并发处理每个事件；不增加应用层语音转发 hop。

---

### R11 · 静态工具注册表与独立执行器

**阶段：** D · 语音与工具拆分  
**性质：** 结构  
**依赖：** R06, R10  
**涉及：** `app/call_state.py`、`app/openai_live.py`、`app/models.py`、`app/exa_search.py`、`tests/test_tool_transfer_safety.py`、`tests/test_send_dtmf.py`

**现状与动机：** 工具 schema、参数解析、业务处理、过期检查、结果持久化和发送重复分布，且不同工具要求不同提交顺序。

**执行步骤**

- [ ] 建立显式静态 ToolSpec 表，包含 name、schema、request parser、handler、允许状态、失败策略和输出策略。
- [ ] 分开执行去重、持久化审计和 provider 交付；保留 ask_agent 的延迟回答语义。
- [ ] 继续检查 request epoch / response identity；外部副作用前的校验不能只在模型提示词里。
- [ ] 保持特定工具的 durability-before-accepted 语义；无效工具快速错误不必被迫等待所有审计写入。

**必须回归**

- [ ] schema 与请求验证器一致；无效参数不调用 provider。
- [ ] DTMF、transfer、close 的 superseded 请求不执行；accepted advisory 在持久化之前不发送。
- [ ] 发送失败与执行失败的重试范围不同，不能重复拨号或重复 DTMF。

**完成定义：** 增加普通工具无需编辑一段横跨生命周期的大分支；执行和交付各自可测试。

**回滚约束：** 按工具逐个迁移，保持原入口映射；不同时切换全部危险工具。

**本包不做：** 不开发通用插件框架；不把所有工具强制成相同的先发送/先提交顺序。

---

### R12 · 抽出 QuestionCoordinator，保持时序语义

**阶段：** D · 语音与工具拆分  
**性质：** 结构  
**依赖：** R11  
**涉及：** `app/call_state.py`、`app/db/questions.py`、`app/mcp_tools.py`、`tests/test_ask_agent.py`

**现状与动机：** 问题创建、wait_for_call_event、超时、回答与侧带交付跨多个服务状态。

**执行步骤**

- [ ] 独立管理问题生命周期、通知、截止时间和回答交付，数据库继续做原子 claim。
- [ ] 保留 sequence cursor、同 tool_call_id 重用、单 pending 问题、问题额度和终止优先级。
- [ ] 将通知视为唤醒提示、数据库视为事实来源，保证通知遗漏不永久丢问题。

**必须回归**

- [ ] 回答与超时同刻发生、终止后晚答、重复问同一 tool、长轮询断开重连、工具结果暂时发送失败。

**完成定义：** QuestionCoordinator 可通过公开用例和 fake clock 测试，不依赖 CallService 的问题字典。

**回滚约束：** 纯提取版本，现有 MCP 和 DB 格式不变。

**本包不做：** 不在同一个 PR 改 sources_checked 持久化；不改变回答截止时间来让测试更容易。

---

### R13 · 把答案来源声明贯穿核心与存储

**阶段：** D · 语音与工具拆分  
**性质：** 行为与数据修复；必须独立 PR  
**依赖：** R07, R12  
**涉及：** `app/mcp_tools.py`、`app/models.py`、`app/db/questions.py`、`app/call_state.py`

**现状与动机：** MCP 验证了 resolution/sources_checked，但只把 answer 传给核心；其他入口和历史审计拿不到相同保证。

**执行步骤**

- [ ] AnswerCommand 贯穿 MCP→应用服务→事务，核心调用必须遵循相同验证。
- [ ] 将 resolution、sources_checked、回答者/入口等最小必要审计信息持久化，设计历史未知值。
- [ ] 原始答案与来源声明分开保存；来源列表只是调用者声明，不能声称证明真实查询已经发生。

**必须回归**

- [ ] 直接调用核心也不能绕过 not_found 的来源声明要求。
- [ ] 并发双答仍只有一个成功；旧问题记录读取兼容。

**完成定义：** 审计可追踪接受了什么答案及哪些来源声明，不能在适配层被丢弃。

**回滚约束：** 使用兼容新增字段/sidecar；不要改旧 JSON 使旧读者失败。

**本包不做：** 不存不必要的原始邮件/个人数据；不宣称此字段能消灭幻觉。

---

### R14 · 明确每通话运行态和音频/告别/hold 负责人

**阶段：** E · 生命周期解耦  
**性质：** 高风险结构；建议按 runtime 与 closing 拆 PR  
**依赖：** R02, R06, R10  
**涉及：** `app/call_state.py`、`app/call_activity.py`、`app/call_audio.py`、`tests/test_voice_closing.py`、`tests/test_hold.py`

**现状与动机：** 大量平行 dict/set 分散创建和清理，持久化状态与进程内状态边界不直观；直接合并成大 CallRuntime 仍可能是另一个 God Object。

**执行步骤**

- [ ] 建立按 call_id 管理的运行态生命周期，内部由各组件私有持有 audio、conversation epoch、closing、hold 等子状态。
- [ ] 先封装访问和 teardown，不改变同步清理的原子性；旧兼容属性仅暂时保留。
- [ ] 将关闭资格计算抽为可测试规则，由 ClosingCoordinator 负责 pending/cancel/commit。
- [ ] 维持 carrier audio 观察在同步快路径；控制层读取其摘要，而不是把音频放进数据库事件总线。

**必须回归**

- [ ] 音频缺帧、陈旧帧、乱序、disconnected、重叠说话、farewell 不匹配、DB claim 等待期间新发言。
- [ ] 持续执行/清理多次通话后 registry 回到基线；保留必要 tombstone 的 TTL/容量语义。

**完成定义：** 每类运行态有唯一所有者和清理规则；三秒连续 carrier silence 和新输入取消语义不变。

**回滚约束：** 先完整保留旧逻辑再转发；不能并行运行两个会挂断电话的 ClosingCoordinator。

**本包不做：** 不把所有 dict 搬进一个无边界的大对象；不把生成结束等同于实际播放结束。

---

### R15 · 统一任务所有权与必须完成的补偿语义

**阶段：** E · 生命周期解耦  
**性质：** 高风险结构  
**依赖：** R02, R14  
**涉及：** `app/call_state.py`、`app/owner_transfer.py`、`app/openai_live.py`、`app/main.py`、`tests/test_lifespan_cleanup.py`

**现状与动机：** 不同路径的 create_task、must_finish、shield、任务集合和 stop 顺序共同承担资源保障。

**执行步骤**

- [ ] 抽出小型 TaskSupervisor，显式标记普通任务、待完成网络操作、补偿任务与服务级监督任务。
- [ ] 先保持既有超时、取消传播和异常聚合语义，再统一可观察的任务退出信息。
- [ ] 为每种任务声明 owner、取消方、关闭时等待方；不让 Finalizer 慢请求阻塞 carrier 挂断。

**必须回归**

- [ ] 任务抛异常、caller 被取消、关闭时再次取消、清理 step 超时、后续清理仍执行。
- [ ] 已登记的外部创建 late completion 能被补偿流程接管。

**完成定义：** 不存在无人观察的关键后台任务；stop 的行为可由契约测试描述。

**回滚约束：** 每类任务逐次切换，保留原 facade；不在同包把全部任务替换为一种 TaskGroup 生命周期。

**本包不做：** 不盲目给全部协程加 shield；不把 must_finish 解读为必须无限等下去。

---

### R16 · 抽出启动/预热/激活流程

**阶段：** E · 生命周期解耦  
**性质：** 高风险结构  
**依赖：** R04, R08, R10, R15  
**涉及：** `app/call_state.py`、`app/twilio_bridge.py`、`app/db/calls.py`、`tests/test_activation.py`

**现状与动机：** setup、provider 回调、monitor 和 activation 目前都经由同一大服务；这部分必须先修准入与取消再提取。

**执行步骤**

- [ ] 建立 SetupCoordinator，负责 SIP 匹配、初始 session 确认、callee create、answer/monitor readiness 与激活。
- [ ] 保留所有 CAS、签名校验后的身份绑定和既有 await 顺序；不使用一把跨网络大锁。
- [ ] CallService 只保留公共入口转发与必要装配。

**必须回归**

- [ ] 未知 SIP、重复 header、绑定竞争、session mismatch、迟到 answer、monitor 失败、重复激活、终止获胜。

**完成定义：** 从计划服务进入 setup 的接口小且清晰；事件入口只调用相关用例。

**回滚约束：** 单实现切换，既有数据库和 provider payload 不变。

**本包不做：** 不在此 PR 优化预热时间；不提前 unmute 或移除 carrier monitor gate。

---

### R17 · 最后拆 termination 与 teardown

**阶段：** E · 生命周期解耦  
**性质：** 最高风险结构  
**依赖：** R14, R15, R16  
**涉及：** `app/call_state.py`、`app/db/termination.py`、`app/owner_transfer.py`、`tests/test_teardown_recovery.py`、`tests/test_voice_closing.py`

**现状与动机：** 终止 claim、transfer ownership、媒体关闭、问题取消、终态写入和 finalization 具有严格并发关系。

**执行步骤**

- [ ] 抽出 TerminationService 和必要的 MediaTeardown 操作，不改变原先谁拥有 claim 的规则。
- [ ] 保持 transfer_outcome 的 expected predicate、claim_guard 与终态完成的 CAS。
- [ ] 将 telephony cleanup、sideband drain、结果提取明确分开；记录“终态已写但远端仍待清理”的情况。

**必须回归**

- [ ] callee exit 重复、owner handoff 与 agent completed 竞争、goodbye claim 期间新输入、清理失败、取消后 retry。
- [ ] carrier hangup 不等待慢 Live usage/extraction；转人工成功不误关 owner-callee conference。

**完成定义：** 任何终止路径都有唯一 teardown owner，且失败后有确定的恢复路径。

**回滚约束：** 逐方法等价提取，避免状态/Schema 变化；未经测试不得合并。

**本包不做：** 不删除看起来多余的 cancellation/ambiguous-write 防护；不顺手统一 termination_reason 文本。

---

### R18 · 恢复流程与转人工的窄协作接口

**阶段：** E · 生命周期解耦  
**性质：** 高风险结构；恢复与 transfer 建议分别提交  
**依赖：** R07, R17  
**涉及：** `app/call_state.py`、`app/owner_transfer.py`、`app/db/transfers.py`、`app/db/termination.py`、`app/db/calls.py`

**现状与动机：** OwnerTransferRun 的 saga 已很细致，但协调器用大量反向回调连接 CallService；恢复逻辑同样依赖这些状态。

**执行步骤**

- [ ] 为 OwnerTransferCoordinator 提供显式的 Telephony、LiveOutput、TaskSupervisor、TerminationPort；移除因 monkeypatch 而存在的晚绑定逻辑。
- [ ] 保留 joining/promoted/completed/terminal 的阶段与补偿顺序，不换工作流引擎。
- [ ] 抽出 RecoveryService，覆盖 nonterminal、terminal missing result、telephony_only、cleanup_pending。
- [ ] 若将安全的历史提取 backlog 从启动阻塞移出，另开行为 PR；危险的远端资源恢复不能因此被跳过。

**必须回归**

- [ ] owner create 后取消、owner 先离开、AI 移除失败、promotion 已提交但返回异常、成功移交后保留 conference。
- [ ] 每个可持久化中间状态的重启恢复；相同恢复执行两次不造成第二次外部创建。

**完成定义：** 不通过整个 CallService 调用相邻组件；恢复结果有可解释的阶段与补偿记录。

**回滚约束：** 保持存储中的 transfer_outcome 编码和时间字段；结构提取可代码回退。

**本包不做：** 不重新实现 saga；不把人工已接管的会话当作孤儿直接挂断。

---

### R19 · 拆分 finalization 的编排、提取与结果政策

**阶段：** F · 独立维护支线  
**性质：** 结构  
**依赖：** R06, R02  
**涉及：** `app/finalizer.py`、`app/models.py`、`app/db/transcripts.py`、`app/prompts.py`、`tests/test_finalizer.py`

**现状与动机：** 一个类同时负责 telephony fallback、API 调用重试、证据校验、结果降级、用量与通知。

**执行步骤**

- [ ] 分成 ExtractorClient、纯 ResultPolicy、FinalizationService，先保持输出完全相同。
- [ ] 保留提取前先保存 telephony_only checkpoint、真实 transcript turn_id 校验与失败回退。
- [ ] 将 owner summary 格式与推送作为外围工作，不能改变原始结果是否可获取。

**必须回归**

- [ ] 空 transcript、未知 evidence ID、可重试 API 错误、提取失败、空 objective checklist、部分要求未满足。
- [ ] 重复 get_result/finalize 不重复生成不必要的结果；终话状态不被当作任务完成。

**完成定义：** 结果政策可完全离线测试；替换提取 fake 无需构造整个通话服务。

**回滚约束：** 原有结果 JSON 保持，纯代码回退。

**本包不做：** 不改 outcome 定义；不将 extraction succeeded 等同于 objective completed。

---

### R20 · 结果证据、费用版本和锁生命周期加固

**阶段：** F · 独立维护支线  
**性质：** 三个独立行为 PR：R20a / R20b / R20c  
**依赖：** R07, R19  
**涉及：** `app/finalizer.py`、`app/models.py`、`app/costs.py`、`app/db/transcripts.py`、`app/db/calls.py`

**现状与动机：** objective_assessments 被使用却未进入最终结果；历史费用用当前 Settings 估算；Finalizer keyed locks 未清理。

**执行步骤**

- [ ] R20a：兼容地保存 objective assessments、证据 ID、提取版本与完整性状态，优先 sidecar，避免旧 StoredCallResult extra=forbid 读失败。
- [ ] R20b：为新通话保存 pricing version / rate snapshot；旧通话标注 historical estimate，不伪造过去价格；失败提取用量不可得时标为不完整。
- [ ] R20c：安全管理 keyed lock 生命周期，考虑等待者引用计数与并发 acquire，不能 finalize 结束就直接 pop 锁。

**必须回归**

- [ ] 上一版本读取新库；证据能回到原 transcript turn；缺失历史证据保持未知。
- [ ] 修改 Settings 不应改变已快照的新通话历史估计；计费未确认明确标注。
- [ ] 大量合法/非法 call_id 的查询后锁表不会无限增长；同 key 并发仍互斥。

**完成定义：** 结果解释可追踪、费用估算可重现、锁不会因历史通话数持续增长；三类变化各自可回退。

**回滚约束：** 仅对新记录启用兼容写入；R20a/b 禁止修改旧 JSON 直到读者兼容；R20c 可单独代码回退。

**本包不做：** 不声称费用等于账单；不无条件移除仍有等待者的锁。

---

### R21 · OAuth：先隔离阻塞校验，再整理持久化事务

**阶段：** F · 独立维护支线  
**性质：** 独立支线；R21a 行为修复，R21b 结构，R21c 事务修复  
**依赖：** R02, R07  
**涉及：** `app/routes/mcp_oauth.py`、`app/mcp_oauth/provider.py`、`app/mcp_oauth/crypto.py`、`app/db/oauth.py`、`tests/test_mcp_oauth.py`

**现状与动机：** async consent route 同步运行 Argon2 校验；provider 同时负责框架适配、token lifecycle 与分散持久化步骤。DB OAuth 模块本次未逐段精读，事务修复须先扩展审查。

**执行步骤**

- [ ] R21a：将 owner secret 校验放到有并发上限的 off-loop 执行器，保留全局/来源失败限流；取消不会终止线程的事实须纳入容量管理。
- [ ] R21b：抽出 OAuthStore 接口与框架适配边界，先保持 token 语义与加密格式。
- [ ] R21c：逐项检查 authorization-code consume+issue、refresh rotate、family revoke 的原子边界，补故障测试后独立修复，不能先断言存在鉴权绕过。

**必须回归**

- [ ] 并发登录不阻塞模拟音频快路径；资源消耗受限；无明文 secret 日志。
- [ ] code 单次使用、refresh reuse 整 family 撤销、resource/audience/scope、密钥轮换、旧密文读取。

**完成定义：** 认证重工作不占用 event loop；存储失败的结果可预测；不改变既有授权契约。

**回滚约束：** 单独回退 off-loop 或 store adapter；禁止改 KDF 标签、salt 与已有密文格式。

**本包不做：** 不引入新的身份平台；不把注册客户端显示名当成可信身份。

---

### R22 · 配置、CLI 与安装包的一致性

**阶段：** F · 独立维护支线  
**性质：** 结构为主；行为变化独立提交  
**依赖：** R02, R06  
**涉及：** `app/settings.py`、`app/doctor.py`、`app/cli.py`、`app/setup.py`、`app/local_start.py`、`app/tunnel.py`、`pyproject.toml`、`scripts/agent_call_console.py`

**现状与动机：** 环境变量/ dotenv / cwd/显式目录逻辑有多条路径；源码可运行不等于安装包可运行。

**执行步骤**

- [ ] 统一 ConfigSource 与实例目录解析，保持已发布环境变量名称和优先级；配置加载、验证、连接探测分离。
- [ ] Settings 对外继续兼容，内部给每个子系统传入最小配置视图；不要一次重命名全部 env key。
- [ ] 保留 managed start 的私有目录、凭据隔离、固定 cloudflared 哈希和停止前 idle gate。
- [ ] 在干净环境、仓库目录之外安装构建 wheel，检查真正的 console entry point；整理包版本/服务版本和迁移后的描述。
- [ ] Python 3.12/3.13 加矩阵；安装、help、dummy/evaluation、prepare-only smoke 不拨号、不自动调用 managed start 下载隧道。

**必须回归**

- [ ] 环境变量覆盖、.env/.env.local、显式 from_environ、多个实例目录、symlink/权限失败、信号关闭和隧道退出。
- [ ] wheel 安装后从非源码 cwd 运行；故障提示不泄露秘密。

**完成定义：** 安装路径、源码路径和容器路径的配置契约一致且有独立验证；平台宣称与测试覆盖相符。

**回滚约束：** 保持旧 CLI 参数和 env 名称；配置文件格式变化必须显式迁移而非静默重写。

**本包不做：** 不轻率删 httpx/httpx2 或 console shim；不更改默认 live/evaluation 行为而不单独公告。

---

### R23 · 部署租约、文档与兼容层退出

**阶段：** G · 发布收口  
**性质：** 发布加固 + 文档；租约修复可提前独立执行  
**依赖：** R00, R01  
**涉及：** `.github/workflows/fly-deploy.yml`、`app/db/deployment.py`、`app/local_start.py`、`app/routes/openai_webhooks.py`、`README.md`、`docs/architecture.md`、`docs/self-hosting.md`、`tests/test_fork_safety.py`

**现状与动机：** 部署租约 TTL 为 15 分钟，获取后才 remote build/deploy；超时期间的新拨号保护需要验证。文档对一次一通、webhook 顺序等需要与实现收敛。

**执行步骤**

- [ ] R23a：先构建/验证不可变 artifact，再尽量缩短 drain→lease→promote 窗口；必要时增加带 owner 的续租和安全释放，构建失败不长期锁服务。
- [ ] R23b：明确签名验证、schema 验证、receipt dedup、业务 claim 和外部副作用的顺序；当前 receipt-before-processing 语义须故障测试后单独决定，不能随便移到 handler 后。
- [ ] R23c：全部结构工作完成后更新架构、运行手册、恢复手册、契约与评估指引；维护者生产地址不得泄入 fork 默认配置。
- [ ] 只有引用和兼容性测试证明不再需要时，才删除 CallService 兼容属性或旧 import re-export；历史数据和 crypto 标签不属于可删除 shim。

**必须回归**

- [ ] 发布超过 lease TTL、续租失败、错误 owner release、进程重启、部署后健康校验失败、活跃通话拒绝部署。
- [ ] 已签名重复 webhook 和中途处理失败重试都不得造成重复付费动作。
- [ ] 新/旧数据库读取、prepare-only 安装烟测和关键通话竞态套件通过。

**完成定义：** 发布、回滚、恢复步骤有可验证结果；生产保护覆盖构建/切换窗口；文档不再承诺代码未强制的行为。

**回滚约束：** 租约 schema 采用 reader-first 或兼容新增；不删除上一稳定 artifact；数据兼容先于 image rollback。

**本包不做：** 不以移除部署锁修复发布慢；不在 shadow 模式重复执行真实 provider 副作用。

---

## 8. 测试与发布门槛

### 8.1 现有常规命令

以下命令来自仓库验证配置，是实施时需要执行的命令；本次未执行。使用无生产配置、无生产数据库的隔离环境。

```bash
uv sync --all-groups --frozen
uv run ruff format --check app tests scripts
uv run ruff check app tests scripts
uv run mypy app
uv run pytest -q --cov=app
```

不得为了通过检查删除测试、降低已有 85% 配置门槛，或将新代码批量改为 Any / type: ignore。覆盖率总数并不替代并发场景证明；不要求用无价值测试机械追求 100%。

### 8.2 新增隔离安装验证

构建 wheel 后，在源码目录之外、干净 Python 3.12 和 3.13 环境安装。验证 `agent-call --help`、dummy doctor、loopback evaluation 服务以及 prepare-only smoke。配置必须显式使用假凭据/测试实例和临时数据库。

不要在该测试中调用 `agent-call start`：它可能下载并启动真实隧道。不要把 `doctor --live-ready` 当离线测试；它会进行实际 provider readiness 探测。不要启动任意 live-phone run 或真实 canary。

### 8.3 核心风险矩阵

| 风险 | 必要场景 | 观察结果 |
|---|---|---|
| 双启动 | 同 plan / 不同 plan / deployment lease 竞争 | 准入数、远端 create 次数、DB claim |
| 取消创建 | 远端执行中 / 已成功未返回 / DB 写前后 | SID 归属、补偿、pending cleanup |
| 媒体激活 | session mismatch / monitor 不可用 / duplicate callback | 不越过 gate、不重复 enable |
| 请求过期 | 新发言和旧 tool result 交错 | 不执行旧副作用 |
| 工具交付 | 输出失败重试与 response completed 乱序 | 不重复执行；只允许正确 continuation |
| 问题 | answer / expire / terminate 三方竞争 | 单一最终状态、无过期注入 |
| 告别 | gap/stale/disconnect/重叠发言/SQL 等待中回复 | 不提前挂断，保留回复窗口 |
| 转接 | owner late join / depart / ambiguous promotion / AI remove fail | ownership 和 compensation 正确 |
| 恢复 | 每个持久化中间态 / terminal missing result / cleanup_pending | 不再创建新外部通话、清理可继续 |
| 持久化 | old DB / old JSON / new writer + old reader / old ciphertext | 可读或明确阻止不安全回滚 |
| 监督故障 | watchdog 异常 / 清理超时 / task 崩溃 | 不静默失去保护、不再接受新风险 |
| 发布 | build 超过 TTL / renew fail / wrong owner release | 保护不失效、可恢复 |

### 8.4 性能验收

先测基线，再定阈值。建议观察接通至首句、callee 语音结束至实际接收音频、打断后残留语音、工具结果至继续发言、carrier 挂断耗时、恢复积压处理时间、有界队列深度和内存回收。

既有三秒回复窗口不能被当作“延迟”削减。Live transcript 或音频生成完成不能替代 carrier playback；carrier monitor 也不能宣称物理听筒已收到。真实接收端测量复用现有 live-phone harness，但必须获得针对测试号码、场景和预算的明确授权后才执行。

没有实测之前，不承诺重构能让延迟减少某个百分比。

## 9. Webhook 的专项决定记录

现有 OpenAI webhook 在签名验证后先记 webhook-id，再进入事件类型/业务处理，并拒绝重复。这个设计保护重复执行，但会影响处理失败后的重试。

实施时应分别描述：认证成功、schema 合法、receipt 记录、业务 claim、provider 副作用、结果持久化。对“已收到但未处理完”提供可恢复状态，或者明确接受当前 fail-closed 策略并记录限制。不能直接将去重改成 handler 执行后，也不能仅将重复统一回 200 就声称问题解决。

若无队列需求，不必引入 Kafka/Redis。一个有明确状态的 SQLite journal 可能足够，但是否需要新增表必须由故障测试与产品重试需求决定。

## 10. 回滚规则

**结构 PR：** schema 与 wire format 不变；保留 facade/re-export 期间可以代码回退。

**数据 PR：** 先 expand，再 reader-first，再 writer，最后另一个版本 contract。旧结果模型 extra=forbid 时，单纯“增加字段”并不保证回滚。sidecar 证据数据可以先保留而不改变旧 result_json。

**副作用 PR：** 不能以双实现同时执行真实 create、transfer、DTMF 或 hangup 来比较新旧行为。Shadow comparison 只允许纯决策、离线事件重放或完全 fake provider。

**生产发布：** 先保存稳定 artifact 标识和数据兼容结论，再 drain/lease/promote；迁移或清理不确定时不能盲目回旧镜像。不要在有人通话时采用破坏性回滚。受控测试场景也需要独立授权，不由“用户要求重构计划”自动授权。

## 11. 给 coding agent 的单 PR 工作说明模板

```text
任务：仅实现工作包 Rxx 的指定切片。
基线：先确认当前 main 与审查 SHA 的差异，不盲目覆盖此后修改。
目标：<本 PR 要建立的边界或修复的具体行为>
可改路径：<明确目录与文件>
禁止事项：不部署、不拨真实电话、不读取/输出秘密、不修改模型和 prompt；
          不顺手改 unrelated code，不降低测试门槛。
步骤：
1. 阅读目标代码、相关调用方和既有竞态测试。
2. 对行为修复先增加失败复现；对结构提取先冻结行为快照。
3. 实现最小切片，保留原公开入口。
4. 运行对应测试和完整验证；未执行的命令必须明确列为未执行。
5. 提交说明覆盖：变更前后、事务与 await 顺序、资源所有权、兼容性、回滚。
停止条件：出现无法证明等价的并发语义、实际 base 已变更或回滚格式不兼容。
输出：diff 摘要、实际测试输出、未覆盖风险；不自动 merge。
```

同一时段只让一个 agent 修改 `call_state.py` / 核心生命周期。OAuth、CLI 和 finalizer 可以在接口冻结后并行开发，但每条支线独立分支、独立审查，不并行改共享 schema。

## 12. 不在本轮范围内的工程

不迁移微服务；不加入 Redis/Celery/Kafka；不使用工作流引擎替换已实现的 owner-transfer saga；不迁移 PostgreSQL/ORM；不做多租户/SaaS 计费；不抽象任意电话/模型 provider；不为了“更整洁”重写音频路径；不以文件行数作为主要验收指标。

未来 managed service 或多实例运行确实可能需要新的持久化和调度设计，但那是独立产品/架构项目，不应该偷偷混进本次行为保持型重构。

## 13. 第一批实际实施建议

首先落地 R00、R01、R02。然后分别用可控故障测试推进 R03、R04、R05。完成后已经可以停止一轮，确认发布与回归稳定，再进入 R06 之后的结构拆分。

最重要的成功标准是：**改一个功能需要理解的范围明显变小，而既有电话安全、实时性、恢复能力和存储兼容性没有变弱。**
