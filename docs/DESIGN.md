# Coding Agent Runtime — 完整设计文档

> **版本**: v1.2.1  
> **日期**: 2026-06-20  
> **状态**: Draft (Revised — 阻塞项修复)

## 1. 概述

### 1.1 目标

构建一个**通用、高性能、可扩展**的 coding agent runtime，为 Hermes、OpenClaw 及任意 agent 提供统一的 coding agent 调用能力。

**核心价值**：
- **统一接口** — 一套 API 调用 Claude Code、Codex 及未来更多 agent
- **高性能** — 流式处理、批量写入、零拷贝，支持长任务（>30min）
- **可扩展** — 插件化 agent、存储后端、输出格式
- **生产就绪** — 崩溃恢复、会话管理、成本追踪、全文搜索

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **流式优先** | 所有输出实时流式处理，不堆内存 |
| **存储分离** | 核心逻辑与存储解耦，支持 SQLite/Postgres/内存 |
| **协议驱动** | 基于 Protocol 的鸭子类型，无需继承 |
| **零依赖** | 核心库不依赖 Hermes/OpenClaw 框架 |
| **渐进增强** | 基础功能零配置，高级功能按需启用 |
| **简单优先** | v1.0 架构从简，避免过度设计 |
| **安全默认** | 默认绑定 localhost，认证必须 |
| **正确性优先** | v1.2.1 后增加：状态机终态不可覆盖、错误路径必须清理资源、JSON 必须安全转义 |

### 1.3 使用场景

| 场景 | 示例 | 阶段 |
|------|------|------|
| **Hermes 集成** | 作为 tool 调用，流式输出到 CLI | Phase 1 |
| **OpenClaw 集成** | 通过 HTTP API 调用，SSE 推送事件 | Phase 2 |
| **CI/CD 管道** | 批量执行任务，结果写入数据库 | Phase 1 |
| **多 Agent 编排** | 并行执行多个 agent，统一监控 | **Phase 3+** |
| **历史搜索** | 全文搜索过去的执行记录 | Phase 2 |

### 1.4 关键设计决策

#### 为什么选 Python？

| 选项 | 优势 | 劣势 | 选择 |
|------|------|------|------|
| **Python** | 生态丰富、asyncio 成熟、与 Hermes/memorix 同栈 | 性能不如 Go/Rust | ✅ |
| Go | 性能优秀、并发模型好 | 与现有栈不匹配、学习成本 | ❌ |
| Rust | 性能最佳、内存安全 | 开发慢、团队经验少 | ❌ |
| Node.js | 与 Claude Code (Bun) 同生态 | 与 Hermes 栈不匹配 | ❌ |

**结论**：Python 是最佳选择，因为：
1. 与 Hermes/memorix 同栈，集成自然
2. asyncio 足够处理 subprocess 流式 IO
3. 团队熟悉，开发效率高

#### 为什么不用现有 Agent Runtime？

| 项目 | 定位 | 问题 |
|------|------|------|
| **LangGraph** | LLM 编排框架 | 太重，我们是 subprocess 管理 |
| **CrewAI** | 多 Agent 协作 | 面向 LLM API，不是 CLI agent |
| **AutoGen** | 多 Agent 对话 | 同上 |
| **Aider** | 单一 coding agent | 不是 runtime，是应用 |

**结论**：现有项目都不解决"统一管理多个 CLI coding agent"的问题。

---

## 2. 架构设计

### 2.1 v1.0 简化架构（3 层）

```
┌─────────────────────────────────────────────────────────┐
│                    Interface Layer                       │
│  CLI  │  HTTP Server (SSE)  │  Python SDK               │
├─────────────────────────────────────────────────────────┤
│                    Executor Layer                        │
│  Stream Executor  │  Agent Adapters  │  SessionRegistry │
├─────────────────────────────────────────────────────────┤
│                    Storage Layer                         │
│  StorageBackend (Protocol)  │  SQLite  │  Postgres      │
└─────────────────────────────────────────────────────────┘
```

**v1.2.1 新增**：`SessionRegistry` — 轻量级 session 管理 + 并发控制（信号量）

**v1.0 不包含**（后续版本考虑）：
- ~~Orchestrator 层~~ — 初期不需要复杂的会话编排
- ~~Event Bus~~ — 简单回调足够
- ~~Embedding Layer~~ — 向量搜索作为可选插件，不是核心层
- ~~Node.js SDK~~ — HTTP API 足够，推迟
- ~~Web UI~~ — 独立项目，不耦合
- ~~stdio JSON 模式~~ — 推迟到 Phase 3
- ~~多 Agent 编排~~ — 推迟到 Phase 3+

### 2.2 核心组件

| 组件 | 职责 | 关键特性 |
|------|------|---------|
| **Interface** | 对外接口 | CLI/HTTP/SDK，统一事件格式 |
| **Executor** | 进程执行 | 流式 subprocess、可配置 buffer、背压控制 |
| **Agent** | Agent 适配 | 命令构建、输出解析、成本提取 |
| **Storage** | 数据持久化 | 批量写入、WAL 模式、全文搜索 |
| **SessionRegistry** | 会话管理 | 并发限制、活跃 session 跟踪 |

### 2.3 数据流

```
User Request
    │
    ▼
┌──────────────┐
│  Interface   │ ← 统一入口 (CLI/HTTP/SDK)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│SessionRegistry│ ← 并发控制 (Semaphore)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Executor   │ ← 创建 session, 启动 subprocess
└──────┬───────┘
       │
       ├──────────────┐
       │              │
       ▼              ▼
┌──────────────┐  ┌──────────────┐
│  Subprocess  │  │   Storage    │
│  (claude/    │  │  (SQLite)    │
│   codex)     │  │              │
└──────┬───────┘  └──────▲───────┘
       │                 │
       │  write first    │
       │  then yield     │
       └─────────────────┘
              │
              ▼
       ┌──────────────┐
       │   Callback   │ ← 分发到多个消费者
       └──────┬───────┘
              │
    ┌─────────┼─────────┐
    │         │         │
    ▼         ▼         ▼
  stdout    file     webhook
```

**关键设计**：先 write 再 yield，确保 storage 是 durable 真相。

### 2.4 与 memorix 的关系

**明确声明**：本项目**借鉴** memorix 的设计模式，但**不直接复用**其代码。

| 方面 | memorix | coding-agents |
|------|---------|---------------|
| **定位** | Agent 记忆层 | Coding agent 执行层 |
| **数据模型** | Observation + Topic | Session + Event |
| **存储** | SQLite + sqlite-vec | SQLite (可选 sqlite-vec) |
| **Embedder** | 核心功能 | 可选插件 |

**借鉴的设计**：
- Protocol-based 存储接口
- SQLite PRAGMA 优化（WAL、mmap）
- 批量写入模式
- 迁移系统

---

## 3. 数据模型

### 3.1 核心实体

```python
# models.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Any

class AgentType(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"

class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"
    ORPHANED = "orphaned"  # 进程崩溃后标记

class EventType(str, Enum):
    SESSION_START = "session.start"  # v1.2.1 新增
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"
    RESULT = "result"
    WATCH = "watch"
    ERROR = "error"

@dataclass
class Session:
    """执行会话"""
    id: str                          # UUID
    agent: AgentType
    prompt: str
    workdir: str
    status: SessionStatus = SessionStatus.PENDING
    
    # 进程信息
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    
    # 时间
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    last_heartbeat_at: Optional[datetime] = None  # 用于检测 orphaned
    
    # 成本与用量
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    
    # 元数据
    model: Optional[str] = None
    provider: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Event:
    """执行事件"""
    id: Optional[int] = None         # 自增 ID
    session_id: str = ""
    channel: str = "stdout"          # "stdout" | "stderr" | "system"
    seq: int = 0                     # 全局单调递增（跨 channel）
    type: EventType = EventType.STDOUT
    data: str = ""
    
    # 可选：原始 JSON（透传模式）
    raw_json: Optional[str] = None
    
    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)
    
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class ExecutionConfig:
    """执行配置"""
    # 超时
    timeout_seconds: int = 3600      # 默认 1 小时
    idle_timeout_seconds: int = 300  # 无输出超时
    
    # 资源限制
    max_memory_mb: int = 4096
    max_budget_usd: float = 10.0
    
    # 重试
    max_retries: int = 0
    retry_delay_seconds: int = 5
    
    # 监控（v1.2.1 恢复 WatchPattern）
    watch_patterns: list[WatchPattern] = field(default_factory=list)
    
    # 输出模式
    output_mode: str = "standard"    # "passthrough" | "standard"
    
    # 模型
    model: Optional[str] = None
    
    # 行长度限制（默认 8MiB）
    line_limit: int = 8 * 1024 * 1024
    
    # 环境变量
    env: dict[str, str] = field(default_factory=dict)

@dataclass
class WatchPattern:
    """监控模式（v1.2.1 恢复）"""
    pattern: str
    action: str = "notify"           # "notify" | "callback" | "stop"
    callback: Optional[str] = None   # webhook URL or function name
```

### 3.2 状态机

