# Coding Agent Runtime — 设计评审 (Claude / Amenda 第二轮)

> **评审人**: Claude Code (架构师视角)，由 Amenda (PM) 协调  
> **日期**: 2026-06-20  
> **评审对象**: DESIGN.md v1.0.0 (Draft)  
> **互补关系**: 与 `DESIGN-REVIEW.md`（Atlas 评审，4/5）形成交叉验证视角

---

## 总体评价

**评分: 6.5 / 10**

文档结构清晰，核心数据流设计合理，性能优化思路正确。但作为 v1.0.0 定稿稿，存在若干**架构硬伤**和**关键设计缺失**，需修复后方可定稿。

> ⚠️ 与 Atlas 评审（4/5，偏正面）的分歧：本评审更侧重**实施可行性**，对架构硬伤和安全问题给出更严格结论。

---

## ✅ 核心优点

1. **流式优先设计正确** — `async for line in proc.stdout` + 批量 flush（100条/100ms）的 tradeoff 合理，避免内存爆炸和 IO 放大
2. **Protocol 鸭子类型** — 用 `Protocol` 替代 `ABC` 解耦存储后端，比继承更灵活
3. **SQLite PRAGMA 调优到位** — WAL、mmap_size=256MB、busy_timeout=5000 对并发读写场景足够
4. **stderr 并发 drain 设计** — 独立 `asyncio.Task` 防止 pipe 满，是 subprocess 管理的正确姿势
5. **统一 JSONL 事件协议** — 让四种接口层共享同一数据模型，降低一致性风险

---

## 🚨 必须修复的问题 P0

### P0-1：stderr seq=1000000+ 硬编码分流是架构硬伤

**问题**：
```python
seq=1000000 + seq,  # stderr 用大序号避免冲突
```
- Magic number `1000000` 无文档解释来源，若 stderr 超过 100 万条则与 stdout 冲突
- 破坏了 seq 的**时序语义**：无法判断 stderr 和 stdout event 的先后顺序
- 崩溃恢复从 `last_seq` 续跑时，无法判断 last_seq 属于 stdout 还是 stderr

**建议**：改用 `(channel, seq)` 复合主键，或引入全局 monotonic counter + channel 字段：

```python
@dataclass
class Event:
    channel: str  # "stdout" | "stderr" | "system"
    seq: int      # 每个 channel 独立单调递增
```

---

### P0-2：Session.tags JSON 存储 + 索引 = 无效索引

**问题**：
```sql
tags TEXT DEFAULT '[]',
CREATE INDEX idx_sessions_tags ON sessions(tags);  -- 无效！
```
SQLite 对 JSON 数组字段建 B-tree 索引**毫无作用** — `WHERE tags LIKE '%重构%'` 无法走索引，`json_extract` 也无法利用该索引。按 tag 过滤 session 会退化为全表扫描。

**建议**：拆成关联表：

```sql
CREATE TABLE session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX idx_session_tags_tag ON session_tags(tag);
```

---

### P0-3：崩溃恢复只字未提实现方案

**问题**：文档声称支持「崩溃恢复」，但全篇无一行实现代码：
- 进程被 `kill -9` 后，如何检测到崩溃？（需要 supervisor/watchdog）
- `last_seq` 存哪里？（内存？SQLite？文件系统？）
- 续跑时 agent CLI 是否支持 `--resume`？**Claude Code / Codex 均不支持**
- 未完成 session 的状态如何从 RUNNING 转为 FAILED？

**建议**：
- **明确边界**：v1.0 只支持「状态恢复」（标记为 FAILED），**不支持「执行续跑」**
- 加 heartbeat 字段：`last_heartbeat_at REAL`，supervisor 定期检查超时 session
- 文档必须明示这是设计边界，避免误导用户

---

### P0-4：stream_events 流式传输协议未定义

**问题**：HTTP API 写了 `?stream=true`，响应是「SSE stream or JSON array」，但：
- SSE vs WebSocket 没选型依据
- 语义完全不同：SSE 单向推送，WebSocket 双向通信
- 断线重连机制没有设计

**建议**：选 **SSE**（单向推送足够，HTTP 原生支持，断线重连有标准 `Last-Event-ID`）：
- 明确端点：`GET /api/v1/sessions/:id/events/stream`
- Header：`Accept: text/event-stream`
- 断点续传：客户端通过 `Last-Event-ID` header 续读

