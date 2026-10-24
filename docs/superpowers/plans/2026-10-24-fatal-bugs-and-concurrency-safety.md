# doubao2api 致命 Bug 修复与并发安全 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复对比报告中实证的三个致命 bug（tools 请求 500、keepalive 截断流、幽灵方法 apply_account），并给账号切换/重启加上「在途流排空」护栏，使浏览器重载不再杀死所有进行中的请求。

**Architecture:** 全部改动落在既有文件的两层：`unified_server.py`（请求层：分支绑定、SSE 循环、换号路径）与 `browser_client.py`（客户端层：在途流计数 + 排空）。不改并发模型本身——仍是单 Context 单 Page，只是让破坏性操作（clear_cookies + reload）先等在途流结束。真·多 Context 并行明确推迟（见文末「不做的事」）。

**Tech Stack:** Python 3.10+ / FastAPI / Playwright async API / pytest（仓库现有栈，零新依赖）

## Global Constraints

- **行号基准**：本计划行号基于当前工作区（含未提交改动）。`doubao2api/unified_server.py`、`doubao2api/browser_client.py` 已有未提交修改，**开工第一步必须先把现有 WIP 单独提交**（见 Task 0），否则后续 commit 会混入无关改动。
- **测试命令**（本机 venv 无 pytest，用系统 Python）：
  `D:/python3132/python.exe -m pytest tests/ -q`
  基线：**53 passed, 1 failed**。唯一失败 `test_unit.py::test_ssrf_is_safe_url` 是环境问题（本机 DNS 把 www.doubao.com 解析到 fake-IP 段 198.18.1.98，被 SSRF 防护正确拦截），**不是回归，不要去修它**。
- **零新依赖**：只用标准库（asyncio/functools/inspect）与已装包。
- **最小根因修复**：所有改动打在所有调用方共同经过的位置（如 switch_to_account、_stream_chat 主循环），不在各调用点重复打补丁。
- **风格**：跟随仓库现状——中文日志/注释、logging 模块、4 空格缩进。
- **deliberate simplification 标注**：砍掉真实边角的简化处用 `ponytail:` 注释标注上限与升级路径。

## 背景：三个已验证的 bug（实现者需知）

1. **tools 请求必 500**：`unified_server.py` 的 `chat_completions` 中，`image_refs`/`file_refs` 只在非 tools 分支（约 :984）赋值，而 :1017 `if image_refs:` 无条件引用。任何带 `tools` 的请求第一步就 `UnboundLocalError`。Cline/Continue/LangChain 全灭。
2. **keepalive 反而截断流**：`unified_server.py:1847` 用 `asyncio.wait_for(event_iter.__anext__(), timeout=5.0)`。超时取消会关闭异步生成器（已在本机 Python 3.12/3.13 最小复现：`TIMEOUT -> keepalive；STOP -> 流在 0 个事件后终止`）。上游 5 秒无事件 → 客户端收到一个心跳 → 流当场结束。
3. **换号路径调用幽灵方法**：`unified_server.py:930` 调 `client.apply_account(next_acc)`，`BrowserClient` 上不存在该方法（真实方法是 `switch_to_account(account_id: str)`）。走到 Tier-3 即 `AttributeError` → 500。`tests/test_captcha_defense.py:126` 用 `AsyncMock` 凭空 mock 了这个方法，把 bug 藏在了绿灯后面。

---

### Task 0: 提交现有 WIP，清出干净基线

**Files:**
- 无新改动；只提交已存在的未提交内容。

**Interfaces:**
- Consumes: 无
- Produces: 干净工作区，后续每个 Task 的 commit 只含本 Task 改动。

- [ ] **Step 1: 确认当前未提交内容属于可提交状态**

```bash
cd F:/AI_APIS/doubao2api && git status --short && git diff --stat
```

预期：`.gitignore`、`README.md`、`doubao2api/__init__.py`、`doubao2api/browser_client.py`、`doubao2api/unified_server.py` 为 M，`doubao2api/_runtime.py` 未跟踪。内容为 Windows 运行时兼容（_runtime.py）与视频会话留存改动——这是用户既有工作，**原样提交，不审查不改写**。

- [ ] **Step 2: 提交为独立 WIP commit**

```bash
git add -A && git commit -m "wip: Windows 运行时兼容(_runtime)与视频会话留存/即焚清理改动"
```

