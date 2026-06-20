# Coding Agent Runtime — 完整设计文档

> **版本**: v1.0.0  
> **日期**: 2026-06-20  
> **状态**: Draft

## 1. 概述

### 1.1 目标

构建一个**通用、高性能、可扩展**的 coding agent runtime，为 Hermes、OpenClaw 及任意 agent 提供统一的 coding agent 调用能力。

**核心价值**：
- **统一接口** — 一套 API 调用 Claude Code、Codex 及未来更多 agent
- **高性能** — 流式处理、批量写入、零拷贝，支持长任务（>30min）
- **可扩展** — 插件化 agent、存储后端、输出格式
- **生产就绪** — 崩溃恢复、会话管理、成本追踪、语义搜索

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **流式优先** | 所有输出实时流式处理，不堆内存 |
| **存储分离** | 核心逻辑与存储解耦，支持 SQLite/Postgres/内存 |
| **协议驱动** | 基于 Protocol 的鸭子类型，无需继承 |
| **零依赖** | 核心库不依赖 Hermes/OpenClaw 框架 |
| **渐进增强** | 基础功能零配置，高级功能按需启用 |

### 1.3 使用场景

| 场景 | 示例 |
|------|------|
| **Hermes 集成** | 作为 tool 调用，流式输出到 CLI |
| **OpenClaw 集成** | 通过 HTTP API 调用，SSE 推送事件 |
| **CI/CD 管道** | 批量执行任务，结果写入数据库 |
| **多 Agent 编排** | 并行执行多个 agent，统一监控 |
| **历史搜索** | 语义搜索过去的执行记录 |

---

## 2. 架构设计

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────┐
│                    Interface Layer                       │
│  CLI  │  HTTP Server  │  stdio JSON  │  Python SDK      │
├─────────────────────────────────────────────────────────┤
│                    Orchestrator                          │
│  Session Manager  │  Agent Router  │  Event Bus         │
├─────────────────────────────────────────────────────────┤
│                    Executor Layer                        │
│  Stream Executor  │  Process Pool  │  Error Recovery    │
├─────────────────────────────────────────────────────────┤
│                    Agent Layer                           │
│  BaseAgent  │  ClaudeAgent  │  CodexAgent  │  Custom    │
├─────────────────────────────────────────────────────────┤
│                    Storage Layer                         │
│  StorageBackend  │  SQLite  │  Postgres  │  Memory      │
├─────────────────────────────────────────────────────────┤
│                    Embedding Layer (Optional)            │
│  Embedder  │  LMStudio  │  OpenAI  │  SentenceTransform │
└─────────────────────────────────────────────────────────┘
```

### 2.2 核心组件

| 组件 | 职责 | 关键特性 |
|------|------|---------|
| **Interface** | 对外接口 | CLI/HTTP/stdio/SDK，统一事件格式 |
| **Orchestrator** | 会话编排 | 生命周期管理、agent 路由、事件分发 |
| **Executor** | 进程执行 | 流式 subprocess、8MiB buffer、崩溃恢复 |
| **Agent** | Agent 适配 | 命令构建、输出解析、成本提取 |
| **Storage** | 数据持久化 | 批量写入、WAL 模式、向量搜索 |
| **Embedding** | 语义索引 | 可选，用于事件语义搜索 |

### 2.3 数据流

```
User Request
    │
    ▼
┌──────────────┐
│  Interface   │ ← 统一入口
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Orchestrator │ ← 创建 session, 路由到 agent
└──────┬───────┘
       │
       ├──────────────┐
       │              │
       ▼              ▼
┌──────────────┐  ┌──────────────┐
│   Executor   │  │   Storage    │
│  (subprocess)│  │  (SQLite)    │
└──────┬───────┘  └──────▲───────┘
       │                 │
       │  stream events  │
       └─────────────────┘
              │
              ▼
       ┌──────────────┐
       │  Event Bus   │ ← 分发到多个消费者
       └──────┬───────┘
              │
    ┌─────────┼─────────┐
    │         │         │
    ▼         ▼         ▼
  stdout    file     webhook