---

### P0-5：鉴权/认证完全缺失（RCE 风险）

**问题**：HTTP Server 绑定 `0.0.0.0:8080`，无任何认证。任何能访问该端口的进程/用户可以：
- 执行任意 agent 命令（**RCE 风险**）
- 读取所有历史 session 数据
- 终止他人的 session

**建议**：
- **最低限度**：绑定 `127.0.0.1`，仅本地访问
- **生产部署**：Bearer token 认证（API key），或 Unix socket 模式
- **长期**：OAuth2 / mTLS，与 Hermes/OpenClaw 认证体系打通

---

## 🟡 建议改进 P1

### P1-1：8MiB stream limit 硬编码
8MiB 是 asyncio StreamReader 的 buffer limit，但绝大多数 agent 输出行不会超过 1KB。
- 文档注明 8MiB 来源（哪类 agent 会产生超长行？base64 图片？minified JS？）
- 提供 `line_limit` 配置项，允许按 agent 类型调整

### P1-2：滚动 buffer 1000 条设计未在代码中体现
文档声称「内存只保留最近 1000 条事件」，但 `StreamExecutor.execute()` 中**只有批量 flush**，没有滚动 buffer 实现。需要明确：
- 滚动 buffer 在哪个组件？（Executor？Event Bus？）
- 超出 1000 条后丢弃还是写磁盘？

### P1-3：100 万事件 <1s 查询需要限定查询类型
- `WHERE session_id = ? AND seq > ?` 走复合索引 ✅
- `WHERE data LIKE '%keyword%'` 全表扫描，100 万行可能 5-10s ❌
- 全文搜索需要 **FTS5 虚拟表**，文档未提及

### P1-4：embedding BLOB 大小无估算
1024 维 × 4 字节 = 4KB/条。100 万条 event 全量 embedding ≈ **4GB**。文档未说明：
- 哪些 event 需要 embedding？（全部？仅 stdout？仅 result？）
- 存储预算上限？
- 是否分表（events vs events_embedding）？

### P1-5：sqlite-vec 1024 维硬编码
硬编码 1024 维。若换 embedding 模型（OpenAI text-embedding-3-small = 1536 维、本地模型 = 384 维），schema 需要重建。建议：
- 增加 `embedding_dim` 配置项
- 向量表独立管理，不与 events 强耦合

### P1-6：状态机未形式化定义
六个状态 `PENDING → RUNNING → {COMPLETED, FAILED, KILLED, TIMEOUT}`，但合法转换未定义：
- TIMEOUT / KILLED 是终态还是中间态？
- 能否从 TIMEOUT 恢复到 RUNNING？
- FAILED 能否 retry 回到 RUNNING？

**建议**：画状态机图，明确每个转换的触发条件。

### P1-7：DELETE /sessions/:id 语义模糊
RESTful 中 DELETE 通常表示「删除资源」，但文档的语义是「终止执行」。
- 终止执行：`POST /sessions/:id/kill` 或 `POST /sessions/:id/cancel`
- 删除记录：`DELETE /sessions/:id`（需级联删除 events）

### P1-8：Event Bus 组件设计与代码脱节
架构图和数据流图都有 Event Bus，但**代码部分完全没有实现**。需要说明：
- Event Bus 是进程内 pub/sub 还是跨进程（Redis/NATS）？
- 多个消费者（stdout/file/webhook）如何注册？
- 背压策略？

### P1-9：WatchPattern pattern 匹配过于简单
```python
if pattern.pattern in event.data:  # 子串匹配
```
生产场景需要正则、JSONPath、结构化匹配。建议：
```python
pattern: str
match_type: str = "contains"  # contains | regex | jsonpath
```

### P1-10：多 agent 编排缺少依赖管理
文档提到「并行执行多个 agent」，但缺少：
- 依赖图（DAG）定义
- 超时传播（子 agent 超时，父 agent 如何处理？）
- 结果聚合策略

---

## ✂️ 可砍掉的过度设计

### 1. Node.js SDK 可推迟到 Phase 4 之后
v1.0 核心价值是 runtime 本身。Node.js 用户通过 HTTP API 调用即可，无需原生 SDK。

