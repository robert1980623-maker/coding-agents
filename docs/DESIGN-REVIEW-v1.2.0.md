# Coding Agent Runtime 设计文档审查报告：v1.0.0 → v1.2.0

> **评审人**: Claude Code (架构师视角)，由 Amenda (PM) 协调  
> **日期**: 2026-06-20  
> **评审对象**: DESIGN.md v1.2.0 (Draft, Revised — P0/P1 修复)  
> **对比基线**: DESIGN.md v1.0.0  
> **关联文档**:
> - [`DESIGN-REVIEW.md`](./DESIGN-REVIEW.md) — Atlas 对 v1.0.0 评审（4/5）
> - [`DESIGN-REVIEW-CLAUDE.md`](./DESIGN-REVIEW-CLAUDE.md) — Claude 对 v1.0.0 评审（6.5/10）
> - 本文档 — Claude 对 v1.2.0 深度对比审查

---

## 📊 总体结论

| 维度 | 结论 |
|------|------|
| **方向** | ✅ 正确 |
| **P0 修复质量** | 3 项完全修复 + 5 项部分修复 |
| **v1.2.0 定稿** | ⚠️ **不建议立即定稿** |
| **进入 Phase 1** | ⚠️ **需补 5 项阻塞后进入** |

### 三份评审定位

| 评审 | 对象 | 评分 | 视角 |
|------|------|------|------|
| Atlas | v1.0.0 | 4/5 ★★★★☆ | 架构合理性，正面 |
| Claude（v1） | v1.0.0 | 6.5/10 | 实施可行性，严格 |
| Claude（本文） | v1.2.0 | ⚠️ 需补 5 项 | v1 → v1.2 修复验证，新增问题识别 |

---

## 1. P0 修复验证表

| 编号 | 修复项 | 判定 | 详细说明 |
|------|--------|------|---------|
| **P0-1** | stderr seq 全局 monotonic counter | **⚠️ 部分修复** | 代码实现正确：`SeqCounter` 用 `asyncio.Lock` 保证原子递增，stdout/stderr 共享同一个 `_seq` 实例，**无死锁风险**（asyncio 单线程，无循环依赖；锁持有时间仅一次 `int+1`，纳秒级）。但有两个问题：① 命名为"全局"有误导——`SeqCounter` 是 `StreamExecutor` 的实例变量，实际是 **per-session 单调递增**，并非跨 session 全局；② 性能无问题（100k 行/秒级别锁竞争在 asyncio 模型下可忽略）。 |
| **P0-2** | tags 拆 session_tags 关联表 | **⚠️ 部分修复** | Schema 正确：`session_tags(session_id, tag)` 复合主键 + CASCADE 删除 + tag 索引，消除了 v1.0.0 JSON 字段无法高效查询的问题。**但 StorageBackend Protocol 未定义任何 tag 管理 API**——没有 `add_tag()`/`remove_tag()`/`list_tags()`/`filter_by_tag()`。v1.0.0 有 `idx_sessions_tags` 索引用于过滤，v1.2.0 连 `list_sessions` 的 `tags` 过滤参数都丢了。**表建好了，没有门可以进出。** |
| **P0-3** | 崩溃恢复明确边界 | **⚠️ 基本修复，有小问题** | 边界清晰：只做状态恢复（`RUNNING → ORPHANED`），不做执行续跑。SQL 正确。`timeout_seconds=300` 合理（heartbeat 在每行 stdout 时更新，5 分钟无输出确实意味着死亡）。**问题**：① `finished_at` 被设为恢复执行时的时间而非实际死亡时间，`duration_ms` 会虚长；② ORPHANED 是终态（无 `ORPHANED → RUNNING` 转换），被标记的 session **不可恢复**，只能事后审计；③ `recover_orphaned_sessions` 放在 `StorageBackend` Protocol 里不合适——这是编排逻辑，不是存储操作。 |
| **P0-4** | SSE + Last-Event-ID 断点续传 | **❌ 仅声明，未实现** | API 端点 `/events/stream` 定义了 `Last-Event-ID` header 和 `?after_seq=100` query param。SSE 事件格式正确（`id: seq`）。**但**：① 没有任何 Python 代码实现 SSE 端点；② `Last-Event-ID` 的解析逻辑不存在；③ 无边界处理（ID 超出范围？ID 为负？session 已结束？重连后何时关闭连接？）；④ SSE 连接空闲时的 ping/keepalive 未提及。**整个 HTTP 层都是空中楼阁。** |
| **P0-5** | 默认 127.0.0.1 + Bearer token | **⚠️ 方向正确，细节缺失** | CLI 默认 `--host 127.0.0.1` ✅。API 文档标注 `Auth: Bearer token` ✅。**但**：① Token 生成方式未指定（UUID？JWT？随机字符串？）；② 存储方式未指定（明文？hash？配置文件？环境变量？）；③ 无轮转机制；④ **`--auth-token <token>` 通过命令行传入，出现在 `ps aux` 和 shell history 中**——安全风险；⑤ 无 HTTPS/TLS 方案，Bearer token 在 `0.0.0.0` 部署时明文传输；⑥ 本地模式（127.0.0.1）是否强制认证？文档暗示可选，但未明确。 |
| **P0-6** | append_events 返回 None | **✅ 已修复** | Protocol 签名改为 `async def append_events(self, events: list[Event]) -> None`。消除了 v1.0.0 返回假 ID（`range(1, len(events) + 1)`，硬编码且完全错误）的问题。 |
| **P0-7** | ExecutionConfig 补 model | **✅ 已修复** | `model: Optional[str] = None` 加入 `ExecutionConfig`。`ClaudeAgent.build_command` 和 `CodexAgent.build_command` 都正确处理了 `--model` / `-m` 参数。 |
| **P0-8** | 先 write 再 yield | **✅ 已修复，有对账隐患** | 代码流程正确：`buffer.append(event)` → `await _flush_if_needed()` → `yield event`。Storage 是 durable 真相。**对账**：SSE 用 `seq` 作为 event ID + `Last-Event-ID` 重连，理论上客户端断线后可从 storage 补回。**但无显式日志/指标记录"客户端错过事件"的情况**。建议：在 yield 失败时记 warning 日志（`logger.warning(f"Client missed event seq={event.seq}")`）。 |

