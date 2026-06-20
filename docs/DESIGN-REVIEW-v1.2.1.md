# DESIGN.md v1.2.0 → v1.2.1 深度对比审查

> **审查日期**: 2026-06-20
> **审查基线**: `31d642e` (v1.2.0, 1431 行)
> **审查目标**: `5ac5967` (v1.2.1, 1678 行, +247 行)
> **审查范围**: 全部 13 项变更 + 回归 + 可执行性

---

## 总体评价

v1.2.1 在**广度上**有明显进步——13 项 CEO 声称的修复中，**10 项确实有实质代码或 schema 体现**，SessionRegistry、idle watchdog、Tags CRUD、SSE 协议细化、WatchPattern 恢复都是正向变更。

但在**深度上**，新增代码引入了**至少 4 个新 P0 级 bug**，且之前审查提出的部分 P0-miss 仍未解决。文档处于"**看起来更完整，但直接交给 Phase 1 编码会产生错误实现**"的状态。

**评级：🟡 有条件通过**——需修复 P0-NEW-1 ~ P0-NEW-4 后才能作为编码基线。

---

## 一、逐项变更审查

### 变更 1: SessionRegistry（新增 4.1 节）

**声称**: `asyncio.Semaphore(5)` 并发控制 + 60s 排队超时 + 活跃 session 跟踪

**审查结果**: ⚠️ 设计合理但有 3 个实现级问题

#### 锁顺序与死锁风险

```python
async def acquire(self, session_id: str) -> bool:
    try:
        await asyncio.wait_for(self._semaphore.acquire(), timeout=60)
        async with self._lock:
            self._active_sessions[session_id] = asyncio.current_task()
        return True
    except asyncio.TimeoutError:
        return False
```

**结论：无死锁风险**。Semaphore 在 lock 外获取，release 时也是 lock 外释放——顺序一致。asyncio.Lock 不可重入但这里没有嵌套获取，安全。

#### P0-NEW-1: Semaphore 泄漏 — kill_session 后无释放路径

```python
async def kill_session(self, session_id: str) -> bool:
    async with self._lock:
        task = self._active_sessions.get(session_id)
        if task and not task.done():
            task.cancel()
            return True      # ← semaphore 未释放
        return False
```

`task.cancel()` 只是发出取消信号。**semaphore slot 永远不会被释放**，除非有外部代码（谁？）在 task 的 CancelledError 处理中调用 `release()`。但文档中：

1. `SessionRegistry` 与 `StreamExecutor` **无任何集成代码**——`execute()` 从未调用 `acquire()`/`release()`
2. `kill_session` 不释放 semaphore
3. 没有 `async with` 上下文管理器包装

**后果**：每次 kill 一个活跃 session，并发上限永久 -1。5 次 kill 后系统死锁——所有新请求排队到超时。

**修复建议**：

```python
class SessionRegistry:
    async def run(self, session_id: str, coro):
        """包装执行：自动 acquire/release"""
        if not await self.acquire(session_id):
            raise ConcurrencyLimitExceeded()
        try:
            return await coro
        finally:
            await self.release(session_id)
```

#### P0-NEW-2: 重复 acquire 泄漏

如果调用者对同一 `session_id` 调用两次 `acquire()`（例如重试逻辑 bug）：
- 第一次：semaphore -1，注册 task
- 第二次：semaphore -1（再扣一个 slot），覆盖注册

第二次 acquire 成功但只消耗了一个 slot——另一个 slot 泄漏。`release()` 只调用一次。

**修复建议**：acquire 开头检查 `session_id in self._active_sessions`。

#### 缺失：集成点未定义

文档没有展示 `StreamExecutor.execute()` 何时调用 `registry.acquire()`。应该在 `execute()` 入口处 acquire，在 finally 中 release。但代码没有。Phase 1 编码者会问："我在哪里插入这些调用？"

---

### 变更 2: Idle timeout watchdog

**声称**: 每 5 秒检查 heartbeat，超时 kill

**审查结果**: ⚠️ 核心逻辑正确，但有一个严重的状态覆盖 bug

#### P0-NEW-3: Watchdog 设置的 TIMEOUT 状态被 finally 块覆盖

Watchdog 逻辑：
```python
# watchdog 检测到超时
if self._process and self._process.returncode is None:
    self._process.terminate()
await self.store.update_session(
    session_id,
    status=SessionStatus.TIMEOUT,    # ← 设置 TIMEOUT
    finished_at=datetime.now(),
)
break
```

