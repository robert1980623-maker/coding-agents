# Phase 1 任务跟踪

**开始时间**: 2026-06-20 15:31 GMT+8
**总预计**: 19 天（CEO 决策：一次性做完，然后详细测试）

## 任务列表

### T1.1 项目骨架 + 数据模型 (1.5d)
- [ ] pyproject.toml + 项目目录结构
- [ ] `coding_agents/models.py` - Session, Event, ExecutionConfig, WatchPattern, AgentType, SessionStatus, EventType
- [ ] `coding_agents/storage/base.py` - StorageBackend Protocol

### T1.2 SQLiteStorage Protocol 实现 (3d)
- [ ] `coding_agents/storage/sqlite.py` - SQLiteStorage
- [ ] SQLite schema 初始化（来自 DESIGN.md 3.3）
- [ ] tags CRUD
- [ ] FTS5 全文搜索
- [ ] session_tags 关联表
- [ ] 崩溃恢复（ORPHANED 检测）

### T1.3 SessionRegistry (0.5d)
- [ ] `coding_agents/registry.py` - P0-NEW-1/2 修复版（semaphore 泄漏修复）

### T1.4 StreamExecutor (5d)
- [ ] `coding_agents/executor.py` - 流式 subprocess 执行器
- [ ] SeqCounter + 全局单调递增
- [ ] heartbeat 节流（1s）
- [ ] idle timeout watchdog
- [ ] session.start / error / result event（json.dumps 安全转义）
- [ ] stderr + stdout Queue 桥接（readers_remaining 计数器）
- [ ] finally 块状态机终态检查（P0-NEW-3）
- [ ] _flush 原子交换 buffer（P1-NEW-1）
- [ ] WatchPattern 实现（notify/callback/stop）

### T1.5 Agent 适配器 (2d)
- [ ] `coding_agents/agents/base.py` - BaseAgent ABC
- [ ] `coding_agents/agents/claude.py` - Claude Code 适配器
- [ ] `coding_agents/agents/codex.py` - Codex CLI 适配器
- [ ] `coding_agents/agents/factory.py` - Agent factory

### T1.6 CLI 基础命令 (2d)
- [ ] `coding_agents/cli.py` - typer CLI
- [ ] `run` 命令 - 启动 session
- [ ] `status` 命令 - 查看 session
- [ ] `list` 命令 - 列出会话
- [ ] `search` 命令 - FTS5 全文搜索
- [ ] `kill` 命令 - 终止 session
- [ ] `recover` 命令 - 扫描 ORPHANED
- [ ] `tag` 命令 - tags 管理

### T1.7 单元测试 (3d)
- [ ] `tests/test_models.py`
- [ ] `tests/test_storage.py`（含 sqlite-tmp 测试）
- [ ] `tests/test_registry.py`
- [ ] `tests/test_executor.py`（含 mock subprocess）
- [ ] `tests/test_agents.py`（含 mock CLI）
- [ ] `tests/test_cli.py`

### T1.8 集成测试 + 基准 (1d)
- [ ] `tests/integration/` 真实 claude/codex 调用
- [ ] 性能基准（10 并发、30min 长任务 <50MB）
- [ ] README 编写
- [ ] GitHub Release v0.1.0-alpha tag

## 进度更新

| 时间 | 任务 | 状态 | 备注 |
|------|------|------|------|
| 15:31 | 开始 | 🚀 | 派遣 Claude Code CLI |
| | | | |

## 风险

- T1.7 测试覆盖率需 ≥80%（性能/可靠性目标）
- 真实 Claude Code / Codex CLI 依赖外部安装（集成测试条件）
- Long task 19d 可能需要分批提交（每完成一个 T1.x 提交一次）

## 验收标准

按 DESIGN.md §12 成功标准：
- 支持 Claude Code + Codex，CLI
- 5 并发 30min 长任务 <50MB 内存
- 零数据丢失，崩溃恢复
- 默认 localhost，认证（Phase 2 才有 HTTP）
- 测试覆盖率 ≥80%