---

## 2. 修复引入的新问题（P1_new）

| 编号 | 问题 | 严重性 | 说明 |
|------|------|--------|------|
| **P1_new-1** | SessionStartEvent 缺失 | 高 | v1.0.0 统一事件格式定义了 `session.start` 事件。v1.2.0 文档 §3.4 仍列出该事件类型，但 **Executor 代码从未 yield session.start**。SSE 客户端永远收不到"会话开始"信号。需要在 `execute()` 开头、`yield` 第一个 stdout 前 yield 一个 `session.start` 事件。 |
| **P1_new-2** | Heartbeat 写风暴 | 高 | `last_heartbeat_at` 在 **每行 stdout** 输出时都执行 `await self.store.update_session(session_id, last_heartbeat_at=datetime.now())`。若 agent 每秒输出 100 行，则每秒 100 次 SQLite UPDATE（在 WAL 模式下每次仍需获取写锁）。**应节流**：记录 `last_heartbeat_write_time`，最多每 1-5 秒写一次。 |
| **P1_new-3** | Idle timeout 无实现 | 中 | `ExecutionConfig.idle_timeout_seconds = 300` 存在，但 **没有任何代码检测空闲超时**。v1.0.0 也没有。如果 subprocess hang 住不输出，进程将永远运行直到 `timeout_seconds`。需要一个 watchdog task 定期检查 `now - last_heartbeat_at > idle_timeout_seconds` 然后 kill。 |
| **P1_new-4** | Tags 管理 API 缺失 | 中 | P0-2 建了表但没有 CRUD API。Storage Protocol 无 tag 相关方法。`list_sessions` 无 tag 过滤参数。SDK/CLI/HTTP 均无 tag 操作接口。**功能不可用。** |
| **P1_new-5** | 多 Agent 编排能力归零 | 中 | §1.3 使用场景仍列出"多 Agent 编排 — 并行执行多个 agent，统一监控"。但 Orchestrator 和 Event Bus 已被 v1.1.0 完全删除。**没有替代方案**。如果用户需要"一个任务拆成 3 个 agent 并行跑然后合并结果"，v1.2.0 无法支持。建议：要么从使用场景中删除该条目，要么在 Phase 2/3 明确补充编排机制。 |
| **P1_new-6** | 背压控制有名无实 | 中 | §2.2 声称"背压控制：buffer 超过阈值时暂停读取"。代码中只有 `_flush_if_needed`（100 条/100ms 刷盘），**没有暂停读取逻辑**。如果 storage 写入慢（如磁盘 IO 瓶颈），buffer 会无限增长。真正的背压需要：当 `len(buffer) > threshold` 时暂停 `async for line in proc.stdout`（即停止读取 stdout pipe），让 pipe buffer 背压传导到 subprocess。 |
| **P1_new-7** | stderr 事件消费者不可见 | 中 | `_drain_stderr` 将 stderr 事件写入 buffer/storage，**但从不 yield**。SSE 客户端和 SDK 消费者 **永远收不到 stderr 事件**。v1.0.0 也有此问题（stderr 直接写 storage 不经过 yield）。但 v1.2.0 在 §3.4 统一事件格式中明确列出了 stderr 事件类型，暗示应该可见。**需要决定**：stderr 是否应通过 SSE 推送？如果是，需要 yield；如果不需要，从事件格式文档中删除。 |
| **P1_new-8** | WatchPattern 被静默删除 | 低 | v1.0.0 有 `WatchPattern` dataclass + `ExecutionConfig.watch_patterns: list[WatchPattern]`，支持 notify/callback/stop 三种 action。v1.2.0 将 `watch_patterns` 简化为 `list[str]`（纯字符串），**丢失了 action/callback 定义**。Executor 中 `_check_watch_patterns` 只剩 `pass`。**监控功能完全失效。** |