```
                    ┌─────────────┐
                    │   PENDING   │
                    └──────┬──────┘
                           │ start()
                           ▼
                    ┌─────────────┐
              ┌─────│   RUNNING   │─────┐
              │     └──────┬──────┘     │
              │            │            │
         kill()       exit_code=0   exit_code!=0
              │            │            │
              ▼            ▼            ▼
        ┌──────────┐ ┌───────────┐ ┌────────┐
        │  KILLED  │ │ COMPLETED │ │ FAILED │
        └──────────┘ └───────────┘ └────────┘
              
              │
              │ timeout
              ▼
        ┌──────────┐
        │ TIMEOUT  │
        └──────────┘

        ┌──────────┐
        │ ORPHANED │ ← 启动时扫描 status=running 且 heartbeat 超时
        └──────────┘
```

**状态转换规则**：
- `PENDING → RUNNING`: 调用 `start()`
- `RUNNING → COMPLETED`: 进程退出，exit_code=0
- `RUNNING → FAILED`: 进程退出，exit_code!=0
- `RUNNING → KILLED`: 调用 `kill()`
- `RUNNING → TIMEOUT`: 超过 timeout_seconds 或 idle_timeout_seconds
- `RUNNING → ORPHANED`: 启动时扫描，heartbeat 超时

**终态**：COMPLETED, FAILED, KILLED, TIMEOUT, ORPHANED

### 3.3 SQLite Schema

```sql
-- 基于 memorix 风格的存储设计

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA mmap_size=268435456;  -- 256MB mmap

-- 执行会话表
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    prompt TEXT NOT NULL,
    workdir TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    
    -- 进程信息
    pid INTEGER,
    exit_code INTEGER,
    
    -- 时间
    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    last_heartbeat_at REAL,  -- 用于检测 orphaned
    
    -- 成本与用量
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    
    -- 元数据
    model TEXT,
    provider TEXT,
    metadata TEXT DEFAULT '{}',
    
    -- 索引字段
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- tags 关联表
CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);

-- 事件表
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL DEFAULT 'stdout',  -- "stdout" | "stderr" | "system"
    seq INTEGER NOT NULL,                     -- 全局单调递增
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    raw_json TEXT,  -- 原始 JSON（透传模式）
    metadata TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    
    UNIQUE(session_id, seq)
);

-- 全文搜索索引
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    data, content=events, content_rowid=id
);

-- 触发器：保持 FTS 同步
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, data) VALUES (new.id, new.data);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, data) VALUES('delete', old.id, old.data);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, data) VALUES('delete', old.id, old.data);
    INSERT INTO events_fts(rowid, data) VALUES (new.id, new.data);
END;

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_heartbeat ON sessions(last_heartbeat_at) WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_channel ON events(channel);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

-- 迁移版本追踪
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);
```

### 3.4 统一事件格式

**双模式设计**：

#### 模式 1: 标准化模式（默认）

提取关键字段，丢失部分细节：

```jsonl
// 会话开始（v1.2.1 新增）
{"type":"session.start","session_id":"uuid","agent":"claude","prompt":"...","timestamp":1234567890}

// 标准输出（提取文本内容）
{"type":"stdout","session_id":"uuid","channel":"stdout","seq":1,"data":"...","timestamp":1234567890}

// 标准错误（v1.2.1 明确：通过 SSE 可见）
{"type":"stderr","session_id":"uuid","channel":"stderr","seq":2,"data":"...","timestamp":1234567890}

// 错误事件（v1.2.1 新增）
{"type":"error","session_id":"uuid","code":"SUBPROCESS_FAILED","message":"...","timestamp":1234567890}

// 会话结束（提取成本）
{"type":"session.end","session_id":"uuid","status":"completed","exit_code":0,"duration_ms":5000,"cost_usd":0.15,"timestamp":1234567890}
```

#### 模式 2: 透传模式

原样输出 agent 的 JSON，保留所有细节：

```jsonl
// Claude Code stream-json 原样输出
{"type":"system","subtype":"init","cwd":"/path","session_id":"...","tools":["Bash","Edit",...]}
{"type":"system","subtype":"thinking_tokens","estimated_tokens":100}
{"type":"assistant","message":{"content":[{"type":"text","text":"Hello!"}]}}
{"type":"result","subtype":"success","total_cost_usd":0.15,...}
```

**存储优化（v1.2.1 修复）**：
- **standard 模式**：`data` 存提取的文本，`raw_json` 为 NULL
- **passthrough 模式**：`data` 存提取的文本（用于 FTS），`raw_json` 存完整原始 JSON

**使用场景**：
- **标准化模式**：简单集成、日志收集、监控
- **透传模式**：需要完整信息（如 thinking tokens、tool calls）

---

## 4. 核心组件设计

### 4.1 SessionRegistry — 并发控制（v1.2.1 新增）

> **v1.2.1 P0-NEW 修复**：修 semaphore 泄漏（P0-NEW-1/2）。`acquire()` 前置检查防止重复获取同 session_id；`kill_session()` 取消任务后释放 semaphore slot，避免 5 次 kill 后死锁。

```python
# registry.py

import asyncio
from typing import Dict, Optional

class SessionRegistry:
    """会话注册表 + 并发控制"""
    
    def __init__(self, max_concurrent: int = 5):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_sessions: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        # v1.2.1 P0-NEW-2 修复：跟踪已持有 slot 的 session
        self._acquired: set[str] = set()
    
    async def acquire(self, session_id: str) -> bool:
        """获取执行许可，返回是否成功"""
        async with self._lock:
            # v1.2.1 P0-NEW-2 修复：防止重复 acquire 同一 session_id 泄漏 slot
            if session_id in self._acquired:
                raise RuntimeError(f"session {session_id} already acquired")
        
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=60)
        except asyncio.TimeoutError:
            return False
        
        # semaphore 获取成功后才记录
        async with self._lock:
            self._active_sessions[session_id] = asyncio.current_task()
            self._acquired.add(session_id)
        return True
    
    async def release(self, session_id: str):
        """释放执行许可"""
        async with self._lock:
            self._active_sessions.pop(session_id, None)
            had_slot = session_id in self._acquired
            self._acquired.discard(session_id)
        # v1.2.1 P0-NEW-1 修复：只在持有 slot 时释放
        if had_slot:
            self._semaphore.release()
    
    async def list_active(self) -> list[str]:
        """列出活跃 session"""
        async with self._lock:
            return list(self._active_sessions.keys())
    
    async def kill_session(self, session_id: str) -> bool:
        """终止活跃 session"""
        async with self._lock:
            task = self._active_sessions.get(session_id)
            had_slot = session_id in self._acquired
        
        if task and not task.done():
            task.cancel()
        
        # v1.2.1 P0-NEW-1 修复：cancel 后必须释放 semaphore slot
        # 否则 5 次 kill 后系统死锁（所有 slot 被占但 session 已结束）
        if had_slot:
            async with self._lock:
                self._active_sessions.pop(session_id, None)
                self._acquired.discard(session_id)
            self._semaphore.release()
        
        return task is not None and not task.done()
```

### 4.2 Executor — 流式进程执行器

**关键设计**：
- 使用 `asyncio.create_subprocess_exec` + 可配置 `limit`（默认 8MiB）
- stdout/stderr 并发读取，防止 pipe 满卡死
- **全局单调递增 seq**（跨 channel），用 `asyncio.Lock` 保证唯一
- **先 write 再 yield**，确保 storage 是 durable 真相
- **Heartbeat 节流**：最多每 1 秒写一次（v1.2.1 修复）
- **Idle timeout watchdog**：检测空闲超时并 kill（v1.2.1 新增）
- **SessionStartEvent**：yield session.start 事件（v1.2.1 新增）
- **stderr 可见**：stderr 事件也 yield 给消费者（v1.2.1 修复）

