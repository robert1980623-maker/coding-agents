# Buffer Size 调研 — Claude Code / Codex

## 问题
Claude Code 和 Codex 在执行长任务时可能被 kill，原因是 subprocess buffer 限制。

## 结论

**核心发现：不是靠配置 maxBufferSize，而是靠流式读取模式绕开限制。**

### 1. Python Runtime (local_runtime/executor.py)

**问题**: asyncio `StreamReader` 默认 64KiB 单行限制，单行超长会报错。

**解决方案**:
```python
# 定义 8MiB 单行限制
_STREAM_LIMIT = 8 * 1024 * 1024  # 8 MiB per line

# Claude 调用
create_subprocess_exec(..., limit=_STREAM_LIMIT, stdout=PIPE, stderr=STDOUT, ...)
# 然后 async for raw_line in proc.stdout 流式消费

# Codex 调用
create_subprocess_exec(..., limit=_STREAM_LIMIT, ...)
# stdout/stderr 分开 pipe，流式读取
```

**关键代码位置**:
- `_STREAM_LIMIT` 定义: `executor.py:40-43`
- Claude: `executor.py:2728-2739` (spawn), `2760-2785` (流式读)
- Codex: `executor.py:3069-3077` (spawn), `3083-3087` (stderr drain)

### 2. Rust Runtime v2 (runtime-client-v2)

**策略**: 不靠调 maxBufferSize，而是从模式上绕开整块 buffer 限制。

**Claude (Rust)**:
```rust
// runtime-client-v2/src/executor/claude.rs:921+
let mut child = cmd.spawn()?;
let stdout = child.stdout.take().unwrap();
let stderr = child.stderr.take().unwrap();

// stdout 按行读
let mut reader = BufReader::new(stdout);
while let Some(line) = reader.next_line().await? {
    writer.push(line);
}

// stderr 单独 task drain，防止 pipe 满卡死
tokio::spawn(async move {
    let mut stderr_reader = BufReader::new(stderr);
    // ... drain stderr
});
```

**Codex (Rust)**:
```rust
// runtime-client-v2/src/executor/codex.rs:678+
let mut child = cmd.spawn()?;
// 同样的 spawn + BufReader.lines() 模式
// stdout/stderr 分开并发读取
```

**关键代码位置**:
- Claude: `claude.rs:995-996` (stdout), `974-993` (stderr drain), `1001-1098` (边读边推送)
- Codex: `codex.rs:711-717` (stderr drain), `719-720` (stdout), `723-764` (normalize/push)

## 一句话总结

| Runtime | 策略 | 关键点 |
|---------|------|--------|
| **Python** | 调大 `StreamReader` limit | `limit=8MiB` + 流式读取 |
| **Rust v2** | 流式消费，不用整块收集 | `spawn()` + `BufReader.lines()` + stderr 并发 drain |

**本质**: 不走 `exec/output/communicate` 一次性收集模式，而是 spawn + pipe + streaming 逐行消费。

## 对我们的启示

如果我们自己的 runtime 要调用 Claude Code / Codex：
1. **不要用** `subprocess.run()` 或 `communicate()` 整块收集
2. **用** `Popen` + 逐行读取 stdout/stderr
3. **Python asyncio**: 设置 `limit=8*1024*1024` 避免 64KiB 单行限制
4. **stderr 必须并发 drain**，否则 pipe 满会卡死子进程