- [ ] **Step 3: 跑测试确认基线**

Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `53 passed, 1 failed`（失败仅 `test_ssrf_is_safe_url`，环境问题）

---

### Task 1: 修复 tools 请求 UnboundLocalError（致命 #1）

**Files:**
- Modify: `doubao2api/unified_server.py:956-965`（chat_completions 的 tools 分支）
- Test: `tests/test_fatal_regressions.py`（新建）

**Interfaces:**
- Consumes: 既有 `_extract_prompt_and_file_refs(body.messages) -> tuple[str, list, list]`（`unified_server.py:325`）；既有 `image_attachments`/`file_attachments` 传参链（`_stream_chat` :1026-1032、非流式 tools 路径 :1041-1046 均已接收这两个参数，**无需改动下游**）。
- Produces: tools 分支绑定 `file_refs`/`image_refs`，附件透传给 `chat_completion`。

- [ ] **Step 1: 写失败测试（新建 tests/test_fatal_regressions.py，完整文件）**

```python
"""Regression tests for the three fatal bugs found in the 2026-10 comparison review:

1. tools requests crashed with UnboundLocalError (image_refs only bound in the
   non-tools branch) -> 500 on every Agent-framework request.
2. The SSE keepalive timeout closed the async generator and truncated the stream.
3. Captcha failover called client.apply_account(), a method that never existed.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from doubao2api.browser_client import BrowserClient
from doubao2api.unified_server import create_app


def _mock_client() -> MagicMock:
    m = MagicMock()
    m.needs_captcha = False
    m.is_ready = True
    m.extract_conversation_id = MagicMock(return_value=None)
    m.record_success = MagicMock()
    m.delete_conversation = AsyncMock(return_value=True)
    return m


def test_tools_request_plain_text_does_not_500():
    """Any request with `tools` used to 500 before a single upstream call."""
    m = _mock_client()

    async def fake_chat(*args, **kwargs):
        yield {"_event": "CHUNK_DELTA", "text": "ok"}

    m.chat_completion = fake_chat
    tc = TestClient(create_app(browser_client=m))
    resp = tc.post("/v1/chat/completions", json={
        "model": "doubao-expert",
        "stream": False,
        "tools": [{"type": "function", "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}],
        "messages": [{"role": "user", "content": "北京天气如何"}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "ok"


def test_tools_request_with_image_passes_attachments_through():
    """Cline sends screenshots as image_url alongside tools; they must be
    materialized and forwarded to chat_completion, not crash the branch."""
    m = _mock_client()
    captured = {}

    async def fake_chat(*args, **kwargs):
        captured["kwargs"] = kwargs
        yield {"_event": "CHUNK_DELTA", "text": "ok"}

    m.chat_completion = fake_chat
    m.upload_image = AsyncMock(return_value={"uri": "tos-x", "cdn_url": "", "name": "image.png",
                                             "width": 10, "height": 10})
    tc = TestClient(create_app(browser_client=m))
    resp = tc.post("/v1/chat/completions", json={
        "model": "doubao-expert",
        "stream": False,
        "tools": [{"type": "function", "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图里城市天气如何"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        ]}],
    })
    assert resp.status_code == 200, resp.text
    atts = captured["kwargs"].get("image_attachments")
    assert atts and len(atts) == 1
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py -v`
Expected: 两个测试 **FAIL**（500 + `"cannot access local variable 'image_refs'"` 之类的 detail）

- [ ] **Step 3: 最小实现**

`doubao2api/unified_server.py` tools 分支（约 :956-965），在 `prompt = convert_messages_with_tools(...)` 之后加两行：

```python
        if has_tools:
            requested_deep_think = CHAT_MODELS["doubao-expert"]
            use_deep_think, model_name = _expert_tracker.get_effective_mode(requested_deep_think)
            messages_raw = [m.model_dump(exclude_none=True) for m in body.messages]
            prompt = convert_messages_with_tools(messages_raw, body.tools)
            # Attach images/files sent alongside tool requests (Cline/Continue
            # screenshots); the text prompt itself is built above.
            _, file_refs, image_refs = _extract_prompt_and_file_refs(body.messages)
        else:
```

else 分支保持原样（:984 的 `prompt, file_refs, image_refs = ...` 不动）。