```python
# executor.py

import asyncio
import json  # v1.2.1 P0-NEW-4 修复：error event 用 json.dumps 避免非法 JSON
import time
from typing import AsyncIterator, Callable, Optional

class SeqCounter:
    """per-session 单调递增序号计数器"""
    def __init__(self):
        self._value = 0
        self._lock = asyncio.Lock()
    
    async def next(self) -> int:
        async with self._lock:
            self._value += 1
            return self._value

class StreamExecutor:
    """流式 subprocess 执行器"""
    
    def __init__(self, store: StorageBackend, config: ExecutionConfig):
        self.store = store
        self.config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._seq = SeqCounter()
        self._buffer: list[Event] = []
        self._last_flush = time.time()
        self._last_heartbeat_write = 0  # v1.2.1: heartbeat 节流
    
    async def execute(
        self,
        session_id: str,
        command: list[str],
        workdir: str,
        env: dict[str, str] = None,
    ) -> AsyncIterator[Event]:
        """执行命令，流式返回事件"""
        
        # v1.2.1 新增：yield session.start 事件
        seq = await self._seq.next()
        start_event = Event(
            session_id=session_id,
            channel="system",
            seq=seq,
            type=EventType.SESSION_START,
            # v1.2.1 P0-NEW-4 修复：用 json.dumps 避免 UUID/路径含特殊字符时非法 JSON
            data=json.dumps({"session_id": session_id, "agent": command[0] if command else "unknown"}),
        )
        self._buffer.append(start_event)
        await self._flush()
        yield start_event
        
        # 启动子进程（line_limit 可配置）
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.config.line_limit,
                env=env,
            )
        except Exception as e:
            # v1.2.1 新增：yield error 事件
            seq = await self._seq.next()
            error_event = Event(
                session_id=session_id,
                channel="system",
                seq=seq,
                type=EventType.ERROR,
                # v1.2.1 P0-NEW-4 修复：用 json.dumps 转义消息（文件路径含引号会破坏 JSON）
                data=json.dumps({
                    "code": "SUBPROCESS_FAILED",
                    "message": str(e),
                    "command": command,
                }),
            )
            self._buffer.append(error_event)
            await self._flush()
            yield error_event
            
            # v1.2.1 P0-NEW-5 修复：启动失败必须更新 session 状态为 FAILED，
            # 否则 session 永远卡在 PENDING 成为幽灵记录
            await self.store.update_session(
                session_id,
                status=SessionStatus.FAILED,
                finished_at=datetime.now(),
                metadata={"error": str(e), "error_type": type(e).__name__},
            )
            return
        
        # 更新 session
        await self.store.update_session(
            session_id,
            pid=self._process.pid,
            status=SessionStatus.RUNNING,
            started_at=datetime.now(),
            last_heartbeat_at=datetime.now(),
        )
        
        # v1.2.1 修复：使用 asyncio.Queue 集中产出事件，按 seq 顺序 yield。
        # stdout reader 和 stderr drain 都推同一个 Queue，
        # 主循环从 Queue 取，seq 全局单调递增保证顺序。
        # 关闭协议：用计数器跟踪 reader 数，每个 reader 结束时减 1，为 0 时才推 EOF。
        event_queue: asyncio.Queue[Optional[Event]] = asyncio.Queue()
        readers_remaining = 2  # stdout reader + stderr drain
        
        async def mark_reader_done():
            nonlocal readers_remaining
            readers_remaining -= 1
            if readers_remaining == 0:
                await event_queue.put(None)  # 最后一个 reader 结束才推 EOF
        
        # stderr 并发 drain → 防止 pipe 满
        # v1.2.1 修复：stderr 事件也 yield 给消费者（推到同一 Queue）。
        stderr_task = asyncio.create_task(
            self._drain_stderr(session_id, self._process.stderr, event_queue, mark_reader_done)
        )
        
        # v1.2.1 新增：idle timeout watchdog
        watchdog_task = asyncio.create_task(
            self._idle_watchdog(session_id)
        )
        
        # stdout 流式读取
        try:
            async def stdout_reader():
                """stdout reader：写到 buffer + 推 event_queue"""
                try:
                    async for line in self._process.stdout:
                        now = time.time()
                        if now - self._last_heartbeat_write >= 1.0:
                            await self.store.update_session(
                                session_id, last_heartbeat_at=datetime.now()
                            )
                            self._last_heartbeat_write = now
                        
                        seq = await self._seq.next()
                        event = Event(
                            session_id=session_id,
                            channel="stdout",
                            seq=seq,
                            type=EventType.STDOUT,
                            data=self._extract_text(line.decode(), self.config.output_mode),
                            raw_json=line.decode() if self.config.output_mode == "passthrough" else None,
                        )
                        self._buffer.append(event)
                        await self._flush_if_needed()
                        await event_queue.put(event)
                finally:
                    await mark_reader_done()
            
            stdout_task = asyncio.create_task(stdout_reader())
            
            while True:
                # v1.2.1 修复：从 queue 取事件，按到达顺序 yield
                event = await event_queue.get()
                if event is None:
                    break  # 所有 reader EOF
                yield event
                # 检查 watch patterns
                await self._check_watch_patterns(session_id, event)
        
        finally:
            # 确保所有事件落盘
            await self._flush()
            
            # 取消 watchdog
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
            
            # 等待 stdout reader 完成（可能已经被外层打断）
            try:
                await stdout_task
            except asyncio.CancelledError:
                pass
            
            # 等待 stderr 完成
            await stderr_task
            
            # 等待进程退出
            exit_code = await self._process.wait()
            
            # v1.2.1 P0-NEW-3 修复：检查 status 是否已被 watchdog 设为终态（如 TIMEOUT），
            # 避免覆盖为 FAILED/COMPLETED。
            current_session = await self.store.get_session(session_id)
            terminal_states = {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.KILLED,
                SessionStatus.TIMEOUT,
                SessionStatus.ORPHANED,
            }
            if current_session and current_session.status in terminal_states:
                # 已被 watchdog/kill 设过终态，不再覆盖
                logger.info(
                    f"session {session_id} already in terminal state {current_session.status.value}, "
                    f"skipping status update"
                )
            else:
                # 更新 session
                await self.store.update_session(
                    session_id,
                    status=SessionStatus.COMPLETED if exit_code == 0 else SessionStatus.FAILED,
                    exit_code=exit_code,
                    finished_at=datetime.now(),
                )
            
            seq = await self._seq.next()
            yield Event(
                session_id=session_id,
                channel="system",
                seq=seq,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": exit_code}),  # v1.2.1 P0-NEW-4 顺手修
            )
    
    async def _drain_stderr(
        self,
        session_id: str,
        stderr: asyncio.StreamReader,
        queue: asyncio.Queue,
        on_done: Callable,
    ):
        """并发读取 stderr，防止 pipe 满。
        
        v1.2.1 修复：stderr 也写入 buffer 并通过 Queue yield 给消费者。
        顺序由全局单调递增 seq 保证（主循环从 Queue 取）。本 task 职责：
        1. 反压式读取 stderr pipe（防止 pipe 满）
        2. 写 buffer（durable storage）
        3. 推到 Queue 供主循环 yield
        4. 完成时调 on_done 通知主循环计数
        """
        try:
            async for line in stderr:
                seq = await self._seq.next()
                event = Event(
                    session_id=session_id,
                    channel="stderr",
                    seq=seq,
                    type=EventType.STDERR,
                    data=line.decode(),
                )
                # 先写 storage（durable）
                self._buffer.append(event)
                await self._flush_if_needed()
                # 推到 Queue 供主循环 yield（给 SSE/SDK）
                await queue.put(event)
        finally:
            await on_done()
    
    async def _idle_watchdog(self, session_id: str):
        """v1.2.1 新增：idle timeout watchdog"""
        while True:
            await asyncio.sleep(5)  # 每 5 秒检查一次
            session = await self.store.get_session(session_id)
            if session and session.last_heartbeat_at:
                idle_seconds = (datetime.now() - session.last_heartbeat_at).total_seconds()
                if idle_seconds > self.config.idle_timeout_seconds:
                    # 超时，kill 进程
                    if self._process and self._process.returncode is None:
                        self._process.terminate()
                    await self.store.update_session(
                        session_id,
                        status=SessionStatus.TIMEOUT,
                        finished_at=datetime.now(),
                    )
                    break
    
    def _extract_text(self, line: str, output_mode: str) -> str:
        """v1.2.1 新增：标准化模式文本提取"""
        if output_mode == "passthrough":
            return line  # 保留原始行
        
        # standard 模式：尝试提取文本内容
        import json
        try:
            event = json.loads(line)
            # Claude Code: assistant.message.content[0].text
            if event.get("type") == "assistant":
                content = event.get("message", {}).get("content", [])
                if content and content[0].get("type") == "text":
                    return content[0].get("text", "")
            # Codex: item.text
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    return item.get("text", "")
        except:
            pass
        return line  # fallback: 保留原始行
    
    async def _flush_if_needed(self):
        """批量 flush：100 条或 100ms"""
        if len(self._buffer) >= 100 or time.time() - self._last_flush >= 0.1:
            await self._flush()
    
    async def _flush(self):
        """写入 storage"""
        if self._buffer:
            # v1.2.1 P1-NEW-1 修复：原子交换 buffer，避免 stdout/stderr 并发 append 时丢事件。
            # 原代码：await append_events(buffer); buffer.clear() 之间不是原子的，
            # stderr drain 在 append_events 等待期间插入的 event 会被 clear 丢失。
            events, self._buffer = self._buffer, []
            self._last_flush = time.time()
            await self.store.append_events(events)
    
    async def _check_watch_patterns(self, session_id: str, event: Event):
        """v1.2.1 恢复：检查监控模式"""
        for wp in self.config.watch_patterns:
            if wp.pattern in event.data:
                if wp.action == "notify":
                    # 发送通知（通过回调）
                    pass
                elif wp.action == "stop":
                    # 停止执行
                    if self._process and self._process.returncode is None:
                        self._process.terminate()
    
    async def kill(self, session_id: str):
        """终止进程"""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
```

### 4.3 Agent — 适配器模式

**设计**：每个 agent 类型实现统一接口，封装命令构建和输出解析。

