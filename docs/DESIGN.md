# Coding Agent Runtime — 完整设计文档

> **版本**: v1.2.0  
> **日期**: 2026-06-20  
> **状态**: Draft (Revised — P0/P1 修复)

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

### 1.3 使用场景

| 场景 | 示例 |
|------|------|
| **Hermes 集成** | 作为 tool 调用，流式输出到 CLI |
| **OpenClaw 集成** | 通过 HTTP API 调用，SSE 推送事件 |
| **CI/CD 管道** | 批量执行任务，结果写入数据库 |
| **多 Agent 编排** | 并行执行多个 agent，统一监控 |
| **历史搜索** | 全文搜索过去的执行记录 |

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
│  Stream Executor  │  Agent Adapters  │  Process Pool    │
├─────────────────────────────────────────────────────────┤
│                    Storage Layer                         │
│  StorageBackend (Protocol)  │  SQLite  │  Postgres      │
└─────────────────────────────────────────────────────────┘
```

**v1.0 不包含**（后续版本考虑）：
- ~~Orchestrator 层~~ — 初期不需要复杂的会话编排
- ~~Event Bus~~ — 简单回调足够
- ~~Embedding Layer~~ — 向量搜索作为可选插件，不是核心层
- ~~Node.js SDK~~ — HTTP API 足够，推迟
- ~~Web UI~~ — 独立项目，不耦合
- ~~stdio JSON 模式~~ — 推迟到 Phase 3

### 2.2 核心组件

| 组件 | 职责 | 关键特性 |
|------|------|---------|
| **Interface** | 对外接口 | CLI/HTTP/SDK，统一事件格式 |
| **Executor** | 进程执行 | 流式 subprocess、可配置 buffer、背压控制 |
| **Agent** | Agent 适配 | 命令构建、输出解析、成本提取 |
| **Storage** | 数据持久化 | 批量写入、WAL 模式、全文搜索 |

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
    
    # 监控
    watch_patterns: list[str] = field(default_factory=list)
    
    # 输出模式
    output_mode: str = "standard"    # "passthrough" | "standard"
    
    # 模型（v1.2 新增）
    model: Optional[str] = None
    
    # 行长度限制（v1.2 新增，默认 8MiB）
    line_limit: int = 8 * 1024 * 1024
    
    # 环境变量
    env: dict[str, str] = field(default_factory=dict)
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
- `RUNNING → TIMEOUT`: 超过 timeout_seconds
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
    last_heartbeat_at REAL,  -- v1.2 新增：用于检测 orphaned
    
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

-- v1.2 新增：tags 关联表（替代 JSON 字段）
CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);

-- 事件表（核心：流式写入）
-- v1.2 修改：channel + seq 复合主键，seq 全局单调递增
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

-- v1.2 新增：全文搜索索引
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
// 会话开始
{"type":"session.start","session_id":"uuid","agent":"claude","prompt":"...","timestamp":1234567890}

// 标准输出（提取文本内容）
{"type":"stdout","session_id":"uuid","channel":"stdout","seq":1,"data":"...","timestamp":1234567890}

// 标准错误
{"type":"stderr","session_id":"uuid","channel":"stderr","seq":2,"data":"...","timestamp":1234567890}

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

// Codex --json 原样输出
{"type":"thread.started","thread_id":"..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"agent_message","text":"Hi!"}}
{"type":"turn.completed","usage":{"input_tokens":100}}
```

**使用场景**：
- **标准化模式**：简单集成、日志收集、监控
- **透传模式**：需要完整信息（如 thinking tokens、tool calls）

---

## 4. 核心组件设计

### 4.1 Executor — 流式进程执行器

**关键设计**：
- 使用 `asyncio.create_subprocess_exec` + 可配置 `limit`（默认 8MiB）
- stdout/stderr 并发读取，防止 pipe 满卡死
- **全局单调递增 seq**（跨 channel），用 `asyncio.Lock` 保证唯一
- **先 write 再 yield**，确保 storage 是 durable 真相
- **背压控制**：buffer 超过阈值时暂停读取