但 finally 块：
```python
finally:
    # ...
    exit_code = await self._process.wait()
    await self.store.update_session(
        session_id,
        status=SessionStatus.COMPLETED if exit_code == 0 else SessionStatus.FAILED,  # ← 覆盖为 FAILED
        exit_code=exit_code,
        finished_at=datetime.now(),
    )
```

`process.terminate()` 发送 SIGTERM → `process.wait()` 返回 -15 → `exit_code != 0` → 状态被设为 **FAILED**，**覆盖了 watchdog 设置的 TIMEOUT**。

**后果**：用户永远看不到 TIMEOUT 状态。idle_timeout 触发了 kill，但 session 显示为 FAILED。客户端无法区分"进程自己崩溃"和"被 watchdog 杀掉"。

**修复建议**：

```python
finally:
    # 检查 watchdog 是否已设置终态
    current = await self.store.get_session(session_id)
    if current and current.status == SessionStatus.TIMEOUT:
        # watchdog 已处理，不再覆盖
        await self._process.wait()  # 收割僵尸进程
        return

    exit_code = await self._process.wait()
    # ... 正常状态更新
```

#### 时钟一致性问题

Watchdog 使用 `datetime.now()`（挂钟），heartbeat 节流使用 `time.time()`（也是挂钟，但不同源）。在 NTP 跳变（尤其是 VM/容器）时，`datetime.now()` 可能回退，导致 watchdog 误判或漏判。

建议统一使用 `time.monotonic()` 存储 heartbeat，或至少在 watchdog 中容忍小量回退。

#### 次要问题：heartbeat 只在 stdout 行到达时更新

如果 agent 只写 stderr（某些 agent 的 debug 模式），heartbeat 不更新，watchdog 会误杀。建议 stderr drain 也更新 heartbeat。

---

### 变更 3: SessionStartEvent

**声称**: execute() 开头 yield session.start

**审查结果**: ✅ 基本正确

```python
seq = await self._seq.next()
start_event = Event(
    session_id=session_id,
    channel="system",
    seq=seq,
    type=EventType.SESSION_START,
    data=f'{{"session_id":"{session_id}","agent":"{command[0]}"}}',
)
self._buffer.append(start_event)
await self._flush()
yield start_event
```

#### 问题（P1）：f-string 构造 JSON

`data=f'{{"session_id":"{session_id}","agent":"{command[0]}"}}'`

如果 `session_id` 或 `command[0]` 包含 `"` 或 `\`，生成的 `data` 不是合法 JSON。`session_id` 通常是 UUID（安全），`command[0]` 是 `"claude"` 或 `"codex"`（安全），所以**实际风险低**。但这是坏模式——同样的模式出现在 error event 中，风险更高（见 P0-NEW-4）。

---

### 变更 4: Error event

**声称**: subprocess 启动失败 yield error

**审查结果**: ❌ 有 2 个 P0 级问题

#### P0-NEW-4: error event 的 data 字段会生成非法 JSON

```python
data=f'{{"code":"SUBPROCESS_FAILED","message":"{str(e)}"}}'
```

`str(e)` 可能包含：
- 文件路径中的引号：`FileNotFoundError: [Errno 2] No such file or directory: '/path/with"quote'`
- 多行错误信息
- Unicode 字符

这些都会产生**非法 JSON**，导致下游 SSE 客户端 `JSON.parse()` 崩溃。

**修复**：
```python
import json
data=json.dumps({"code": "SUBPROCESS_FAILED", "message": str(e)})
```

#### P0-NEW-5: Error 路径不更新 session 状态

如果 subprocess 启动失败：
```python
except Exception as e:
    # yield error event
    yield error_event
    return    # ← 直接返回，session 状态仍是 PENDING
```

session 留在 PENDING 状态，永远不会变成 FAILED。`list_sessions(status="failed")` 看不到它。`recover_orphaned_sessions` 也不会处理它（因为 `started_at` 是 NULL）。这个 session 变成**幽灵记录**。

**修复**：在 `return` 前添加：
```python
await self.store.update_session(
    session_id,
    status=SessionStatus.FAILED,
    finished_at=datetime.now(),
)
```

---

### 变更 5: Heartbeat 节流

**声称**: 最多每 1 秒写一次

**审查结果**: ✅ 逻辑正确

```python
now = time.time()
if now - self._last_heartbeat_write >= 1.0:
    await self.store.update_session(
        session_id, last_heartbeat_at=datetime.now()
    )
    self._last_heartbeat_write = now