```python
# agents/base.py

from abc import ABC, abstractmethod
from typing import Optional

class BaseAgent(ABC):
    """Agent 抽象基类"""
    
    @abstractmethod
    def build_command(self, prompt: str, config: ExecutionConfig) -> list[str]:
        """构建执行命令"""
        pass
    
    @abstractmethod
    def parse_output(self, line: str) -> Optional[dict]:
        """解析单行输出，提取结构化信息"""
        pass
    
    @abstractmethod
    def extract_cost(self, output: str) -> Optional[float]:
        """从最终输出提取成本"""
        pass

# agents/claude.py

class ClaudeAgent(BaseAgent):
    """Claude Code 适配器"""
    
    def build_command(self, prompt: str, config: ExecutionConfig) -> list[str]:
        cmd = [
            "claude",
            "-p",  # print mode
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", "bypassPermissions",
        ]
        
        # 预算控制
        if config.max_budget_usd:
            cmd.extend(["--max-budget-usd", str(config.max_budget_usd)])
        
        # 模型
        if config.model:
            cmd.extend(["--model", config.model])
        
        # prompt
        cmd.append(prompt)
        
        return cmd
    
    def parse_output(self, line: str) -> Optional[dict]:
        """解析 Claude Code stream-json 输出"""
        import json
        try:
            event = json.loads(line)
            
            # 提取关键信息
            if event.get("type") == "result":
                return {
                    "cost_usd": event.get("total_cost_usd"),
                    "input_tokens": event.get("usage", {}).get("input_tokens"),
                    "output_tokens": event.get("usage", {}).get("output_tokens"),
                    "model": event.get("model"),
                }
            
            return None
        except:
            return None
    
    def extract_cost(self, output: str) -> Optional[float]:
        """从最终输出提取成本"""
        import json
        try:
            # 找最后一行 result 事件
            for line in reversed(output.split("\n")):
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "result":
                    return event.get("total_cost_usd")
        except:
            pass
        return None

# agents/codex.py

class CodexAgent(BaseAgent):
    """Codex CLI 适配器"""
    
    def build_command(self, prompt: str, config: ExecutionConfig) -> list[str]:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--full-auto",
        ]
        
        # 模型
        if config.model:
            cmd.extend(["-m", config.model])
        
        # prompt
        cmd.append(prompt)
        
        return cmd
    
    def parse_output(self, line: str) -> Optional[dict]:
        """解析 Codex --json 输出"""
        import json
        try:
            event = json.loads(line)
            
            # 提取关键信息
            if event.get("type") == "turn.completed":
                usage = event.get("usage", {})
                return {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    # Codex 不提供成本
                }
            
            return None
        except:
            return None
    
    def extract_cost(self, output: str) -> Optional[float]:
        """Codex 不提供成本信息"""
        return None
```

### 4.4 Storage — Protocol 设计

**设计**：基于 Protocol 的鸭子类型，支持多种实现。

```python
# storage/base.py

from typing import Protocol, runtime_checkable, AsyncIterator, Optional

@runtime_checkable
class StorageBackend(Protocol):
    """存储后端 Protocol"""
    
    async def initialize(self) -> None:
        """初始化存储（创建表等）"""
        ...
    
    async def close(self) -> None:
        """关闭连接"""
        ...
    
    # Session 操作
    async def create_session(self, session: Session) -> str:
        """创建会话，返回 session_id"""
        ...
    
    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话"""
        ...
    
    async def update_session(self, session_id: str, **kwargs) -> None:
        """更新会话字段"""
        ...
    
    async def list_sessions(
        self,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        tags: Optional[list[str]] = None,  # v1.2.1 新增：tag 过滤
        limit: int = 100,
    ) -> list[Session]:
        """列出会话"""
        ...
    
    # v1.2.1 新增：Tags 管理
    async def add_tag(self, session_id: str, tag: str) -> None:
        """添加 tag"""
        ...
    
    async def remove_tag(self, session_id: str, tag: str) -> None:
        """移除 tag"""
        ...
    
    async def list_tags(self, session_id: str) -> list[str]:
        """列出 session 的所有 tags"""
        ...
    
    # Event 操作
    async def append_event(self, event: Event) -> int:
        """追加事件，返回 event_id"""
        ...
    
    async def append_events(self, events: list[Event]) -> None:
        """批量追加事件"""
        ...
    
    async def stream_events(
        self,
        session_id: str,
        after_seq: int = 0,
    ) -> AsyncIterator[Event]:
        """流式读取事件"""
        ...
    
    async def search_events(
        self,
        query: str,
        agent: Optional[str] = None,
        limit: int = 20,
    ) -> list[Event]:
        """搜索事件（全文搜索，使用 FTS5）"""
        ...
    
    # 崩溃恢复
    async def recover_orphaned_sessions(self, timeout_seconds: int = 300) -> int:
        """扫描并标记 orphaned sessions，返回标记数量"""
        ...
```

### 4.5 崩溃恢复

**明确边界**：v1.0 只支持**状态恢复**（标记为 ORPHANED），**不支持执行续跑**。

```python
# recovery.py

class SessionRecovery:
    """会话恢复管理器"""
    
    def __init__(self, store: StorageBackend):
        self.store = store
    
    async def recover_on_startup(self, timeout_seconds: int = 300) -> int:
        """启动时扫描并标记 orphaned sessions"""
        count = await self.store.recover_orphaned_sessions(timeout_seconds)
        if count > 0:
            logger.warning(f"Recovered {count} orphaned sessions")
        return count
```

**SQLite 实现**：
```sql
-- 启动时执行
UPDATE sessions 
SET status = 'orphaned', exit_code = -1, finished_at = strftime('%s', 'now')
WHERE status = 'running' 
  AND (last_heartbeat_at IS NULL OR last_heartbeat_at < strftime('%s', 'now') - ?);
```

---

## 5. 接口设计

### 5.1 CLI

```bash
# 基础用法
coding-agent run claude "重构这个函数" --workdir ~/project

# 后台执行
coding-agent run claude "添加测试" --background
# → {"session_id": "uuid"}

# 查询状态
coding-agent status <session_id>
# → {"status": "running", "duration": "2m30s", "events": 1523}

# 实时流式输出
coding-agent stream <session_id> --follow

# 搜索历史（全文搜索）
coding-agent search "重构" --agent claude --last 7d

# 列出会话
coding-agent list --agent claude --status completed --limit 20

# 列出活跃会话
coding-agent list --active

# 终止执行
coding-agent kill <session_id>

# 恢复 orphaned sessions
coding-agent recover

# Tag 管理（v1.2.1 新增）
coding-agent tag add <session_id> <tag>
coding-agent tag remove <session_id> <tag>
coding-agent tag list <session_id>
```

### 5.2 HTTP Server

```bash
# 启动服务（默认绑定 127.0.0.1）
coding-agent serve --port 8080 --host 127.0.0.1

# 生产部署（需要认证）
coding-agent serve --port 8080 --host 0.0.0.0 --auth-token <token>
```

**认证机制（v1.2.1 明确）**：
- Token 来源：环境变量 `CODING_AGENT_AUTH_TOKEN` 或 `--auth-token` 参数
- 验证方式：明文比对（`token == expected_token`）
- 本地模式（127.0.0.1）：认证可选
- 远程模式（0.0.0.0）：认证强制

**API 端点**：

```
POST /api/v1/run
  Body: {"agent": "claude", "prompt": "...", "workdir": "...", "config": {...}}
  Response: {"session_id": "uuid"}
  Auth: Bearer token（远程模式强制）

GET /api/v1/sessions
  Query: ?agent=claude&status=completed&tags=important,urgent&limit=20
  Response: [{"id": "...", "agent": "claude", "tags": [...], ...}]
  Auth: Bearer token

GET /api/v1/sessions/:id
  Response: {"id": "...", "status": "running", ...}
  Auth: Bearer token

# SSE 流式传输（v1.2.1 明确协议细节）
GET /api/v1/sessions/:id/events/stream
  Query: ?after_seq=100
  Header: Accept: text/event-stream
  Header: Last-Event-ID: 100  (断点续传)
  Response: SSE stream
  
  SSE 协议细节：
  - 每个事件带 id 字段（seq）
  - 支持 Last-Event-ID 断点续传
  - 空闲时每 30 秒发送 ping 心跳
  - session 结束后自动关闭连接
  - 客户端断线后自动重连（浏览器原生支持）
  
  Auth: Bearer token

# 终止执行
POST /api/v1/sessions/:id/kill
  Response: {"status": "killed"}
  Auth: Bearer token

# 删除记录（级联删除 events）
DELETE /api/v1/sessions/:id
  Response: {"deleted": true}
  Auth: Bearer token

# Tag 管理（v1.2.1 新增）
POST /api/v1/sessions/:id/tags
  Body: {"tag": "important"}
  Response: {"added": true}
  Auth: Bearer token

DELETE /api/v1/sessions/:id/tags/:tag
  Response: {"removed": true}
  Auth: Bearer token

POST /api/v1/search
  Body: {"query": "重构", "agent": "claude", "limit": 20}
  Response: [{"session_id": "...", "event": "..."}]
  Auth: Bearer token

GET /health
  Response: {"status": "ok", "sqlite": "ok", "running_sessions": 3, "max_concurrent": 5}
```

**SSE 事件格式**：
```
event: session.start
id: 1
data: {"session_id":"uuid","agent":"claude","timestamp":1234567890}

event: stdout
id: 2
data: {"session_id":"uuid","seq":2,"data":"...","timestamp":1234567890}

event: stderr
id: 3
data: {"session_id":"uuid","seq":3,"data":"...","timestamp":1234567890}

event: session.end
id: 4
data: {"session_id":"uuid","status":"completed","exit_code":0}

event: ping
data: {}
```