```python
# executor.py

import asyncio
import time
from typing import AsyncIterator, Optional

class SeqCounter:
    """全局单调递增序号计数器"""
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
    
    async def execute(
        self,
        session_id: str,
        command: list[str],
        workdir: str,
        env: dict[str, str] = None,
    ) -> AsyncIterator[Event]:
        """执行命令，流式返回事件"""
        
        # 启动子进程（line_limit 可配置）
        self._process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.config.line_limit,
            env=env,
        )
        
        # 更新 session
        await self.store.update_session(
            session_id,
            pid=self._process.pid,
            status=SessionStatus.RUNNING,
            started_at=datetime.now(),
            last_heartbeat_at=datetime.now(),
        )
        
        # stderr 并发 drain → 防止 pipe 满
        stderr_task = asyncio.create_task(
            self._drain_stderr(session_id, self._process.stderr)
        )
        
        # stdout 流式读取
        try:
            async for line in self._process.stdout:
                # 更新 heartbeat
                await self.store.update_session(
                    session_id, last_heartbeat_at=datetime.now()
                )
                
                seq = await self._seq.next()
                event = Event(
                    session_id=session_id,
                    channel="stdout",
                    seq=seq,
                    type=EventType.STDOUT,
                    data=line.decode(),
                    raw_json=line.decode() if self.config.output_mode == "passthrough" else None,
                )
                
                # 先写入 storage（durable）
                self._buffer.append(event)
                await self._flush_if_needed()
                
                # 再 yield（best-effort 实时）
                yield event
                
                # 检查 watch patterns
                await self._check_watch_patterns(session_id, event)
        
        finally:
            # 确保所有事件落盘
            await self._flush()
            
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
            
            seq = await self._seq.next()
            yield Event(
                session_id=session_id,
                channel="system",
                seq=seq,
                type=EventType.RESULT,
                data=f"{{\"exit_code\":{exit_code}}}",
            )
    
    async def _drain_stderr(
        self,
        session_id: str,
        stderr: asyncio.StreamReader,
    ):
        """并发读取 stderr，防止 pipe 满"""
        async for line in stderr:
            seq = await self._seq.next()
            event = Event(
                session_id=session_id,
                channel="stderr",
                seq=seq,
                type=EventType.STDERR,
                data=line.decode(),
            )
            # 先写入，不 yield（stderr 通常不需要实时消费）
            self._buffer.append(event)
            await self._flush_if_needed()
    
    async def _flush_if_needed(self):
        """批量 flush：100 条或 100ms"""
        if len(self._buffer) >= 100 or time.time() - self._last_flush >= 0.1:
            await self._flush()
    
    async def _flush(self):
        """写入 storage"""
        if self._buffer:
            await self.store.append_events(self._buffer)
            self._buffer.clear()
            self._last_flush = time.time()
    
    async def _check_watch_patterns(self, session_id: str, event: Event):
        """检查监控模式"""
        for pattern in self.config.watch_patterns:
            if pattern in event.data:
                # 触发回调（如果有）
                pass
    
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
        
        # 模型（v1.2 修复）
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
        
        # 模型（v1.2 修复）
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
        limit: int = 100,
    ) -> list[Session]:
        """列出会话"""
        ...
    
    # Event 操作
    async def append_event(self, event: Event) -> int:
        """追加事件，返回 event_id"""
        ...
    
    async def append_events(self, events: list[Event]) -> None:
        """批量追加事件（v1.2 修复：返回 None，不返回假 ID）"""
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
    
    # v1.2 新增：崩溃恢复
    async def recover_orphaned_sessions(self, timeout_seconds: int = 300) -> int:
        """扫描并标记 orphaned sessions，返回标记数量"""
        ...
```

### 4.4 崩溃恢复（v1.2 新增）

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

# 终止执行
coding-agent kill <session_id>

# 恢复 orphaned sessions
coding-agent recover
```

### 5.2 HTTP Server

```bash
# 启动服务（v1.2 修复：默认绑定 127.0.0.1）
coding-agent serve --port 8080 --host 127.0.0.1

# 生产部署（需要认证）
coding-agent serve --port 8080 --host 0.0.0.0 --auth-token <token>
```

**API 端点**：

```
POST /api/v1/run
  Body: {"agent": "claude", "prompt": "...", "workdir": "...", "config": {...}}
  Response: {"session_id": "uuid"}
  Auth: Bearer token

GET /api/v1/sessions
  Query: ?agent=claude&status=completed&limit=20
  Response: [{"id": "...", "agent": "claude", ...}]
  Auth: Bearer token

GET /api/v1/sessions/:id
  Response: {"id": "...", "status": "running", ...}
  Auth: Bearer token

# v1.2 修复：SSE 流式传输，支持断点续传
GET /api/v1/sessions/:id/events/stream
  Query: ?after_seq=100
  Header: Accept: text/event-stream
  Header: Last-Event-ID: 100  (断点续传)
  Response: SSE stream
  Auth: Bearer token

# v1.2 修复：终止执行（语义明确）
POST /api/v1/sessions/:id/kill
  Response: {"status": "killed"}
  Auth: Bearer token

# 删除记录（级联删除 events）
DELETE /api/v1/sessions/:id
  Response: {"deleted": true}
  Auth: Bearer token

POST /api/v1/search
  Body: {"query": "重构", "agent": "claude", "limit": 20}
  Response: [{"session_id": "...", "event": "..."}]
  Auth: Bearer token

GET /health
  Response: {"status": "ok", "sqlite": "ok", "running_sessions": 3}
```

**SSE 事件格式**：
```
event: stdout
id: 1
data: {"session_id":"uuid","seq":1,"data":"...","timestamp":1234567890}