```

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

class EventType(str, Enum):
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
    
    # 标签（用于过滤）
    tags: list[str] = field(default_factory=list)

@dataclass
class Event:
    """执行事件"""
    id: Optional[int] = None         # 自增 ID
    session_id: str = ""
    seq: int = 0                     # 顺序号
    type: EventType = EventType.STDOUT
    data: str = ""
    
    # 可选：向量嵌入
    embedding: Optional[list[float]] = None
    
    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)
    
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class WatchPattern:
    """监控模式"""
    pattern: str
    action: str = "notify"           # notify | callback | stop
    callback: Optional[str] = None   # webhook URL or function name

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
    
    # 监控
    watch_patterns: list[WatchPattern] = field(default_factory=list)
    
    # 环境变量
    env: dict[str, str] = field(default_factory=dict)
```

### 3.2 SQLite Schema

```sql
-- 基于 memorix 的存储设计

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
    tags TEXT DEFAULT '[]',
    
    -- 索引字段
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- 事件表（核心：流式写入）
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    embedding BLOB,  -- float32 数组，可选
    metadata TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    
    UNIQUE(session_id, seq)
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_tags ON sessions(tags);

CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

-- 向量搜索（可选，需要 sqlite-vec）
-- CREATE VIRTUAL TABLE IF NOT EXISTS events_vec USING vec0(
--     embedding float[1024]
-- );

-- 迁移版本追踪
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);
```

### 3.3 统一事件格式

所有接口层输出统一的事件格式：

```jsonl
// 会话开始
{"type":"session.start","session_id":"uuid","agent":"claude","prompt":"...","timestamp":1234567890}

// 标准输出
{"type":"stdout","session_id":"uuid","seq":1,"data":"...","timestamp":1234567890}

// 标准错误
{"type":"stderr","session_id":"uuid","seq":2,"data":"...","timestamp":1234567890}

// 系统事件
{"type":"system","session_id":"uuid","subtype":"init","data":{...},"timestamp":1234567890}

// 监控匹配
{"type":"watch","session_id":"uuid","pattern":"passed","line":"...","timestamp":1234567890}

// 会话结束
{"type":"session.end","session_id":"uuid","status":"completed","exit_code":0,"duration_ms":5000,"cost_usd":0.15,"timestamp":1234567890}

// 错误
{"type":"error","session_id":"uuid","code":"TIMEOUT","message":"...","timestamp":1234567890}
```

---

## 4. 核心组件设计

### 4.1 Executor — 流式进程执行器

**关键设计**：
- 使用 `asyncio.create_subprocess_exec` + `limit=8MiB` 避免 64KiB 单行限制
- stdout/stderr 并发读取，防止 pipe 满卡死
- 批量写入 SQLite（100 条或 100ms flush 一次）
- 支持中断、超时、崩溃恢复