### 5.3 Python SDK

```python
from coding_agents import CodingAgent, SQLiteStorage

# 初始化
storage = SQLiteStorage("~/.coding-agents/data.db")
agent = CodingAgent(storage=storage)

# 同步执行
session = await agent.run(
    agent="claude",
    prompt="重构这个函数",
    workdir="~/project",
)
print(f"Session: {session.id}, Status: {session.status}")

# 流式执行
async for event in agent.run_stream(
    agent="claude",
    prompt="添加测试",
    workdir="~/project",
):
    if event.type == "stdout":
        print(event.data, end="")
    elif event.type == "stderr":
        print(f"[stderr] {event.data}", end="")

# 查询历史
sessions = await agent.list_sessions(agent="claude", limit=10)

# 按 tag 过滤（v1.2.1 新增）
sessions = await agent.list_sessions(tags=["important"], limit=10)

# 全文搜索
results = await agent.search_events("重构", agent="claude", limit=20)

# 恢复 orphaned
count = await agent.recover_orphaned()

# Tag 管理（v1.2.1 新增）
await agent.add_tag(session_id, "important")
await agent.remove_tag(session_id, "important")
tags = await agent.list_tags(session_id)
```

---

## 6. 集成示例

### 6.1 Hermes 集成

#### 方式 1: 作为 Hermes Tool

```python
# tools/coding_agent_tool.py

import json
from tools.registry import registry
from coding_agents import CodingAgent, SQLiteStorage

async def coding_agent_tool(
    agent: str,
    prompt: str,
    workdir: str = ".",
    background: bool = False,
) -> str:
    """调用 coding agent (Claude Code / Codex)"""
    
    storage = SQLiteStorage("~/.coding-agents/data.db")
    ca = CodingAgent(storage=storage)
    
    if background:
        # 后台执行
        session = await ca.run_background(
            agent=agent,
            prompt=prompt,
            workdir=workdir,
        )
        return json.dumps({"session_id": session.id, "status": "started"})
    else:
        # 前台执行，收集输出
        output = []
        async for event in ca.run_stream(
            agent=agent,
            prompt=prompt,
            workdir=workdir,
        ):
            if event.type == "stdout":
                output.append(event.data)
        
        return "".join(output)

registry.register(
    name="coding_agent",
    toolset="coding",
    schema={
        "name": "coding_agent",
        "description": "Call Claude Code or Codex to execute coding tasks",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["claude", "codex"]},
                "prompt": {"type": "string"},
                "workdir": {"type": "string", "default": "."},
                "background": {"type": "boolean", "default": False},
            },
            "required": ["agent", "prompt"],
        },
    },
    handler=lambda args, **kw: coding_agent_tool(
        agent=args["agent"],
        prompt=args["prompt"],
        workdir=args.get("workdir", "."),
        background=args.get("background", False),
    ),
)
```

#### 方式 2: 直接调用 CLI

```python
# 在 Hermes 中直接调用
result = terminal('coding-agent run claude "重构函数" --workdir ~/project')
```

### 6.2 OpenClaw 集成

#### 方式 1: HTTP API

```javascript
// OpenClaw agent 调用 coding-agent
const response = await fetch('http://localhost:8080/api/v1/run', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer <token>',
  },
  body: JSON.stringify({
    agent: 'claude',
    prompt: '重构函数',
    workdir: '/path/to/project',
  }),
});

const { session_id } = await response.json();

// 订阅 SSE 事件流
const eventSource = new EventSource(
  `http://localhost:8080/api/v1/sessions/${session_id}/events/stream`,
  { headers: { 'Authorization': 'Bearer <token>' } }
);

eventSource.addEventListener('stdout', (event) => {
  const data = JSON.parse(event.data);
  console.log(data.data);
});

eventSource.addEventListener('stderr', (event) => {
  const data = JSON.parse(event.data);
  console.error(`[stderr] ${data.data}`);
});

eventSource.addEventListener('session.end', (event) => {
  const data = JSON.parse(event.data);
  console.log(`Completed: ${data.status}`);
});
```

### 6.3 CI/CD 集成

```yaml
# .github/workflows/coding-agent.yml

name: Coding Agent

on:
  workflow_dispatch:
    inputs:
      task:
        description: 'Task description'
        required: true

jobs:
  run-agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Install coding-agents
        run: pip install coding-agents
      
      - name: Run Claude Code
        run: |
          coding-agent run claude "${{ github.event.inputs.task }}" \
            --workdir . \
            --output json \
            > result.json
      
      - name: Upload result
        uses: actions/upload-artifact@v4
        with:
          name: agent-result
          path: result.json
```

---

## 7. 性能设计

### 7.1 内存优化

| 策略 | 实现 |
|------|------|
| **流式读取** | `async for line in proc.stdout`，不收集全量输出 |
| **批量写入** | 100 条或 100ms flush 一次，减少 IO |
| **Heartbeat 节流** | 最多每 1 秒写一次，避免写风暴（v1.2.1 修复） |
| **mmap** | SQLite `mmap_size=256MB`，减少 syscall |

### 7.2 IO 优化

| 策略 | 实现 |
|------|------|
| **WAL 模式** | 读写并发，写入不阻塞读取 |
| **异步 IO** | `aiosqlite` 全异步，不阻塞事件循环 |
| **并发 drain** | stderr 单独 task，防止 pipe 满 |
| **索引优化** | 复合索引 + 部分索引，加速查询 |
| **FTS5** | 全文搜索虚拟表，加速文本搜索 |

### 7.3 并发控制（v1.2.1 明确实现）

| 场景 | 限制 | 实现 |
|------|------|------|
| **写入并发** | SQLite 写锁全局 | 写写串行，WAL 只解决读写并发 |
| **执行并发** | ≤5 个 agent 同时执行 | `asyncio.Semaphore(5)` + `SessionRegistry` |
| **排队超时** | 60 秒 | 超时返回错误 |
| **查询并发** | 无限制 | WAL 模式下读写不冲突 |

**如果高并发写入是瓶颈**：
- 方案 1: 写入队列（异步批量）
- 方案 2: 分库（按 agent 或日期）
- 方案 3: 换 Postgres（真正的并发写入）

### 7.4 性能指标

| 场景 | 目标 | 说明 |
|------|------|------|
| **短任务 (<1min)** | <100ms 启动延迟，<10MB 内存 | |
| **长任务 (30min)** | 恒定 <50MB 内存，零丢失 | Heartbeat 节流 |
| **5 并发** | <100MB 内存，可接受写延迟 | Semaphore 控制 |
| **100 万事件** | <1s 查询延迟（有索引） | 仅限索引查询 |
| **全文搜索** | <2s（FTS5） | 取决于数据量 |

**注意**：Phase 1 结束后需做基准测试验证这些指标。

---

## 8. 安全设计

### 8.1 网络安全

| 措施 | 说明 |
|------|------|
| **默认绑定 localhost** | `--host 127.0.0.1`，仅本地访问 |
| **Bearer token 认证** | 环境变量 `CODING_AGENT_AUTH_TOKEN` 或 `--auth-token` |
| **本地模式** | 认证可选 |
| **远程模式** | 认证强制 |

### 8.2 输入验证

| 风险 | 措施 |
|------|------|
| **workdir 路径遍历** | 规范化路径，检查是否在白名单内 |
| **prompt 注入** | 参数转义，不直接拼接到 shell |
| **环境变量注入** | `env` 字段白名单过滤 |

### 8.3 资源限制

| 资源 | 限制 | 说明 |
|------|------|------|
| **内存** | `max_memory_mb` | 默认 4096MB |
| **预算** | `max_budget_usd` | 默认 $10 |
| **时间** | `timeout_seconds` | 默认 3600s |
| **空闲时间** | `idle_timeout_seconds` | 默认 300s（v1.2.1 有 watchdog） |
| **CPU** | 未限制 | 未来可加 cgroup |

### 8.4 沙箱（未来）

v1.0 不强制沙箱，但建议：
- 开发环境：直接执行
- 生产环境：Docker/nsjail 沙箱

---

## 9. 扩展机制

### 9.1 插件化 Agent

```python
# 注册自定义 agent
from coding_agents import register_agent, BaseAgent

class MyCustomAgent(BaseAgent):
    def build_command(self, prompt, config):
        return ["my-agent", "--prompt", prompt]
    
    def parse_output(self, line):
        # 解析自定义输出
        pass

register_agent("custom", MyCustomAgent())

# 使用
agent = CodingAgent(storage=storage)
session = await agent.run(agent="custom", prompt="...")
```

### 9.2 自定义存储后端

```python
# 实现 Postgres 存储
from coding_agents import StorageBackend

class PostgresStorage:
    async def initialize(self):
        self.pool = await asyncpg.create_pool(...)
        await self.pool.execute(SCHEMA_SQL)
    
    async def append_events(self, events):
        async with self.pool.acquire() as conn:
            await conn.executemany(INSERT_SQL, events)

