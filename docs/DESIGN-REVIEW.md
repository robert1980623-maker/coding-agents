# Coding Agent Runtime — 设计评审

> **评审人**: Atlas (Chief Architect)  
> **日期**: 2026-06-20  
> **评审对象**: DESIGN.md v1.0.0

---

## 总体评价

**评分: ★★★★☆ (4/5)**

设计质量很高，分层清晰，流式优先的理念正确解决了我们实际遇到的 buffer 限制问题。SQLite WAL + 批量写入的组合在单机场景下是合理选择。以下是具体问题和建议。

---

## 🔴 P0 — 必须修复

### 1. stderr 序号方案有碰撞风险

**位置**: §4.1 Executor, `_drain_stderr`

```python
seq=1000000 + seq  # stderr 用大序号避免冲突
```

**问题**:
- 硬编码 1M 偏移，如果 stdout 事件超过 1M 条（30min 高频输出完全可能），序号碰撞
- stdout/stderr 序号空间分离，无法还原真实的交错时序
- UNIQUE(session_id, seq) 约束会在碰撞时直接报错

**建议**:
使用全局单调递增 seq，通过一个 `asyncio.Lock` 或 atomic counter 保证 stdout/stderr 写入的序号唯一：

```python
class SeqCounter:
    def __init__(self):
        self._value = 0
        self._lock = asyncio.Lock()
    
    async def next(self) -> int:
        async with self._lock:
            self._value += 1
            return self._value
```

或者更简单：stderr 事件不写 events 表，而是写入 session 的 metadata 字段（stderr 通常量小且不需要独立查询）。

### 2. `append_events` 返回值错误

**位置**: §4.3 Storage, `SQLiteStorage.append_events`

```python
return list(range(1, len(events) + 1))  # ← 这不是真实 DB ID
```

**问题**:
- 返回假 ID，调用方如果依赖这个 ID 做后续操作会出 bug
- `executemany` 不返回插入的 rowid

**建议**:
- 方案 A：返回 `cursor.lastrowid` 范围（需要改用逐条插入或 `executemany` 后查 `lastrowid`）
- 方案 B：改为返回 `None`，明确告知调用方批量写入不返回单条 ID
- 方案 C：用 `INSERT ... RETURNING id` (SQLite 3.35+)

推荐方案 B，因为调用方实际上不需要这些 ID。

### 3. `ExecutionConfig` 缺少 `model` 字段

**位置**: §3.1 数据模型 vs §4.2 Agent

`ClaudeAgent.build_command` 引用了 `config.model`，但 `ExecutionConfig` dataclass 中没有定义这个字段。`Session` 有 `model` 字段但 `ExecutionConfig` 没有。

**建议**: 在 `ExecutionConfig` 中添加：

```python
@dataclass
class ExecutionConfig:
    # ... 现有字段 ...
    model: Optional[str] = None
    agent_type: Optional[str] = None  # 用于路由
```

### 4. 批量写入与实时 yield 的一致性窗口

**位置**: §4.1 Executor

```python
# 实时 yield
yield event

# 批量 flush（可能还没发生）
if len(buffer) >= 100 or time.time() - last_flush >= 0.1:
    await self.store.append_events(buffer)
```

**问题**:
- 事件先 yield 给消费者，后写入存储
- 如果消费者根据事件做了操作（如触发 webhook），但此时 runtime 崩溃，存储中没有这些事件
- 消费者和存储之间的事件可见性不一致

**建议**:
- 明确文档化：**yield 是 best-effort 实时，storage 是 durable 真相**
- 消费者如果依赖事件持久化，应通过 `stream_events` 从 storage 读取
- 或者改为先 write 再 yield（牺牲少量延迟换一致性）

---

## 🟡 P1 — 强烈建议

### 5. 缺少背压（backpressure）机制

**问题**: 如果 storage 写入慢（比如磁盘 IO 抖动），executor 会持续往 buffer 里塞事件，内存不受控。

**建议**:
```python
# 当 buffer 超过阈值时，暂停读取 stdout
if len(buffer) >= MAX_BUFFER_SIZE:
    await self.store.append_events(buffer)
    buffer.clear()
```

或者用 `asyncio.Queue(maxsize=10000)` 做背压。

### 6. 进程崩溃后 session 状态悬挂

**问题**: 如果 runtime 进程本身崩溃（不是子进程），running 状态的 session 永远不会变成 failed/timeout。

**建议**:
- 启动时扫描 `status=running` 的 session，标记为 `orphaned`
- 或者用 heartbeat 机制：定期更新 `updated_at`，超过阈值自动标记为 `dead`