```

节流用 `time.time()` (float)，写入用 `datetime.now()` (datetime)——两套时钟，但各自内部一致。可接受。

**次要问题**：第一条 stdout 行到达时 `_last_heartbeat_write = 0`，`now - 0 >= 1.0` 必为 True，所以第一次总是写。正确。

---

### 变更 6: Tags CRUD API

**声称**: add_tag/remove_tag/list_tags + list_sessions 加 tags 参数

**审查结果**: ⚠️ Protocol 接口定义了，但无 SQL 实现

```python
async def add_tag(self, session_id: str, tag: str) -> None: ...
async def remove_tag(self, session_id: str, tag: str) -> None: ...
async def list_tags(self, session_id: str) -> list[str]: ...
```

**缺失**：
1. `list_sessions(tags=...)` 是 AND 还是 OR 语义？未定义
2. 没有 SQL 实现——`session_tags` 表存在，但 JOIN 查询未给出
3. CLI 命令 `coding-agent tag add/remove/list` 没有参数校验——tag 可以为空字符串吗？有长度限制吗？
4. HTTP API `POST /api/v1/sessions/:id/tags` 没有输入验证

**P1 建议**：至少给出 `list_sessions` 的 SQL：
```sql
SELECT DISTINCT s.* FROM sessions s
JOIN session_tags st ON s.id = st.session_id
WHERE st.tag IN (?, ?, ...)
```
并明确 AND vs OR。

---

### 变更 7: stderr 可见

**声称**: stderr 写入 storage + 通过 stream_events 读取

**审查结果**: ⚠️ 有并发安全问题

#### stderr drain 的 `_buffer` 竞争

`_drain_stderr` 和 stdout 循环**并发** append 到 `self._buffer`。`_flush()` 方法：

```python
async def _flush(self):
    if self._buffer:
        await self.store.append_events(self._buffer)  # ← 异步，不持有锁
        self._buffer.clear()                            # ← 清空整个 buffer
        self._last_flush = time.time()
```

**竞争场景**：
1. stderr task: `self._buffer.append(event_A)` → append_events 开始（包含 event_A）
2. stdout task: `self._buffer.append(event_B)` → 进入 _flush
3. stderr task: append_events 完成 → `self._buffer.clear()` → **event_B 丢失**
4. stdout task: 也开始 clear → 已经空了，no-op

**后果**：在 stderr 活跃时（agent 输出大量 debug 信息），stdout 事件可能静默丢失。

**修复**：
```python
async def _flush(self):
    if self._buffer:
        events, self._buffer = self._buffer, []   # 原子交换
        await self.store.append_events(events)
        self._last_flush = time.time()
```

**注意**：此 bug 在 v1.2.0 中也存在（stderr + stdout 并发 append），但 v1.2.0 的文档声称 stderr "不 yield"，暗示 stderr 量少。v1.2.1 明确说 stderr 可见，暗示 stderr 量可能很大，放大了此 bug 的触发概率。

#### 文档矛盾

关键设计声称 "stderr 事件也 yield 给消费者（v1.2.1 修复）"——但实际代码注释说：
```python
# v1.2.1 修复：stderr 事件也放入 buffer，但不 yield（避免乱序）
```

**两处说法矛盾**。实际代码是 stderr 不 yield，只写入 buffer。关键设计列表是错的。

---

### 变更 8: WatchPattern 恢复

**声称**: 从 list[str] 改回 dataclass，支持 notify/callback/stop

**审查结果**: ⚠️ 部分实现

```python
@dataclass
class WatchPattern:
    pattern: str
    action: str = "notify"           # "notify" | "callback" | "stop"
    callback: Optional[str] = None   # webhook URL or function name
```

```python
async def _check_watch_patterns(self, session_id: str, event: Event):
    for wp in self.config.watch_patterns:
        if wp.pattern in event.data:
            if wp.action == "notify":
                pass                          # ← 空实现
            elif wp.action == "stop":
                if self._process and self._process.returncode is None:
                    self._process.terminate()
            # callback action 完全没处理
```

**问题**：
1. `notify` action 是 `pass`——什么都不做
2. `callback` action 完全没处理——dataclass 有 `callback` 字段但代码忽略它
3. 只有 `stop` 有实现

**P1**：要么砍掉未实现的 action（只保留 `stop`），要么实现它们。当前状态会误导用户。

---

### 变更 9: 双模式存储优化

**声称**: standard 模式 raw_json=NULL

**审查结果**: ✅ 逻辑正确

```python
data=self._extract_text(line.decode(), self.config.output_mode),
raw_json=line.decode() if self.config.output_mode == "passthrough" else None,
```

standard 模式：`data` = 提取的文本，`raw_json` = NULL。节省存储空间。
passthrough 模式：`data` = 提取的文本（用于 FTS），`raw_json` = 原始 JSON。

**问题（P2）**：standard 模式下，如果 `_extract_text` 提取失败（非 JSON 行、未知事件类型），`data` = 原始行，`raw_json` = NULL。原始行被当作文本存储，无法还原为 JSON——这是设计意图，但意味着 **standard 模式有信息损失且不可逆**。文档应明确这一点。

---

### 变更 10: SSE 协议细节

**声称**: 30s ping 心跳 + 断点续传 + 自动关闭

**审查结果**: ⚠️ 协议格式正确，但实现细节缺失

SSE 格式：
```
event: session.start
id: 1
data: {...}