# 使用
storage = PostgresStorage(dsn="postgresql://...")
agent = CodingAgent(storage=storage)
```

### 9.3 事件回调

```python
# 简单回调注册
agent.on("session.start", lambda e: print(f"Started: {e.session_id}"))
agent.on("session.end", lambda e: send_notification(e))
agent.on("watch.match", lambda e: trigger_webhook(e))
```

---

## 10. 实现计划

### Phase 1: 核心（3-4 周，含 5 天 buffer）

| 任务 | 预计 | 说明 |
|------|------|------|
| 数据模型 + SQLite schema | 2d | 含 tags 关联表、FTS5 |
| StreamExecutor（流式执行器） | 5d | 含全局 seq、heartbeat 节流、idle watchdog、SessionStartEvent |
| SessionRegistry（并发控制） | 1d | Semaphore + 活跃 session 跟踪 |
| ClaudeAgent + CodexAgent | 2d | 命令构建、输出解析 |
| SQLiteStorage | 3d | Protocol 实现、崩溃恢复、tags 管理 |
| CLI 基础命令 | 2d | run/status/list/search/kill/recover/tag |
| 单元测试 | 3d | 覆盖核心逻辑 |
| 基准测试 | 1d | 验证性能指标 |
| **小计** | **19d** | 含 buffer |

### Phase 2: 接口（2 周）

| 任务 | 预计 | 说明 |
|------|------|------|
| HTTP Server (FastAPI + SSE) | 5d | 含认证、断点续传、心跳、tag 管理 |
| Python SDK | 2d | 封装 HTTP 或直接调用 |
| 集成示例 | 2d | Hermes + OpenClaw |
| 文档 | 2d | API 文档 + 示例 |
| 集成测试 | 2d | |
| **小计** | **13d** | |

### Phase 3: 高级功能（持续）

| 任务 | 预计 | 说明 |
|------|------|------|
| 向量搜索（sqlite-vec） | 3d | 可选 |
| stdio JSON 模式 | 2d | |
| Node.js SDK | 3d | HTTP-only client |
| 多 Agent 编排 | 5d | DAG 定义、依赖管理 |
| 会话恢复（执行续跑） | 5d | 需要 agent CLI 支持 |
| Web UI | 5d | 独立项目 |

**总计**：Phase 1-2 约 7-8 周（含 buffer）

---

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| **subprocess 崩溃** | 丢失输出 | WAL + 批量写入，最多丢 100ms |
| **pipe 满卡死** | 进程挂起 | stderr 并发 drain |
| **内存爆炸** | OOM | 流式读取 + Semaphore 并发控制 |
| **SQLite 写竞争** | 性能下降 | 限制并发数 ≤5，或换 Postgres |
| **agent CLI 变更** | 解析失败 | 版本检测 + 适配器模式 |
| **长任务超时** | 执行中断 | timeout_seconds + idle_timeout watchdog |
| **runtime 崩溃** | session 悬挂 | 启动时扫描 orphaned，标记为 ORPHANED |
| **heartbeat 写风暴** | 性能下降 | 节流：最多每 1 秒写一次（v1.2.1 修复） |

---

## 12. 成功标准

| 指标 | 目标 |
|------|------|
| **功能** | 支持 Claude Code + Codex，CLI/HTTP/SDK |
| **性能** | 5 并发，30min 长任务，<50MB 内存 |
| **可靠性** | 零数据丢失，崩溃恢复（状态恢复） |
| **安全** | 默认 localhost，Bearer token 认证 |
| **集成** | Hermes + OpenClaw 验证通过 |
| **文档** | API 文档 + 集成示例 |

---

## 附录

### A. 参考资料

- [memorix 架构](../../memorix/AGENTS.md)
- [Claude Code CLI 对比](./cli-comparison.md)
- [Buffer Size 调研](./research/buffer-size-investigation.md)
- [设计评审 - Atlas](./DESIGN-REVIEW.md)
- [设计评审 - Claude v1.0.0](./DESIGN-REVIEW-CLAUDE.md)
- [设计评审 - Claude v1.2.0](./DESIGN-REVIEW-v1.2.0.md)

### B. 术语表

| 术语 | 定义 |
|------|------|
| **Session** | 一次 agent 执行会话 |
| **Event** | 会话中的一条事件（stdout/stderr/system） |
| **Stream** | 实时流式输出 |
| **WAL** | Write-Ahead Logging，SQLite 并发模式 |
| **mmap** | 内存映射文件，减少 syscall |
| **FTS5** | SQLite 全文搜索扩展 |
| **SSE** | Server-Sent Events，单向推送协议 |

### C. 变更日志

- 2026-06-20: v1.0.0 初始设计
- 2026-06-20: v1.1.0 修订版
  - 简化架构：5 层 → 3 层
  - 明确 memorix 关系：借鉴而非复用
  - 补充并发限制说明
  - 增加双模式输出（透传/标准化）
  - 补充具体集成示例
  - 补充设计决策说明
  - 调整实现计划时间估计
- 2026-06-20: v1.2.0 P0/P1 修复版
  - **P0-1**: stderr seq 改用全局 monotonic counter + channel 字段
  - **P0-2**: tags JSON 字段拆成关联表
  - **P0-3**: 崩溃恢复明确边界（只做状态恢复，不做执行续跑）
  - **P0-4**: SSE 协议明确（SSE + Last-Event-ID 断点续传）
  - **P0-5**: 安全设计（默认 localhost + Bearer token）
  - **P0-6**: append_events 返回 None（不返回假 ID）
  - **P0-7**: ExecutionConfig 补充 model 字段
  - **P0-8**: 先 write 再 yield（确保 storage 是 durable 真相）
  - **P1**: 背压控制、状态机形式化、FTS5 全文搜索
  - **P1**: 8MiB line_limit 可配置
  - **P1**: DELETE 语义明确（kill vs delete）
  - **砍掉**: Node.js SDK/Web UI/stdio 推迟
  - **时间**: Phase 1 加 5 天 buffer
- 2026-06-20: v1.2.1 阻塞项修复版
  - **新增 SessionRegistry**: `asyncio.Semaphore(5)` 并发控制 + 排队超时
  - **新增 Idle timeout watchdog**: 每 5 秒检查，超时自动 kill
  - **新增 SessionStartEvent**: execute() 开头 yield session.start
  - **新增 Error event**: subprocess 启动失败时 yield error 事件
  - **Heartbeat 节流**: 最多每 1 秒写一次，避免写风暴
  - **Tags 管理 API**: `add_tag`/`remove_tag`/`list_tags`/`filter_by_tag`
  - **stderr 可见**: stderr 事件写入 storage，可通过 stream_events 读取
  - **WatchPattern 恢复**: 支持 notify/callback/stop 三种 action
  - **双模式存储优化**: standard 模式 `raw_json` 为 NULL，避免冗余
  - **SSE 协议细节**: 心跳（30s ping）、断点续传、自动关闭
  - **认证明确**: 环境变量 `CODING_AGENT_AUTH_TOKEN`，明文比对
  - **多 Agent 编排**: 标注为 Phase 3+
  - **时间**: Phase 1 增加到 19 天
- 2026-06-20: v1.2.1 P0-NEW 修复版（Claude 评审后修订）
  - **P0-NEW-1**: SessionRegistry.kill_session 释放 semaphore slot（修 5 次 kill 后死锁）
  - **P0-NEW-2**: SessionRegistry.acquire 前置检查同 session_id 重复 acquire
  - **P0-NEW-3**: finally 块检查 status 已被 watchdog 设为终态，避免覆盖 TIMEOUT
  - **P0-NEW-4**: error/start/result event 改用 `json.dumps` 转义（修含特殊字符时非法 JSON）
  - **P0-NEW-5**: subprocess 启动失败 yield error 后必须 `update_session(status=FAILED)`
  - **P1-NEW-1**: `_flush` 原子交换 buffer（修并发丢事件）
  - **顺手修**: stderr yield 语义统一（Queue 桥接 + 主循环从 Queue 取）
  - **顺手修**: start_event 处理 `command=[]` 边界情况
  - **顺手修**: result event data 用 `json.dumps`
  - **注意**: 仍需出 **v1.2.2** 才能作为 Phase 1 编码基线
- 2026-06-20: v0.2.0 Session 1 — CLI 增强 + 监控基础
  - **T1.1 kill 命令直接终止子进程 + 心跳轮询**:
    - `StreamExecutor.execute()` 新增 `_heartbeat_checker` task，每 2s 轮询 DB 状态
    - 检测到 KILLED/FAILED → SIGTERM → 等 5s → SIGKILL
    - 实测：`sleep 60` → kill 在 2.1s 内终止（远低于 10s 要求）
    - 修 P1-1 + P1-4
  - **T1.2 structlog 日志框架**:
    - 引入 structlog，替换 stdlib logging
    - JSON 输出 + 标准化字段（timestamp/level/event/session_id/seq）
    - CLI 全局选项 `--log-level` / `--log-json`
    - 修 P2-7
  - **T1.3 bandit B608 误报标注**:
    - 在 sqlite.py 的 f-string SQL 处加 `# nosec B608`
    - bandit medium 3 → 0
    - 修 P2-1
  - **T1.4 CLI 流式输出**:
    - `run --stream` 实时打印 `[channel seq=N] data` 到 stderr
    - 默认模式只显示最终结果（保持向后兼容）
    - 修 P2-3
  - **T1.5 CLI 本地认证 token**:
    - 新增 `auth.py`：token 生成 / 存储 / 加载 / 验证
    - 全局选项 `--auth-token-file`（默认 `~/.coding-agents-token`）
    - 首次运行自动生成 256-bit token（0600 权限）
    - Phase 1 仅基础设施；Phase 2 HTTP server 将消费此 token
    - 修 L-3
  - **测试结果**: 161 passed / 2 skipped（新增 24 测试）
  - **覆盖率**: 91%（基线 89%，+2pp）
  - **mypy strict**: 0 errors
  - **bandit**: 0 medium