```sql
-- 启动时恢复
UPDATE sessions SET status = 'orphaned', exit_code = -1
WHERE status = 'running' AND updated_at < strftime('%s', 'now') - 300;
```

### 7. HTTP SSE 缺少 resume 机制

**位置**: §5.2 HTTP Server

```
GET /api/v1/sessions/:id/events?after_seq=100&stream=true
```

**问题**: SSE 断连后，客户端需要知道从哪个 seq 恢复。当前设计没有提供 `Last-Event-ID` 或类似机制。

**建议**:
- SSE 每个 event 带 `id` 字段（用 seq）
- 支持 `Last-Event-ID` header 自动恢复
- 或者在 query 里明确 `after_seq`

### 8. 安全考虑缺失

**问题**: 文档没有讨论安全模型。

**需要考虑的**:
- `workdir` 是否需要路径校验？（防止 `../../etc` 之类的）
- `prompt` 注入风险 — agent CLI 的参数注入
- HTTP API 的认证/授权
- 资源限制（CPU、网络）— 目前只有 memory 和 budget

**建议**: 增加 §9.5 安全设计，至少覆盖：
- workdir 白名单/规范化
- prompt 参数转义
- HTTP API token 认证
- 默认资源上限

### 9. 进程池设计只有占位

**位置**: §6.3 并发控制

"进程池：限制并发 agent 数量（默认 5）" — 但没有设计细节。

**建议**: 补充：
- 队列策略（FIFO？优先级？）
- 排队超时
- 进程复用 vs 每次新建
- 与 `max_concurrent` 的关系

---

## 🟢 P2 — 建议改进

### 10. 事件类型可以更丰富

当前 `EventType` 只有 stdout/stderr/system/result/watch/error。

建议增加：
- `TOOL_CALL` — agent 调用了什么工具（Claude Code 的 stream-json 里有）
- `PROGRESS` — 进度信息（文件修改、测试执行等）
- `COST_UPDATE` — 实时成本更新（不只在 session.end 时）

### 11. 性能指标可能过于乐观

| 指标 | 设计目标 | 疑虑 |
|------|---------|------|
| 100 万事件 <1s 查询 | 需要验证 | SQLite 在 100 万行时的实际性能取决于查询模式和索引 |
| 语义搜索 <500ms | 依赖 embedder | 如果是本地 LM Studio，首次加载模型可能需要数秒 |
| 10 并发 <200MB | 取决于输出量 | 每条 stdout 如果很长（如 cat 大文件），8MiB limit × 10 = 80MB 仅 buffer |

**建议**: Phase 1 结束后做一次基准测试，验证这些指标。

### 12. Node.js SDK 的实现方式未明确

**问题**: Node.js SDK 是独立实现还是 FFI/HTTP 调用 Python 核心？

**建议**: 明确选择：
- **方案 A**: HTTP-only client（最简，推荐先做）
- **方案 B**: 独立 Python 实现（通过 subprocess 调用 Python core）
- **方案 C**: 纯 Node.js 重写（工作量大，但性能好）

推荐 Phase 4 先做方案 A，后续根据需求决定是否独立实现。

### 13. 缺少测试策略

**建议**: 增加一节：
- 单元测试：每个组件独立测试
- 集成测试：executor + storage 端到端
- 压力测试：10 并发 × 30min 长任务
- 混沌测试：随机 kill 子进程，验证数据完整性

### 14. Watch Pattern 的匹配效率

```python
async def _check_watch_patterns(self, session_id: str, event: Event):
    for pattern in self.config.watch_patterns:
        if pattern.pattern in event.data:
```

**问题**: 每行输出都遍历所有 pattern，O(n×m)。如果 pattern 多或输出快，可能成为瓶颈。

**建议**: 用 `re.compile` 预编译 + Aho-Corasick 多模式匹配（pattern 多时）。

---

## 📋 总结

| 级别 | 数量 | 关键项 |
|------|------|--------|
| 🔴 P0 | 4 | stderr 序号碰撞、假 ID、缺字段、一致性窗口 |
| 🟡 P1 | 5 | 背压、崩溃恢复、SSE resume、安全、进程池 |
| 🟢 P2 | 5 | 事件类型、性能验证、Node SDK、测试、匹配效率 |

**整体判断**: 设计方向正确，核心架构合理。P0 问题修复后可以进入实现阶段。建议在 Phase 1 结束时做一次架构评审，验证实际实现与设计的一致性。

---

*— Atlas, 2026-06-20*