```python
# executor.py

import asyncio
import time
from typing import AsyncIterator, Optional

# 8MiB per line — 避免 asyncio StreamReader 默认 64KiB 限制
_STREAM_LIMIT = 8 * 1024 * 1024

class StreamExecutor:
    """流式 subprocess 执行器"""
    
    def __init__(self, store: StorageBackend, config: ExecutionConfig):
        self.store = store
        self.config = config
        self._process: Optional[asyncio.subprocess.Process] = None
    
    async def execute(
        self,
        session_id: str,
        command: list[str],
        workdir: str,
        env: dict[str, str] = None,
    ) -> AsyncIterator[Event]:
        """执行命令，流式返回事件"""
        
        # 启动子进程
        self._process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
            env=env,
        )
        
        # 更新 session
        await self.store.update_session(
            session_id,
            pid=self._process.pid,
            status=SessionStatus.RUNNING,
            started_at=datetime.now(),
        )
        
        # stderr 并发 drain → 防止 pipe 满
        stderr_task = asyncio.create_task(
            self._drain_stderr(session_id, self._process.stderr)
        )
        
        # stdout 流式读取 → 批量写入
        seq = 0
        buffer: list[Event] = []
        last_flush = time.time()
        
        try:
            async for line in self._process.stdout:
                seq += 1
                event = Event(
                    session_id=session_id,
                    seq=seq,
                    type=EventType.STDOUT,
                    data=line.decode(),
                )
                
                # 写入 buffer
                buffer.append(event)
                
                # 批量 flush
                if len(buffer) >= 100 or time.time() - last_flush >= 0.1:
                    await self.store.append_events(buffer)
                    buffer.clear()
                    last_flush = time.time()
                
                # 实时 yield
                yield event
                
                # 检查 watch patterns
                await self._check_watch_patterns(session_id, event)
        
        finally:
            # 确保所有事件落盘
            if buffer:
                await self.store.append_events(buffer)
            
            # 等待 stderr 完成
            await stderr_task
            
            # 等待进程退出
            exit_code = await self._process.wait()
            
            # 更新 session
            await self.store.update_session(
                session_id,
                status=SessionStatus.COMPLETED if exit_code == 0 else SessionStatus.FAILED,
                exit_code=exit_code,
                finished_at=datetime.now(),
            )
            
            yield Event(
                session_id=session_id,
                seq=seq + 1,
                type=EventType.RESULT,
                data=f"{{\"exit_code\":{exit_code}}}",
            )
    
    async def _drain_stderr(
        self,
        session_id: str,
        stderr: asyncio.StreamReader,
    ):
        """并发读取 stderr，防止 pipe 满"""
        seq = 0
        async for line in stderr:
            seq += 1
            event = Event(
                session_id=session_id,
                seq=1000000 + seq,  # stderr 用大序号避免冲突
                type=EventType.STDERR,
                data=line.decode(),
            )
            await self.store.append_event(event)
    
    async def _check_watch_patterns(self, session_id: str, event: Event):
        """检查监控模式"""
        for pattern in self.config.watch_patterns:
            if pattern.pattern in event.data:
                # 触发 action
                if pattern.action == "notify":
                    # 发送通知
                    pass
                elif pattern.action == "stop":
                    # 停止执行
                    await self.kill(session_id)
    
    async def kill(self, session_id: str):
        """终止进程"""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
```

### 4.2 Agent — 适配器模式

**设计**：每个 agent 类型实现统一接口，封装命令构建和输出解析。

```python
# agents/base.py

from abc import ABC, abstractmethod
from typing import AsyncIterator

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

### 4.3 Storage — Protocol 设计

**设计**：基于 memorix 的 `StorageBackend` Protocol，支持多种实现。

```python
# storage/base.py

from typing import Protocol, runtime_checkable, AsyncIterator

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
        limit: int = 100,
    ) -> list[Session]:
        """列出会话"""
        ...
    
    # Event 操作
    async def append_event(self, event: Event) -> int:
        """追加事件，返回 event_id"""
        ...
    
    async def append_events(self, events: list[Event]) -> list[int]:
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
        """搜索事件（全文或向量）"""
        ...

# storage/sqlite.py

import aiosqlite
import json
from pathlib import Path