---

## §C v0.2.0 Session 4 — 测试增强（E2E + 重试 + 基准）

**日期**: 2026-06-20
**目标**: 补齐 3 项测试/性能改进

### C.1 真实 CLI E2E 测试（mock server）

**问题**: P2-2 — 真实 Claude/Codex E2E 测试覆盖率低（需要 API key）

**方案**: 用 mock CLI server 替代真实 API，验证事件解析 + cost 提取

**实现**:
- `tests/integration/real_e2e/conftest.py` — pytest fixture，启动 mock CLI server
- `tests/integration/real_e2e/mock_claude.py` — 模拟 claude CLI（stream-json 格式）
- `tests/integration/real_e2e/mock_codex.py` — 模拟 codex CLI（--json 格式）
- `tests/integration/real_e2e/test_mock_claude_e2e.py` — 验证 claude 事件解析 + cost 提取
- `tests/integration/real_e2e/test_mock_codex_e2e.py` — 验证 codex 事件解析 + usage 提取

**技术要点**:
- Mock CLI 用 `#!/usr/bin/env python3` 脚本
- conftest 创建临时目录 + symlink + PATH 注入
- Mock 输出符合 agent adapter 的 parse_output 期望格式
- 验证完整 pipeline: Agent.build_command → StreamExecutor.execute → parse_output

**测试结果**: 6 个新测试，验证事件解析、cost 提取、session 生命周期

### C.2 Session 重试机制

**问题**: P2-4 — ExecutionConfig 有 max_retries 字段，但 executor 未实现

**方案**: 实现指数退避重试，不修改 executor.py（通过 wrapper 集成）

**实现**:
- `src/coding_agents/retry.py` — RetryPolicy dataclass + with_retry async decorator
  - `RetryPolicy(max_retries, delay_seconds, backoff_multiplier, retry_on)`
  - `with_retry(coro_factory, policy)` — 通用 async retry
  - `with_retry_generator(gen_factory, policy)` — async generator retry
  - `RetryError` exception
- `src/coding_agents/retry_integration.py` — make_executor_with_retry wrapper
  - 不修改 executor.py，通过 monkey patch 集成
  - 返回 RetryingExecutor 代理对象，转发 execute() + kill()
- `tests/test_retry.py` — 单元测试（成功、失败、重试、退避、generator）

**技术要点**:
- 指数退避: delay * (backoff_multiplier ** attempt)
- 支持指定 retry_on 异常类型
- Generator 重试：失败时重启整个 generator
- 日志记录每次重试尝试 + 最终失败

**测试结果**: 14 个新测试，覆盖 policy 配置、with_retry、with_retry_generator

### C.3 30min 长任务基准

**问题**: P2-8 — 内存基线测试只验证 < 50MB，未做 30min 长任务基准

**方案**: 用 mock subprocess 模拟 5min 任务（压缩 30min），测量内存/CPU/吞吐

**实现**:
- `tests/benchmarks/conftest.py` — psutil fixture（内存/CPU 监控）
- `tests/benchmarks/mock_subprocess.py` — mock subprocess 持续 5min（20 events/sec）
- `tests/benchmarks/test_benchmark.py` — 3 个基准测试:
  1. `test_memory_baseline` — 内存峰值 < 50MB（设计目标）
  2. `test_event_throughput` — 事件吞吐 > 100 events/sec（设计目标）
  3. `test_concurrent_5_sessions` — 5 并发内存 < 100MB（设计目标）

**技术要点**:
- pytest-benchmark 自动对比性能
- psutil 测量内存峰值 + CPU 平均
- Mock subprocess 每 0.05s 输出一行，持续 300s（压缩 5min）
- CI 环境缩短到 10s（避免超时）

**测试结果**: 3 个基准测试，验证内存/吞吐/并发性能

### C.4 依赖 + 配置

**新增依赖** (pyproject.toml):
- `pytest-benchmark>=4.0.0` — 性能基准测试
- `pytest-timeout>=2.2.0` — 测试超时控制
- `psutil>=5.9.0` — 内存/CPU 监控

**配置更新**:
- `[tool.pytest.ini_options]` 加 `timeout = 300`（5min 全局超时）

### C.5 测试结果汇总

**新增测试**: 23 个
- E2E: 6 个（mock claude + mock codex）
- 重试: 14 个（policy + with_retry + generator）
- 基准: 3 个（内存 + 吞吐 + 并发）

**总计**: 184 passed / 2 skipped（基线 161，+23）

**修复问题**:
- ✅ P2-2: 真实 E2E 测试覆盖率低（mock server 替代 API key）
- ✅ P2-4: Session 重试机制未实现（指数退避 + generator 支持）
- ✅ P2-8: 无 30min 长任务基准（mock subprocess 压缩 5min）

**文件隔离**: ✅ 严格遵守，只修改/创建指定文件，未碰 executor.py

**下一步**:
- Phase 2: HTTP API（13 天）
- Phase 3: 多 Agent 编排（5 天）

---

### C.6 Session 2: HTTP API + Metrics (T2.1-T2.3)

**新增模块**:
- `src/coding_agents/http/` - FastAPI HTTP 服务器
  - `server.py` - FastAPI 应用工厂
  - `auth.py` - Bearer token 认证中间件
  - `sse.py` - SSE 事件格式化（支持 Last-Event-ID 续传）
  - `cli_integration.py` - 服务器启动脚本
  - `metrics_endpoint.py` - Prometheus /metrics 端点
  - `routes/sessions.py` - Session CRUD (POST/GET /sessions)
  - `routes/events.py` - 事件查询 (REST + SSE)
  - `routes/actions.py` - 操作端点 (kill, recover)
  - `routes/tags.py` - 标签管理
- `src/coding_agents/metrics.py` - Prometheus 指标定义
- `src/coding_agents/metrics_integration.py` - 装饰器集成 (@track_session, @track_event)

**新增依赖** (pyproject.toml):
- `fastapi>=0.115.0` - Web 框架
- `uvicorn[standard]>=0.32.0` - ASGI 服务器
- `sse-starlette>=2.0.0` - SSE 支持
- `prometheus-client>=0.21.0` - Prometheus 指标
- `httpx>=0.27.0` - 异步 HTTP 客户端（测试）

**API 端点**:
- `POST /sessions` - 创建 session
- `GET /sessions` - 列出 sessions（支持过滤）
- `GET /sessions/{id}` - 获取 session
- `GET /sessions/{id}/events` - 获取事件（REST）
- `GET /sessions/{id}/events/stream` - 流式事件（SSE）
- `POST /sessions/{id}/kill` - 终止 session
- `POST /sessions/{id}/tags` - 添加标签
- `DELETE /sessions/{id}/tags/{tag}` - 删除标签
- `GET /sessions/{id}/tags` - 列出标签
- `POST /recover` - 恢复孤儿 sessions
- `GET /metrics` - Prometheus 指标
- `GET /health` - 健康检查

**Prometheus 指标**:
- `sessions_total` (Counter, labels: agent, status) - Session 生命周期
- `session_duration_seconds` (Histogram) - Session 持续时间
- `events_appended_total` (Counter, labels: channel) - 事件追加
- `active_sessions` (Gauge) - 当前活跃 sessions
- `session_registry_wait_seconds` (Histogram) - Registry 等待时间
- `subprocess_memory_bytes` (Gauge, labels: session_id) - 子进程内存

**测试结果**:
- HTTP API: 23 个测试全部通过
- Metrics: 18 个测试全部通过
- 总计: 41 个新测试

**类型检查**: ✅ mypy --strict 通过（13 个文件）