---

## 3. v1.1.0 变更评估

### 3.1 五层 → 三层架构

| 维度 | 评价 |
|------|------|
| **正确性** | ✅ 正确。v1.0.0 的 Orchestrator（Session Manager + Agent Router + Event Bus）对于"一个请求对应一个 subprocess"的模型确实过度。Executor 直接管理 subprocess 生命周期足够。 |
| **完整性** | ⚠️ 不完整。砍掉 Orchestrator 后，**没有东西承担跨 session 的协调工作**：① 全局并发限制（v1.0.0 有进程池+队列，v1.2.0 只在 §7.3 文字建议"≤5"但无实现）；② 跨 session 事件路由；③ 多 agent DAG 编排。 |
| **可演进性** | ⚠️ 当前够用，Phase 3 会撞墙。建议在 Executor 和 Interface 之间预留一个轻量的 `SessionRegistry`（管理活跃 session 的 dict + 并发信号量），成本很低但为未来编排留口子。 |

### 3.2 双模式输出（passthrough / standard）

| 维度 | 评价 |
|------|------|
| **设计合理性** | ✅ 合理。不同消费者有不同需求：监控用 standard（提取文本+成本），深度分析用 passthrough（保留原始 JSON）。 |
| **存储** | ⚠️ **存在冗余**。passthrough 模式下，`data` 字段存原始 JSON 字符串，`raw_json` 字段 **也存原始 JSON 字符串**——同一条数据存两份。建议：passthrough 模式下 `data` 存提取的纯文本（或空），`raw_json` 存完整原始 JSON；或者 passthrough 模式下 `raw_json` 是唯一数据字段，`data` 为空。 |
| **标准化模式解析** | ⚠️ 未指定解析细节。`data` 字段"提取文本内容"——提取什么？Claude Code 的 `assistant.message.content[0].text`？还是整行 JSON？Agent adapter 的 `parse_output` 返回 dict 但 execute() 中 **没有调用它来填充 `data`**——`data=line.decode()` 直接存原始行。标准化模式的"标准化"逻辑不存在。 |

### 3.3 memorix 关系声明

| 维度 | 评价 |
|------|------|
| **清晰度** | ✅ 清晰。§2.4 明确"借鉴设计模式，不复用代码"，并列出对比表和具体借鉴项（Protocol 存储接口、SQLite PRAGMA、批量写入、迁移系统）。 |
| **边界** | ✅ 合理。定位区分明确：memorix = 记忆层（Observation + Topic），coding-agents = 执行层（Session + Event）。Embedding 从核心功能降级为可选插件。 |
| **风险** | ✅ 无风险。借鉴的都是成熟模式（Protocol、WAL、批量写入），不涉及 memorix 的特有逻辑。 |

---

## 4. 遗漏的 P0