class SQLiteStorage:
    """SQLite 存储实现"""
    
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._batch_buffer: list[tuple] = []
    
    async def initialize(self) -> None:
        """初始化数据库"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._conn = await aiosqlite.connect(str(self.db_path))
        
        # 性能优化 PRAGMA
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA mmap_size=268435456")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        
        # 创建表
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()
    
    async def close(self) -> None:
        """关闭连接"""
        if self._conn:
            await self._conn.close()
    
    async def create_session(self, session: Session) -> str:
        """创建会话"""
        await self._conn.execute(
            """INSERT INTO sessions 
               (id, agent, prompt, workdir, status, metadata, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                session.agent.value,
                session.prompt,
                session.workdir,
                session.status.value,
                json.dumps(session.metadata),
                json.dumps(session.tags),
            )
        )
        await self._conn.commit()
        return session.id
    
    async def append_events(self, events: list[Event]) -> list[int]:
        """批量追加事件"""
        rows = [
            (
                e.session_id,
                e.seq,
                e.type.value,
                e.data,
                json.dumps(e.metadata),
            )
            for e in events
        ]
        
        await self._conn.executemany(
            """INSERT INTO events (session_id, seq, type, data, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            rows
        )
        await self._conn.commit()
        
        # 返回 ID（简化：假设自增）
        return list(range(1, len(events) + 1))
    
    async def stream_events(
        self,
        session_id: str,
        after_seq: int = 0,
    ) -> AsyncIterator[Event]:
        """流式读取事件"""
        async with self._conn.execute(
            """SELECT id, session_id, seq, type, data, metadata, created_at
               FROM events
               WHERE session_id = ? AND seq > ?
               ORDER BY seq""",
            (session_id, after_seq)
        ) as cursor:
            async for row in cursor:
                yield Event(
                    id=row[0],
                    session_id=row[1],
                    seq=row[2],
                    type=EventType(row[3]),
                    data=row[4],
                    metadata=json.loads(row[5]),
                    created_at=datetime.fromtimestamp(row[6]),
                )
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

# 搜索历史
coding-agent search "重构" --agent claude --last 7d

# 列出会话
coding-agent list --agent claude --status completed --limit 20

# 回放事件
coding-agent replay <session_id> --from-seq 100

# 终止执行
coding-agent kill <session_id>
```

### 5.2 HTTP Server

```bash
# 启动服务
coding-agent serve --port 8080 --host 0.0.0.0
```

**API 端点**：

```
POST /api/v1/run
  Body: {"agent": "claude", "prompt": "...", "workdir": "...", "config": {...}}
  Response: {"session_id": "uuid"}

GET /api/v1/sessions
  Query: ?agent=claude&status=completed&limit=20
  Response: [{"id": "...", "agent": "claude", ...}]

GET /api/v1/sessions/:id
  Response: {"id": "...", "status": "running", ...}

GET /api/v1/sessions/:id/events
  Query: ?after_seq=100&stream=true
  Response: SSE stream or JSON array

DELETE /api/v1/sessions/:id
  Response: {"status": "killed"}

POST /api/v1/search
  Body: {"query": "重构", "agent": "claude", "limit": 20}
  Response: [{"session_id": "...", "event": "..."}]
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

# 查询历史
sessions = await agent.list_sessions(agent="claude", limit=10)

# 语义搜索
results = await agent.search_events("重构", agent="claude", limit=20)
```

### 5.4 Node.js SDK

```javascript
import { CodingAgent } from 'coding-agents';

const agent = new CodingAgent({
  dbPath: '~/.coding-agents/data.db',
});

// Promise
const session = await agent.run({
  agent: 'claude',
  prompt: '重构这个函数',
  workdir: '~/project',
});

// 流式
for await (const event of agent.runStream({
  agent: 'claude',
  prompt: '添加测试',
})) {
  if (event.type === 'stdout') {
    process.stdout.write(event.data);
  }
}

// HTTP 客户端模式
const agent = new CodingAgent({
  mode: 'http',
  url: 'http://localhost:8080',
});
```

---

## 6. 性能设计

### 6.1 内存优化

| 策略 | 实现 |
|------|------|
| **流式读取** | `async for line in proc.stdout`，不收集全量输出 |
| **批量写入** | 100 条或 100ms flush 一次，减少 IO |
| **滚动 buffer** | 内存只保留最近 1000 条事件 |
| **mmap** | SQLite `mmap_size=256MB`，减少 syscall |

### 6.2 IO 优化

| 策略 | 实现 |
|------|------|
| **WAL 模式** | 读写并发，写入不阻塞读取 |
| **异步 IO** | `aiosqlite` 全异步，不阻塞事件循环 |
| **并发 drain** | stderr 单独 task，防止 pipe 满 |
| **索引优化** | 复合索引 + 部分索引，加速查询 |

### 6.3 并发控制

| 策略 | 实现 |
|------|------|
| **进程池** | 限制并发 agent 数量（默认 5） |
| **队列** | 超出限制的任务排队 |
| **超时** | 全局超时 + 空闲超时 |
| **资源限制** | 内存、预算、时间 |

### 6.4 性能指标

| 场景 | 目标 |
|------|------|
| **短任务 (<1min)** | <100ms 启动延迟，<10MB 内存 |
| **长任务 (30min)** | 恒定 <50MB 内存，零丢失 |
| **10 并发** | <200MB 内存，无锁竞争 |
| **100 万事件** | <1s 查询延迟（有索引） |
| **语义搜索** | <500ms（1024 维向量） |

---

## 7. 扩展机制

### 7.1 插件化 Agent

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

### 7.2 自定义存储后端

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

### 7.3 事件钩子

```python
# 注册事件回调
agent.on("session.start", lambda e: print(f"Started: {e.session_id}"))
agent.on("session.end", lambda e: send_notification(e))
agent.on("watch.match", lambda e: trigger_webhook(e))

# 或使用装饰器
@agent.hook("watch.match", pattern="ERROR")
async def on_error(event):
    await send_alert(event.data)
```

---

## 8. 实现计划

### Phase 1: 核心（2 周）

| 任务 | 预计 |
|------|------|
| 数据模型 + SQLite schema | 2d |
| StreamExecutor（流式执行器） | 3d |
| ClaudeAgent + CodexAgent | 2d |
| SQLiteStorage | 2d |
| CLI 基础命令 | 1d |

### Phase 2: 接口（1 周）

| 任务 | 预计 |
|------|------|
| HTTP Server (FastAPI) | 2d |
| Python SDK | 1d |
| stdio JSON 模式 | 1d |
| 文档 | 1d |

### Phase 3: 高级功能（1 周）

| 任务 | 预计 |
|------|------|
| 向量搜索（sqlite-vec） | 2d |
| 会话恢复 | 1d |
| 多 agent 编排 | 1d |
| 性能测试 | 1d |

### Phase 4: 生态（持续）

| 任务 | 预计 |
|------|------|
| Node.js SDK | 3d |
| OpenClaw 集成 | 2d |
| Hermes tool 集成 | 1d |
| Web UI | 3d |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| **subprocess 崩溃** | 丢失输出 | WAL + 批量写入，最多丢 100ms |
| **pipe 满卡死** | 进程挂起 | stderr 并发 drain |
| **内存爆炸** | OOM | 流式读取 + 滚动 buffer |
| **SQLite 锁竞争** | 性能下降 | WAL 模式 + busy_timeout |
| **agent CLI 变更** | 解析失败 | 版本检测 + 适配器模式 |
| **向量搜索慢** | 查询延迟 | 限制 top_k + 索引优化 |

---

## 10. 成功标准

| 指标 | 目标 |
|------|------|
| **功能** | 支持 Claude Code + Codex，CLI/HTTP/SDK |
| **性能** | 10 并发，30min 长任务，<50MB 内存 |
| **可靠性** | 零数据丢失，崩溃恢复 |
| **集成** | Hermes + OpenClaw 验证通过 |
| **文档** | API 文档 + 使用示例 |

---

## 附录

### A. 参考资料

- [memorix 架构](../../memorix/AGENTS.md)
- [Claude Code CLI 对比](./cli-comparison.md)
- [Buffer Size 调研](./research/buffer-size-investigation.md)

### B. 术语表

| 术语 | 定义 |
|------|------|
| **Session** | 一次 agent 执行会话 |
| **Event** | 会话中的一条事件（stdout/stderr/system） |
| **Stream** | 实时流式输出 |
| **WAL** | Write-Ahead Logging，SQLite 并发模式 |
| **mmap** | 内存映射文件，减少 syscall |

### C. 变更日志

- 2026-06-20: v1.0.0 初始设计