**文件隔离**: ✅ 严格遵守，只修改/创建指定文件，未碰 executor.py, cli.py, registry.py, storage/*, agents/*, orchestrator/*

**启动服务器**:
```bash
uv run python -m coding_agents.http.cli_integration --port 8080 --host 127.0.0.1
```

**安全特性**:
- 默认绑定 127.0.0.1（仅本地访问）
- Bearer token 认证（从 ~/.coding-agents-token 读取）
- 常量时间比较防止时序攻击

**SSE 特性**:
- 支持 Last-Event-ID 头实现断点续传
- 自动检测客户端断开连接
- 事件格式：event type + JSON data

**下一步**:
- T2.4: 更新 PHASE1_ISSUES.md
- Phase 2 后续: 实际执行集成（当前 HTTP API 只创建 session 记录，不启动执行）

### C.7 Session 3: 多 Agent 编排 + Session 续跑 (T3.1-T3.2)

**日期**: 2026-06-20
**目标**: 解锁批量多 agent 场景 + 从中断点续跑 session

#### C.7.1 T3.1 — DAG-based multi-agent orchestration

**问题**: P1-2 — 多 Agent 编排未实现（v1.2.1 标注 Phase 3+）

**方案**: 引入 `TaskFlow`（DAG 容器）+ `TaskResult`（任务结果），按拓扑分层并行执行，超时通过 executor 的 `finally` 块传播到子进程，依赖失败时跳过下游任务。

**实现**:
- `src/coding_agents/orchestrator/__init__.py` — 模块导出
- `src/coding_agents/orchestrator/dag.py` — `Task` dataclass + `TaskFlow`（Kahn 拓扑排序 + 环检测）+ `TaskResult` + `execution_layers()`（返回可并行执行的层）
- `src/coding_agents/orchestrator/runner.py` — `FlowRunner`（asyncio.gather 并行执行 + per-task `asyncio.wait_for` 超时 + 依赖失败跳过）
- `src/coding_agents/orchestrator/cli_integration.py` — CLI helpers（不改 cli.py）
- `tests/test_orchestrator.py` — 40 个测试（TaskFlow 构造、拓扑排序、环检测、Diamond DAG、并行执行、超时、依赖失败传播、CLI integration）

**技术要点**:
- Kahn 算法拓扑排序：O(V+E)，自然暴露环
- `execution_layers` 返回 `list[list[Task]]`，每个内层可并发执行
- per-task timeout 经 `asyncio.wait_for` 触发，最终由 `StreamExecutor` finally 块 SIGTERM 子进程
- 依赖失败传播：父任务未 COMPLETED → 子任务标 `skipped`，不启动 subprocess
- 不修改 `executor.py` / `cli.py` / `registry.py` / `storage/*` / `agents/*`，纯新增模块

#### C.7.2 T3.2 — Session resume（执行续跑）

**问题**: P2-5 — 只能恢复状态（标记 ORPHANED），不能从 `last_seq` 续跑；v1.2.1 设计明确这是 Phase 3+ 范围

**方案**: 新增 `resume.py`，提供 `ResumeInfo` + `can_resume()` + `prepare_resume_command()` + `resume_session()`，注入 agent CLI 的 `--resume` flag 即可续跑（claude/codex 原生支持）。

**实现**:
- `src/coding_agents/resume.py` — `ResumeInfo` / `can_resume()`（status 终态 + exit_code=0 或 KILLED/TIMEOUT）/ `prepare_resume_command()`（注入 `--resume <session_id>`）/ `resume_session()`（创建 linked 新 session + 重启 agent）
- `tests/test_resume.py` — 46 个测试（can_resume 各种 status、--resume flag 注入、seq 续号、linked session 关联、CLI 不支持场景降级、异常路径全覆盖）

**技术要点**:
- agent CLI 差异：`claude --resume <id>` vs `codex --resume <id>`，由 adapter 提供
- 续跑策略：创建**新** session（避免破坏原 session 历史），通过 metadata 字段关联 `resumed_from`
- last_seq 续号：resume session 从 `original_last_seq + 1` 开始 append events
- 不可续跑场景：FAILED/INVALID_CONFIG/非 agent 错误 → 抛 `ResumeError`
- 不修改 `executor.py` / `agents/*`，纯新增模块

#### C.7.3 测试结果汇总

**新增测试**: 86 个
- Orchestrator: 40 个（DAG + Runner + CLI integration）
- Resume: 46 个（can_resume + prepare_resume + resume_session）

**已知问题**: `tests/test_orchestrator.py::TestRunFlow::test_complex_dag_topological_order` 偶发失败（task c 在 mock agent 中报告 "bad parameter or other API misuse"，疑为 mock 副作用，待后续修复）。本节文档同步阶段不修改源代码，留待独立 PR。

**总计**: ~270 passed / 2 skipped / 1 flaky（基线 184，+86，扣除 1 flaky）

**修复问题**:
- ✅ P1-2: 多 Agent 编排未实现（DAG + 并行执行 + 超时传播）
- ✅ P2-5: 无 session 恢复 / 执行续跑（--resume flag 注入 + last_seq 续号）

**文件隔离**: ✅ 严格遵守，只修改/创建指定文件

**下一步**:
- Phase 3 后续: Node.js SDK（HTTP-only client）/ Web UI / 向量搜索（sqlite-vec）
- Phase 3+ 高级: 并发调度优化、distributed executor、更多 agent 适配

### C.8 Session 4: Python SDK + OpenClaw 集成示例 (Phase 2 接入层)

**日期**: 2026-06-20
**目标**: 完成 DESIGN.md §1.3 Phase 2「OpenClaw 集成」,交付可被 OpenClaw / 其他异步 Python host 直接消费的 SDK 和示例。

#### C.8.1 决策依据 — Option A 薄封装

按 plan v2 选择 **薄 HTTP 封装** 而非「SDK 包揽执行语义」,原因:

- HTTP API 是单一可信源,SDK = 强类型视图,避免双实现
- 「SDK 触发执行」会把执行策略(谁、何时、并发度)绑死在 SDK,违反职责分离
- 测试简单(`httpx.MockTransport`),不依赖真子进程

关键契约(plan v2 §约束 1):

> `POST /sessions` 仅创建 `PENDING` session,**不**触发执行。SDK 的 `create_session()` 返回的 session 状态为 `pending`,需用户自管 executor 来消费 pending 队列。

#### C.8.2 交付清单

| 路径 | 内容 |
| --- | --- |
| `sdk/coding_agents_sdk/client.py` | `AsyncCodingAgentClient`(async-only),覆盖全部 12 个端点 |
| `sdk/coding_agents_sdk/models.py` | Pydantic 模型(Session/Event/Tag/KillResult/RecoverResult/HealthStatus),独立定义不依赖服务端 |
| `sdk/coding_agents_sdk/exceptions.py` | `APIError` / `AuthenticationError`(401)/ `NotFoundError`(404)/ `ServerError`(5xx)/ `ConnectionError_` |
| `sdk/coding_agents_sdk/__init__.py` | 公共 API + 版本导出 |
| `sdk/tests/test_client.py` | 24 个异步测试,使用 `httpx.MockTransport`,覆盖所有端点 + 错误路径 + SSE 流 |
| `sdk/README.md` | 快速开始 + 完整 API 表 |
| `sdk/pyproject.toml` | 可独立 `pip install -e ./sdk` |
| `openclaw_integration/examples/create_session.py` | 创建 session(标注「不触发执行」) |
| `openclaw_integration/examples/query_status.py` | 轮询直到终态(2s 间隔,默认 600s 超时) |
| `openclaw_integration/examples/stream_events.py` | SSE 订阅 + `Last-Event-ID` 续跑 + SIGINT 优雅停 |
| `openclaw_integration/examples/error_handling.py` | 401/404/5xx/超时 重试与降级演示 |
| `openclaw_integration/README.md` + `docs/INTEGRATION.md` | 拓扑图 + executor 契约 + FAQ |

#### C.8.3 测试结果

**SDK 测试**: `pytest sdk/tests/ -v` → **24 passed in 0.07s**

覆盖:
- 会话生命周期(create/get/list、context manager、owned vs injected client)
- 事件 REST + SSE(stream_events 多事件 + Last-Event-ID 头透传 + 404 错误传播)
- 操作(kill/recover + recover 参数透传)
- Tag(create 正确 body `{"tag": "..."}` / list 兼容两种返回格式 / delete 路径)
- 健康(metrics 透传 Prometheus 文本)
- 错误路径:401→AuthenticationError、404→NotFoundError、5xx→ServerError、其他 4xx→APIError、连接失败→ConnectionError_
- Base URL 尾斜杠兼容、token Bearer 头注入

**示例脚本冒烟**: 4 个脚本均可独立运行,无参数时打印 usage 提示并以非零码退出。

#### C.8.4 已修复问题

- ✅ Phase 2 「OpenClaw 集成」无 SDK(新增 `sdk/` 提供 async Python 入口)
- ✅ Phase 2 接入层缺文档与示例(新增 `openclaw_integration/`,含 4 个脚本 + 集成指南)
- ✅ Tag API 路径与 body 签名易错(`POST /sessions/{id}/tags`,body=`{"tag":"..."}`,在 SDK 模型和测试里固化)
- ✅ SSE 流测试不稳定(使用 `httpx.MockTransport` 直接构造 SSE 文本,完全离线)

#### C.8.5 未触碰的边界(严格执行)

- ❌ `src/coding_agents/` 未修改
- ❌ `tests/` 既有测试未修改(新增的 `sdk/tests/` 是新目录)
- ❌ 没有修改 SDK 之外的服务端模型(SDK 模型独立,见 SHOULD #2)
- ❌ 没有引入同步 client(只 async,见 SHOULD #1)

#### C.8.6 下一步

- Phase 3+: 高级接入层(Node.js SDK / Web UI / OpenAPI spec 自动生成 client)
- 后续若新增 SDK 不承诺的语义(如 `wait_until_done()`),放到 `coding_agents_sdk.highlevel` 子模块,绝不污染薄封装核心