event: ping
data: {}
```

✅ 格式符合 SSE 规范。`id` 字段用于断点续传（`Last-Event-ID` header）。

**缺失**：
1. **ping 由谁发送？** SSE endpoint 的代码没有给出。executor yield 事件，HTTP handler 转发——但 ping 需要 timer，不是来自 executor。
2. **自动关闭**：session 结束后如何通知 HTTP handler 关闭 SSE 连接？executor yield `session.end` 后 handler 收到并关闭？需要在 HTTP handler 代码中展示。
3. **断点续传的 seq 语义**：`after_seq=100` 返回 seq > 100 的事件。但 `seq` 是 per-session 的（见 SeqCounter），所以跨 session 无意义。文档说 "全局单调递增"（v1.2.0）→ "per-session 单调递增"（v1.2.1），修改正确。

---

### 变更 11: 认证明确

**声称**: 环境变量 `CODING_AGENT_AUTH_TOKEN` + 明文比对

**审查结果**: ✅ 可接受

```
Token 来源：环境变量 CODING_AGENT_AUTH_TOKEN 或 --auth-token 参数
验证方式：明文比对（token == expected_token）
本地模式（127.0.0.1）：认证可选
远程模式（0.0.0.0）：认证强制
```

**P2 建议**：明文比对有 timing attack 风险。虽然本地部署风险低，但建议用 `hmac.compare_digest()`。文档应提及这一点。

---

### 变更 12: 多 Agent 编标注 Phase 3+

**审查结果**: ✅ 正确决策。从核心场景降级到高级功能，减少 Phase 1 范围。

---

### 变更 13: _extract_text

**审查结果**: ⚠️ 功能正确但有多个问题

```python
def _extract_text(self, line: str, output_mode: str) -> str:
    if output_mode == "passthrough":
        return line
    import json
    try:
        event = json.loads(line)
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            if content and content[0].get("type") == "text":
                return content[0].get("text", "")
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                return item.get("text", "")
    except:
        pass
    return line