### 2. Web UI 不应出现在实现计划中
Web UI 是独立产品，不应与 runtime 耦合。建议只提供 HTTP API + OpenAPI spec，Web UI 作为独立项目。

### 3. 事件钩子装饰器语法是过度设计
```python
@agent.hook("watch.match", pattern="ERROR")
async def on_error(event):
    ...
```
v1.0 阶段，简单回调注册已足够。装饰器增加 API surface 但无实质功能提升。

---

## ❌ 遗漏的关键设计

### 1. 认证/授权（已在 P0-5 提及）
- API key / Bearer token
- 多租户隔离（不同用户看到不同 session）
- 权限控制（谁能 kill 谁的 session）

### 2. 监控与可观测性
- Metrics：session 数、event 写入速率、内存占用、查询延迟
- Health check：`GET /health` 返回 SQLite 状态、进程池状态
- Tracing：跨 agent 调用的 `trace_id` 传播

### 3. 日志规范
- runtime 自身日志（debug/info/warn/error）
- 日志输出格式（JSON？text？）
- 日志轮转策略

### 4. 部署方案
- 单进程 vs 多进程？
- SQLite 并发写入的锁竞争如何缓解？（多进程场景下 WAL 也不够）
- 是否需要进程管理器（systemd/supervisor）？

### 5. 数据清理与归档
- events 表会持续膨胀，需要 TTL 或归档策略
- 是否支持导出历史 session 到文件后删除？

### 6. 安全沙箱
- agent 执行可能修改文件系统，是否需要沙箱（Docker/nsjail）？
- 环境变量注入的安全风险（env 字段直接传给 subprocess）

---

## 📅 Phase 风险评估

| Phase | 周期 | 风险 | 关键问题 |
|-------|------|------|---------|
| **Phase 1** 核心 | 2 周 | 🟡 中 | StreamExecutor 最复杂；stderr drain 竞态、批量 flush 异常处理；**建议 +3-5 天 buffer** |
| **Phase 2** 接口 | 1 周 | 🔴 高 | 4 种接口（HTTP/SDK/stdio/文档）太紧；stdio JSON 设计不清；**建议砍掉 stdio 或推 Phase 3** |
| **Phase 3** 高级 | 1 周 | 🔴 极高 | 会话恢复 1 天不现实；多 agent 编排 1 天只能做最简单并行；**建议拆 Phase 3+5** |
| **Phase 4** 生态 | 持续 | 🟡 不确定 | 依赖外部团队；Web UI 应独立；**建议明确边界** |

---

## 🎯 v1.0.0 定稿结论：**不建议定稿**

文档作为「架构设计参考」是合格的，但作为「v1.0.0 实施稿」存在以下阻塞项：

1. **P0-1**（stderr seq 硬编码）和 **P0-2**（tags 索引无效）是数据模型硬伤，定稿后修改成本高
2. **P0-3**（崩溃恢复）和 **P0-4**（流式协议）是关键功能缺失，定稿后无法实施
3. **P0-5**（认证）是安全问题，必须在定稿前解决

### 建议路径

1. 修复 5 个 P0 问题（约 **3-5 天**）
2. 补充监控/日志/部署章节（约 **2 天**）
3. 重新评估 Phase 3 时间估算（会话恢复至少需要 1 周）
4. 状态改为 **v1.0.0-rc1**，P0 修完改为 **v1.0.0 Final**

---

## 📊 与 Atlas 评审对比

| 维度 | Atlas 评审 | Claude 评审（本文） | 说明 |
|------|-----------|-------------------|------|
| 总分 | 4/5 ★★★★☆ | 6.5/10 | 评分体系不同，本评审更严苛 |
| stderr seq | 碰撞风险 | **架构硬伤** | 严重性判断一致 |
| 崩溃恢复 | 提到 | **明确边界建议** | Claude 更具体 |
| 认证 | 提到 | **P0（安全必修）** | 严重性提升 |
| 性能目标 | 提到 | **拆解可达性** | Claude 更细 |
| 过度设计 | 未提 | 3 条 | 互补 |

> 两份评审可作为 v1.0.0-rc1 修订的完整输入。

---

*评审完成时间: 2026-06-20 14:16 GMT+8*  
*由 Amenda (项目经理) 协调派遣 Claude Code 完成*