| 编号 | 遗漏项 | 严重性 | 说明 |
|------|--------|--------|------|
| **P0-miss-1** | Idle timeout 无 watchdog | 高 | `idle_timeout_seconds=300` 配置存在，但 **零实现**。subprocess hang 住会永远运行。这是生产必须有的保护。 |
| **P0-miss-2** | 并发限制无实现 | 高 | §7.3 建议"≤5 个 agent 同时执行"，但无进程池/信号量/队列代码。如果 20 个请求同时到达，20 个 subprocess 全部启动，SQLite 写竞争 + 内存爆炸。 |
| **P0-miss-3** | Error 事件未生成 | 中 | `EventType.ERROR` 已定义，但 Executor 从未 yield error 事件。subprocess 启动失败（command not found）、OOM kill 等异常情况无结构化错误事件。 |
| **P0-miss-4** | FTS5 `rebuild` 缺失 | 低 | FTS5 触发器在 INSERT/UPDATE/DELETE 时同步，但如果从旧版本迁移（已有 events 数据），需要 `INSERT INTO events_fts(events_fts) VALUES('rebuild')`。迁移脚本未提及。 |

---

## 5. Phase 1 实现风险

| 风险点 | 卡住程度 | 说明 + 建议 |
|--------|---------|------------|
| **SSE 实现细节空白** | 🔴 高 | Phase 2 计划 4 天完成"HTTP Server (FastAPI + SSE)"含认证和断点续传。但 SSE 的协议细节（重连策略、心跳、缓冲区管理、Last-Event-ID 边界处理）完全没设计。**建议**：Phase 1 结束前写一个 SSE 协议设计文档（200 行足够），明确以上细节。 |
| **认证机制空白** | 🔴 高 | Token 怎么来、怎么存、怎么验证都没说。编码时开发者会卡在"我先随便写一个？还是等 CEO 定？"**建议**：最简方案——token 从环境变量读取（`CODING_AGENT_AUTH_TOKEN`），不轮转，明文比对。一行代码搞定，后续再迭代。 |
| **标准化模式名存实亡** | 🟡 中 | `data=line.decode()` 直接存原始行，没有"标准化"解析。Phase 1 编码时开发者会困惑：我是不是要调用 `parse_output`？parse_output 返回的 dict 怎么映射到 `data` 字段？**建议**：明确 standard 模式的转换逻辑（在 Agent adapter 中实现 `to_standard_event(line) -> str`）。 |
| **Heartbeat 写风暴** | 🟡 中 | 不节流的话，长任务（30min × 100 行/秒 = 180,000 次 UPDATE）性能堪忧。**建议**：在 `execute()` 中加 `if time.time() - last_hb_write > 1.0` 节流。 |
| **WatchPattern 功能死代码** | 🟡 中 | `_check_watch_patterns` 是空函数。如果 Phase 1 不实现，应删除或标记 `# TODO`，否则开发者会困惑。 |
| **背压控制无实现** | 🟢 低 | 当前 `_flush_if_needed` 足够应对正常负载。极端场景（storage 极慢）才会触发问题。可以 Phase 1 不实现，但应删除"背压控制"的宣传文字，避免误导。 |

---

## 6. v1.2.0 定稿结论

### ⚠️ 需补充以下内容后方可进入 Phase 1

v1.2.0 在 **方向上正确**——8 项 P0 中 3 项完全修复（P0-6/7/8）、5 项部分修复（P0-1/2/3/4/5）。三层架构简化合理，memorix 关系声明清晰。

但存在 **3 个阻塞 Phase 1 编码的空白** + **4 个遗漏的生产必要功能**：

#### 进入 Phase 1 前必须补充（阻塞项）

1. **Idle timeout watchdog 设计**（30 行伪代码即可）
2. **并发限制机制**（至少一个 `asyncio.Semaphore(5)` 的设计说明）
3. **SSE 协议细节**（重连策略、Last-Event-ID 边界、心跳、超时）
4. **认证最简实现**（环境变量 token，明文比对）
5. **SessionStartEvent yield**（一行代码 + 一个事件定义）

#### 进入 Phase 1 前建议修正（非阻塞但影响质量）

6. **Heartbeat 节流**（避免写风暴）
7. **Tags 管理 API**（至少 `add_tag`/`remove_tag`/`filter_by_tag` 加入 Protocol）
8. **stderr 可见性决策**（yield or not yield — 明确写进文档）
9. **WatchPattern 要么实现要么删除**（不要留死代码）
10. **双模式存储冗余**（passthrough 模式下 data 与 raw_json 不应存相同内容）
11. **多 Agent 编排**——从使用场景中删除或标注"Phase 3+"

#### 补充工作量估计

以上阻塞项约需 **1-2 天设计补充**（不改架构，只补细节到文档）。建议产出 v1.2.1 修订版后再启动 Phase 1 编码。