- [ ] **Step 4: 运行验证测试通过**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py -v`
Expected: `2 passed`

- [ ] **Step 5: 提交**

```bash
git add tests/test_fatal_regressions.py doubao2api/unified_server.py
git commit -m "fix(tools): 修复带 tools 的请求因 image_refs 未绑定而 500，并透传图片/文件附件"
```

---

### Task 2: 修复 keepalive 截断 SSE 流（致命 #2）

**Files:**
- Modify: `doubao2api/unified_server.py`（两处：模块常量区 ~:106；`_stream_chat` 主循环 ~:1836-1853 与收尾 ~:2094-2097）
- Test: `tests/test_fatal_regressions.py`（追加）

**Interfaces:**
- Consumes: 既有 `event_iter = client.chat_completion(...).__aiter__()`（:1837-1843）。
- Produces: 模块常量 `_KEEPALIVE_INTERVAL: float`（默认 5.0，env `DOUBAO_KEEPALIVE_INTERVAL` 可调；测试通过 monkeypatch `doubao2api.unified_server._KEEPALIVE_INTERVAL` 缩短）。下游事件处理代码（:1855 起）**零改动**——只换取事件的等待方式。

- [ ] **Step 1: 写失败测试（追加到 tests/test_fatal_regressions.py）**

```python
def test_stream_survives_idle_gap_with_keepalive(monkeypatch):
    """asyncio.wait_for(__anext__, timeout) cancels and CLOSES the async
    generator on the first idle gap: the client got one keepalive comment,
    then the stream died. asyncio.wait() must be used instead."""
    import doubao2api.unified_server as us
    monkeypatch.setattr(us, "_KEEPALIVE_INTERVAL", 0.2)

    m = _mock_client()

    async def slow_chat(*args, **kwargs):
        yield {"_event": "CHUNK_DELTA", "text": "first"}
        await asyncio.sleep(0.6)  # idle gap longer than the keepalive interval
        yield {"_event": "CHUNK_DELTA", "text": "second"}

    m.chat_completion = slow_chat
    tc = TestClient(create_app(browser_client=m))
    with tc.stream("POST", "/v1/chat/completions", json={
        "model": "doubao-2.1-turbo",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())

    assert b": keep-alive" in body                 # heartbeat emitted
    assert b"first" in body and b"second" in body  # stream did NOT die after it
    assert b"[DONE]" in body
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py::test_stream_survives_idle_gap_with_keepalive -v`
Expected: **FAIL**（`assert b"second" in body` 失败——旧代码发出 keepalive 后流即终止）

- [ ] **Step 3: 实现（三处改动）**

**3a.** 模块常量区（`_MIN_DISPATCH_INTERVAL` ~:106 旁）加：

```python
# ── SSE keepalive interval (idle gap before emitting a comment frame) ──
_KEEPALIVE_INTERVAL = float(os.environ.get("DOUBAO_KEEPALIVE_INTERVAL", "5"))
```

**3b.** `_stream_chat` 内，把 `try:`（~:1836）之前加一行声明、替换取事件循环（:1845-1853）：

```python
        next_event: Optional[asyncio.Task] = None
        try:
            event_iter = client.chat_completion(
                prompt, use_deep_think=use_deep_think,
                conversation_id=conversation_id or None,
                bot_id=bot_id or None,
                image_attachments=image_attachments,
                file_attachments=file_attachments,
            ).__aiter__()

            while True:
                if next_event is None:
                    next_event = asyncio.ensure_future(event_iter.__anext__())
                done, _pending = await asyncio.wait({next_event}, timeout=_KEEPALIVE_INTERVAL)
                if not done:
                    # Keepalive must NOT use asyncio.wait_for: cancelling __anext__()
                    # closes the async generator and the stream ends at the first idle gap.
                    yield ": keep-alive\n\n"
                    continue
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    break
                next_event = None
```

（`:1855` 起的事件处理代码保持原缩进原样。）

**3c.** 文件收尾的 `except Exception as exc:` 块（~:2094-2097）之后补 `finally`，客户端断开时取消挂起的 anext 任务：

```python
        except Exception as exc:
            log.error("Stream error: %s", exc)
            chunk = _make_chunk({"content": f"[Error: {exc}]"})
            yield f"data: {json.dumps(chunk)}\n\n"
        finally:
            if next_event is not None:
                next_event.cancel()
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py -v`
Expected: `3 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `56 passed, 1 failed`（仍只有环境性 SSRF 失败）

- [ ] **Step 5: 提交**

```bash
git add tests/test_fatal_regressions.py doubao2api/unified_server.py
git commit -m "fix(stream): keepalive 改用 asyncio.wait 避免取消 __anext__ 关闭生成器导致截断"
```

---

### Task 3: 修复换号幽灵方法 apply_account（致命 #3）

**Files:**
- Modify: `doubao2api/unified_server.py:917-933`（`_resolve_captcha_or_failover` Tier 3）
- Modify: `tests/test_captcha_defense.py:119-148`（把掩盖 bug 的 mock 换成真实方法名）
- Test: `tests/test_fatal_regressions.py`（追加接口守卫）

**Interfaces:**
- Consumes: `BrowserClient.switch_to_account(account_id: str) -> bool`（`browser_client.py:938`，真实存在：clear_cookies + inject_cookies_and_reload + set_active_account，返回是否登录成功）。
- Produces: Tier-3 失败（switch 返回 False）时落到 Tier 4 弹窗 + 503，而不是 AttributeError 500。

- [ ] **Step 1: 写失败测试（追加到 tests/test_fatal_regressions.py）**

```python
def test_captcha_failover_only_calls_real_methods():
    """Guard against phantom-method drift: unified_server once called
    client.apply_account(), which never existed on BrowserClient — and the
    old test mocked it, masking the bug behind a green test."""
    for name in (
        "switch_to_account", "try_auto_solve_captcha", "auto_popup_for_captcha",
        "is_captcha_visible", "clear_captcha", "record_success",
    ):
        assert hasattr(BrowserClient, name), f"BrowserClient.{name} missing"
```

同时修改 `tests/test_captcha_defense.py:119-148`——把凭空 mock 换成真实方法（替换第 126 行与第 148 行断言）：

```python
    # 旧: mock_client.apply_account = AsyncMock(return_value=True)
    mock_client.switch_to_account = AsyncMock(return_value=True)
```

```python
    # 旧: mock_client.apply_account.assert_called_once_with(acc2)
    mock_client.switch_to_account.assert_called_once_with(acc2.id)
```

- [ ] **Step 2: 运行验证**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py tests/test_captcha_defense.py -v`
Expected: 接口守卫测试 PASS（守卫针对未来回归）；`test_captcha_failover_in_chat_completions` **FAIL**（生产代码仍调 `apply_account`，mock 已不再提供它 → 该请求路径抛 AttributeError → 500）

- [ ] **Step 3: 实现（unified_server.py Tier 3，替换 :917-933）**

```python
        # Tier 3: Account pool failover (switch to next healthy account)
        if client.account_manager and client.account_manager.accounts:
            current_acc = client.account_manager.get_active_account()
            if current_acc:
                client.account_manager.mark_captcha_required(current_acc.id)
            next_acc = client.account_manager.get_next_available_account()
            if next_acc and (not current_acc or next_acc.id != current_acc.id):
                log.warning(
                    "Captcha hit on account %s. Auto-failing over to account %s (%s)...",
                    current_acc.id if current_acc else "unknown",
                    next_acc.id,
                    next_acc.name,
                )
                ok = await client.switch_to_account(next_acc.id)
                if ok:
                    client.clear_captcha()
                    return
                log.error("Failover to account %s failed; falling back to manual captcha", next_acc.id)
```

与旧代码的差异：`apply_account(next_acc)` → `switch_to_account(next_acc.id)`；以返回值决定成功路径；成功时不再强写 `client._ready = True`（switch_to_account 内部的 `_check_login_state` 会把 `_ready` 设成真实状态，强写会在换号失败时撒谎）。

- [ ] **Step 4: 运行验证测试通过**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py tests/test_captcha_defense.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_fatal_regressions.py tests/test_captcha_defense.py doubao2api/unified_server.py
git commit -m "fix(captcha): 换号路径改用真实存在的 switch_to_account，失败时回落 Tier4 而非 500"
```

---

### Task 4: 在途流排空护栏（并发安全 lite）

**Files:**
- Modify: `doubao2api/browser_client.py`（模块级装饰器 + `__init__` 计数 + 新方法 + 4 个方法加装饰器 + `switch_to_account` 排空）
- Test: `tests/test_fatal_regressions.py`（追加）

**Interfaces:**
- Consumes: 既有 `_mode_lock`（:178，注意 **restart 不得获取它**——`switch_mode` 持锁调用 restart 会非重入死锁，排空逻辑独立于该锁）。
- Produces:
  - 模块级 `_tracks_page_stream(func)` 装饰器：同时支持 async 函数与 async 生成器函数；
  - `BrowserClient._active_streams: int`、`_begin_stream() -> None`、`_end_stream() -> None`；
  - `BrowserClient.drain_streams(timeout: Optional[float] = None) -> None`（默认读 env `DOUBAO_DRAIN_TIMEOUT`，缺省 150 秒）；
  - 被装饰的长流方法：`chat_completion`、`generate_image`、`generate_music`、`generate_video`。

设计要点（实现者必读）：**排空只打在 `switch_to_account` 一处**——所有换号路径（Tier-3 failover、generate_video 内部换号、/admin/api/accounts/switch）都经过它，满足根因修复。`restart()` 故意**不**排空：`chat_completion` 入口处页面死亡时的自愈 restart 发生在被计数的流内部，若 restart 排空会等自己（自锁）。看门狗 restart 时页面已死、在途流已随错误事件结束，排空无意义。

- [ ] **Step 1: 写失败测试（追加到 tests/test_fatal_regressions.py）**

```python
def test_drain_streams_waits_then_returns_when_idle():
    bc = BrowserClient(headless="true")

    async def scenario():
        bc._active_streams = 1
        t0 = time.monotonic()
        await bc.drain_streams(timeout=0.3)
        assert time.monotonic() - t0 >= 0.3      # waited for the in-flight stream

        bc._active_streams = 0
        t0 = time.monotonic()
        await bc.drain_streams(timeout=5)
        assert time.monotonic() - t0 < 1         # idle -> returns immediately

    asyncio.run(scenario())


def test_tracks_page_stream_counts_inflight():
    from doubao2api.browser_client import _tracks_page_stream

    class Probe:
        def __init__(self):
            self.count = 0

        def _begin_stream(self):
            self.count += 1

        def _end_stream(self):
            self.count -= 1

        @_tracks_page_stream
        async def agen(self):
            yield "a"
            yield "b"

        @_tracks_page_stream
        async def coro(self):
            return 42

    p = Probe()

    async def scenario():
        it = p.agen()
        assert await it.__anext__() == "a"
        assert p.count == 1                      # counted while being consumed
        assert await it.__anext__() == "b"
        assert p.count == 1
        try:
            await it.__anext__()
            assert False, "expected StopAsyncIteration"
        except StopAsyncIteration:
            pass
        assert p.count == 0                      # released after exhaustion
        assert await p.coro() == 42
        assert p.count == 0                      # released after return

    asyncio.run(scenario())


def test_switch_to_account_drains_before_reload(tmp_path, monkeypatch):
    bc = BrowserClient(headless="true", user_data_dir=str(tmp_path))
    acc = bc.account_manager.add_or_update_account("acc", {"sessionid": "x"})

    called = []

    async def fake_drain(timeout=None):
        called.append("drain")

    async def fake_restart():
        called.append("restart")

    async def fake_inject(cookies):
        called.append("inject")
        return True

    async def fake_alive():
        return True

    monkeypatch.setattr(bc, "drain_streams", fake_drain)
    monkeypatch.setattr(bc, "restart", fake_restart)
    monkeypatch.setattr(bc, "inject_cookies_and_reload", fake_inject)
    monkeypatch.setattr(bc, "is_alive", fake_alive)
    bc._context = object()  # truthy: skip the restart branch
    bc._page = object()

    ok = asyncio.run(bc.switch_to_account(acc.id))
    assert ok is True
    assert called == ["drain", "inject"]         # drain happens BEFORE any reload
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py -v`
Expected: 三个新测试 **FAIL**（`_tracks_page_stream` 不存在 → ImportError；`drain_streams` 不存在 → AttributeError）

- [ ] **Step 3: 实现（browser_client.py 五处改动）**

**3a.** 文件头 imports 补两个标准库（在 `import asyncio` 附近）：

```python
import functools
import inspect
```

**3b.** 模块级（`_video_generation_started` 之后、`class BrowserClient` 之前）加装饰器：

```python
def _tracks_page_stream(func):
    """Count in-flight long page streams so account switching can drain first.

    ponytail: one shared page — a reload kills every in-flight fetch on it, so
    we serialize instead. True parallel accounts need one BrowserContext each.
    """
    if inspect.isasyncgenfunction(func):
        @functools.wraps(func)
        async def gen_wrapper(self, *args, **kwargs):
            self._begin_stream()
            try:
                async for item in func(self, *args, **kwargs):
                    yield item
            finally:
                self._end_stream()
        return gen_wrapper

    @functools.wraps(func)
    async def call_wrapper(self, *args, **kwargs):
        self._begin_stream()
        try:
            return await func(self, *args, **kwargs)
        finally:
            self._end_stream()
    return call_wrapper
```

**3c.** `__init__`（`self._mode_lock` ~:178 之后）加：

```python
        # In-flight long page streams; switch_to_account drains them before reload
        self._active_streams = 0
```

**3d.** `is_alive` 与 `restart` 之间（~:648）加三个方法：

```python
    def _begin_stream(self) -> None:
        self._active_streams += 1

    def _end_stream(self) -> None:
        self._active_streams = max(0, self._active_streams - 1)

    async def drain_streams(self, timeout: Optional[float] = None) -> None:
        """Wait for in-flight streams before a page reload/restart.

        Proceeds after `timeout` seconds even if streams remain: a stuck stream
        must not block captcha failover forever. Default DOUBAO_DRAIN_TIMEOUT.
        """
        if timeout is None:
            timeout = float(os.environ.get("DOUBAO_DRAIN_TIMEOUT", "150"))
        deadline = time.monotonic() + timeout
        while self._active_streams > 0 and time.monotonic() < deadline:
            await asyncio.sleep(1)
        if self._active_streams > 0:
            log.warning(
                "drain_streams: %d stream(s) still in flight after %.0fs; proceeding with reload",
                self._active_streams, timeout,
            )
```

**3e.** 四个长流方法加装饰器（仅加 `@_tracks_page_stream` 一行，方法体零改动）：`chat_completion`（~:1087）、`generate_image`（~:1695）、`generate_music`（~:1829）、`generate_video`（~:1990）。

**3f.** `switch_to_account`（~:948-957）在 alive 检查之后、`clear_cookies()` 之前插一行：

```python
        if not self._context or not self._page:
            return False

        # Wait for in-flight streams: clear_cookies + reload would kill them all
        await self.drain_streams()

        # Clear existing cookies to avoid session cross-contamination
        try:
            await self._context.clear_cookies()
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_fatal_regressions.py -v`
Expected: `7 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `60 passed, 1 failed`（仍只有环境性 SSRF 失败）

- [ ] **Step 5: 提交**

```bash
git add tests/test_fatal_regressions.py doubao2api/browser_client.py
git commit -m "feat(concurrency): 账号切换前排空在途流，避免 clear_cookies+reload 杀死所有进行中的请求"
```

---

## 明确不做的事（YAGNI 记录）

- **真·多 Context 并行**（一账号一 BrowserContext）：内存会从 ~950MB 涨到 N×250MB+，与「资源效率」目标相反；单机个人工具的并发压力用排空护栏已够。触发条件：出现真实的「多账号同时高并发」需求再上。
- **去掉人工验证码依赖**：Tier-2 自动滑块求解已存在；彻底无人值守是逆向研究课题（验证码形态会变），不是工程任务。触发条件：自动求解成功率有实测数据再说。
- **内存占用优化（9 个 chrome 进程）**：换不来功能收益，先不动。
- **`test_ssrf_is_safe_url` 失败**：本机 fake-DNS 环境问题，防护行为本身正确。
- **`temperature`/`top_p`/`stop` 透传上游**：豆包网页端 API 无对应参数位，透传无处可去（报告对 qwen2api-rs 的这条批评不适用于本项目）。

## Self-Review 结果

- 覆盖检查：报告点名的三件「必须改的事」中，三 bug 全覆盖；多 Context 与验证码依赖按 ponytail 阶梯评估后记入「不做」并给出触发条件。
- 占位符扫描：无 TBD/TODO；所有代码步骤含完整代码。
- 类型一致性：`switch_to_account(account_id: str)` 与测试断言 `assert_called_once_with(acc2.id)` 一致；`_KEEPALIVE_INTERVAL` 在测试中经 `monkeypatch.setattr(us, "_KEEPALIVE_INTERVAL", 0.2)` 修改，`_stream_chat` 闭包在调用时读模块全局，monkeypatch 生效；`drain_streams(timeout: Optional[float])` 与 fake_drain 签名一致。
- 自锁排查：restart 不排空（chat_completion 自愈路径在被计数的流内部）；switch_mode 持 `_mode_lock` 调 restart，restart 不取该锁，无死锁环。