event: stderr
id: 2
data: {"session_id":"uuid","seq":2,"data":"...","timestamp":1234567890}

event: session.end
id: 3
data: {"session_id":"uuid","status":"completed","exit_code":0}
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

# 全文搜索
results = await agent.search_events("重构", agent="claude", limit=20)

# 恢复 orphaned
count = await agent.recover_orphaned()
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
| **背压控制** | buffer 超过阈值时暂停读取，防止内存爆炸 |
| **mmap** | SQLite `mmap_size=256MB`，减少 syscall |

### 7.2 IO 优化

| 策略 | 实现 |
|------|------|
| **WAL 模式** | 读写并发，写入不阻塞读取 |
| **异步 IO** | `aiosqlite` 全异步，不阻塞事件循环 |
| **并发 drain** | stderr 单独 task，防止 pipe 满 |
| **索引优化** | 复合索引 + 部分索引，加速查询 |
| **FTS5** | 全文搜索虚拟表，加速文本搜索 |

### 7.3 并发限制（明确声明）

| 场景 | 限制 | 原因 |
|------|------|------|
| **写入并发** | SQLite 写锁全局 | 写写串行，WAL 只解决读写并发 |
| **建议并发数** | ≤5 个 agent 同时执行 | 避免写竞争 |
| **查询并发** | 无限制 | WAL 模式下读写不冲突 |

**如果高并发写入是瓶颈**：
- 方案 1: 写入队列（异步批量）
- 方案 2: 分库（按 agent 或日期）
- 方案 3: 换 Postgres（真正的并发写入）

### 7.4 性能指标

| 场景 | 目标 | 说明 |
|------|------|------|
| **短任务 (<1min)** | <100ms 启动延迟，<10MB 内存 | |
| **长任务 (30min)** | 恒定 <50MB 内存，零丢失 | 背压控制 |
| **5 并发** | <100MB 内存，可接受写延迟 | |
| **100 万事件** | <1s 查询延迟（有索引） | 仅限索引查询 |
| **全文搜索** | <2s（FTS5） | 取决于数据量 |

**注意**：Phase 1 结束后需做基准测试验证这些指标。

---

## 8. 安全设计（v1.2 新增）

### 8.1 网络安全

| 措施 | 说明 |
|------|------|
| **默认绑定 localhost** | `--host 127.0.0.1`，仅本地访问 |
| **Bearer token 认证** | `--auth-token <token>`，所有 API 需要认证 |
| **生产部署** | 必须配置 `--host 0.0.0.0` + `--auth-token` |

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

### 9.3 事件回调（v1.2 简化）

```python
# 简单回调注册（不用装饰器）
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
| StreamExecutor（流式执行器） | 4d | 含全局 seq、背压、先 write 再 yield |
| ClaudeAgent + CodexAgent | 2d | 命令构建、输出解析 |
| SQLiteStorage | 3d | Protocol 实现、崩溃恢复 |
| CLI 基础命令 | 2d | run/status/list/search/kill/recover |
| 单元测试 | 3d | 覆盖核心逻辑 |
| 基准测试 | 1d | 验证性能指标 |
| **小计** | **17d** | 含 buffer |

### Phase 2: 接口（2 周）

| 任务 | 预计 | 说明 |
|------|------|------|
| HTTP Server (FastAPI + SSE) | 4d | 含认证、断点续传 |
| Python SDK | 2d | 封装 HTTP 或直接调用 |
| 集成示例 | 2d | Hermes + OpenClaw |
| 文档 | 2d | API 文档 + 示例 |
| 集成测试 | 2d | |
| **小计** | **12d** | |

### Phase 3: 高级功能（持续）

| 任务 | 预计 | 说明 |
|------|------|------|
| 向量搜索（sqlite-vec） | 3d | 可选 |
| stdio JSON 模式 | 2d | |
| Node.js SDK | 3d | HTTP-only client |
| 会话恢复（执行续跑） | 5d | 需要 agent CLI 支持 |
| Web UI | 5d | 独立项目 |

**总计**：Phase 1-2 约 6-7 周（含 buffer）

---

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| **subprocess 崩溃** | 丢失输出 | WAL + 批量写入，最多丢 100ms |
| **pipe 满卡死** | 进程挂起 | stderr 并发 drain |
| **内存爆炸** | OOM | 流式读取 + 背压控制 |
| **SQLite 写竞争** | 性能下降 | 限制并发数 ≤5，或换 Postgres |
| **agent CLI 变更** | 解析失败 | 版本检测 + 适配器模式 |
| **长任务超时** | 执行中断 | 合理设置 timeout，heartbeat 检测 |
| **runtime 崩溃** | session 悬挂 | 启动时扫描 orphaned，标记为 ORPHANED |

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
- [设计评审 - Claude](./DESIGN-REVIEW-CLAUDE.md)

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