```

**问题汇总**：

| # | 问题 | 严重性 |
|---|------|--------|
| 1 | `except:` 裸捕获，会吞掉 KeyboardInterrupt/SystemExit | P1 |
| 2 | 只取 `content[0]`——多 content block（text + tool_use）时丢失 | P2 |
| 3 | 非 JSON 行（banner、分隔线）返回原始行——standard 模式混入原始数据 | P2 |
| 4 | `import json` 在函数体内——每次调用都执行 import（CPython 有缓存，但坏习惯） | P3 |
| 5 | 提取失败时 `return line`——调用者无法区分"提取的文本"和"原始 JSON fallback" | P2 |

---

## 二、新引入的问题汇总

| ID | 问题 | 严重性 | 位置 |
|----|------|--------|------|
| P0-NEW-1 | kill_session 后 semaphore slot 永久泄漏 | **P0** | SessionRegistry |
| P0-NEW-2 | 重复 acquire 泄漏 semaphore slot | **P0** | SessionRegistry |
| P0-NEW-3 | Watchdog TIMEOUT 状态被 finally 块覆盖为 FAILED | **P0** | execute() finally |
| P0-NEW-4 | error event 的 f-string 生成非法 JSON | **P0** | execute() except |
| P0-NEW-5 | Error 路径不更新 session 状态，产生幽灵 PENDING | **P0** | execute() except |
| P1-NEW-1 | `_flush()` 并发竞争导致事件丢失 | **P1** | _flush() |
| P1-NEW-2 | 文档声称 stderr yield 但代码不 yield | **P1** | 关键设计描述 |
| P1-NEW-3 | `except:` 裸捕获 | **P1** | _extract_text |
| P1-NEW-4 | WatchPattern notify/callback 未实现 | **P1** | _check_watch_patterns |

---

## 三、之前 P0-miss 追踪

之前审查提出的 4 项 P0-miss 状态：

| # | P0-miss 描述 | v1.2.1 状态 |
|---|-------------|-------------|
| 1 | SessionRegistry 缺失 | ✅ **已修复**——Semaphore + Lock + active sessions |
| 2 | Idle watchdog 缺失 | ⚠️ **部分修复**——有 watchdog 但 TIMEOUT 被覆盖 (P0-NEW-3) |
| 3 | SessionStartEvent 缺失 | ✅ **已修复**——execute() 开头 yield session.start |
| 4 | Error event 缺失 | ⚠️ **部分修复**——有 error event 但 JSON 非法 (P0-NEW-4) + 状态不更新 (P0-NEW-5) |

**结论**：4 项 P0-miss 全部有对应代码，但 2 项的实现有 bug。**数量上全修，质量上 2/4。**

---

## 四、回归分析（v1.2.0 正确代码是否被改坏）

| 项目 | 回归？ | 说明 |
|------|--------|------|
| SeqCounter 描述 "全局" → "per-session" | ✅ **修正**，非回归 | 实际代码一直是 per-session（每个 executor 一个 counter）。旧描述误导 |
| Codex passthrough 示例被删除 | ⚠️ **文档退化** | v1.2.0 展示了 Codex 的 4 种事件类型；v1.2.1 删除了。`_extract_text` 仍处理 `item.completed`，但缺少示例 |
| 背压控制被替换为 heartbeat 节流 | ⚠️ **概念退化** | v1.2.0 声称 "背压控制防内存爆炸"；v1.2.1 改为 "heartbeat 节流"。heartbeat 节流不防内存爆炸。性能表格中 "长任务 <50MB" 的承诺现在无支撑 |
| execute() 中 subprocess 启动无 try/except | ❌ **回归** | v1.2.0 启动失败直接抛异常（调用者处理）；v1.2.1 捕获但处理有 bug (P0-NEW-4/5)。** worse than before** |
| `_flush()` 并发竞争 | ⚠️ **一直存在，现在更容易触发** | v1.2.0 stderr 量少，触发概率低；v1.2.1 stderr 可见，量增大 |
| WatchPattern list[str] → dataclass | ✅ **修正**，非回归 | v1.2.0 丢失了 action/callback 能力 |
| 多 Agent 编排降级 | ✅ 正确决策 | 非回归 |

---

## 五、可执行性评估

> Phase 1 编码能否直接基于此文档？

**结论：不能。** 有以下阻塞性歧义：

| # | 歧义 | 影响 |
|---|------|------|
| 1 | SessionRegistry 与 StreamExecutor 的集成点未定义 | 编码者不知道在哪里调 acquire/release |
| 2 | finally 块中 TIMEOUT vs FAILED 优先级 | 编码者会实现出错误的状态机 |
| 3 | error 路径不更新 session 状态 | 编码者会实现出幽灵 session |
| 4 | `_flush()` 并发竞争 | 编码者会实现出数据丢失 |
| 5 | WatchPattern notify/callback 是空实现 | 编码者不确定是否要实现 |
| 6 | list_sessions(tags=...) 是 AND 还是 OR | 编码者随机选择 |
| 7 | SSE ping 由谁发送 | 编码者不知道在哪个组件加 timer |

---

## 六、修复优先级排序

### 必须在编码前修复（P0）

1. **P0-NEW-3**: finally 块 TIMEOUT 状态覆盖——改状态机逻辑
2. **P0-NEW-4/5**: error 路径 JSON 非法 + 状态不更新——用 json.dumps + 加 update_session
3. **P0-NEW-1/2**: SessionRegistry semaphore 泄漏——添加 `run()` 上下文管理器 + 防重复 acquire

### 编码中修复（P1）

4. **P1-NEW-1**: `_flush()` 原子交换
5. **P1-NEW-2**: 修正文档"stderr yield"描述
6. **P1-NEW-3**: `except:` 改为具体异常
7. **P1-NEW-4**: WatchPattern 砍掉或实现 notify/callback

### 可延后（P2/P3）

8. list_sessions AND/OR 语义明确
9. SSE ping 实现位置
10. timing-safe 认证比对
11. _extract_text 多 content block 支持

---

## 七、总结

v1.2.1 解决了 v1.2.0 的 4 个 P0-miss 的**结构**问题（缺什么补什么），但新增代码的**实现质量**引入了 5 个新的 P0 级 bug。最严重的是：

1. **SessionRegistry semaphore 泄漏**——5 次 kill 后系统死锁
2. **TIMEOUT 状态被覆盖**——idle timeout 功能形同虚设
3. **error 路径幽灵 session**——启动失败后 session 永远卡在 PENDING

**建议**：CEO 修复上述 5 个 P0-NEW 后，出 v1.2.2。Phase 1 编码应基于 v1.2.2 而非当前 v1.2.1。
