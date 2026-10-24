# 多账号 Context 池 + 验证码重试 + 启动旗子 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多账号真正并行（每账号一个隔离 incognito context，共享一棵 Chromium 进程树）、验证码防线不等人（Tier-2 重试 + Tier-3 零成本逃生）、内存随并发付费（启动旗子立省 + context 惰性开启）。

**Architecture:** 一个 BrowserClient 实例内做「服务 context 池」：持久化 context 只负责登录/管理（`self._page` 一切照旧），API 请求按账号路由到 `browser.new_context()` 开出的隔离 incognito context（已实测 `ctx.browser.new_context()` 可用且 cookie 完全隔离）。页面访问通过给现有方法加 `page=None` 可选参数穿透（缺省回退 `self._page`，零回归）。会话粘性用 `conversation_id -> account_id` 一个 dict。Tier-2 求解器加一次重试；`launch_args` 加 4 个内存旗子。

**Tech Stack:** Python 3.10+ / Playwright async API（现有依赖，零新增）/ pytest

## Global Constraints

- **测试命令**（本机 `.venv` 无 pytest）：`D:/python3132/python.exe -m pytest tests/ -q`
- **基线**：`60 passed, 1 failed`。唯一失败 `test_unit.py::test_ssrf_is_safe_url` 是本机 fake-DNS 环境问题（防护行为正确），**不是回归，禁止去修**。
- **零新依赖、零新文件**：全部改动落在 `doubao2api/browser_client.py`、`doubao2api/unified_server.py`、既有测试文件。
- **行号基准**：基于 commit `2017346` 的工作区。改动后行号会漂移，Edit 时以锚文本为准，行号仅作定位提示。
- **向后兼容铁律**：`page=None` 缺省时一切行为与现在逐字节一致——所有现有调用点不传 page 时走的还是 `self._page`。**除 Task 5 的路由改动外，禁止改动任何现有调用点的语义。**
- **关键机制事实**（实现者必读，已实测验证）：
  1. `window.bdms.frontierSign()` 是 doubao.com 页面自带的站点 JS，与账号无关——任何加载了豆包前端的 context 里都能用。
  2. `ctx.browser.new_context()`（持久化 context 的 `.browser` 属性）返回挂在同一棵 Chromium 进程树上的隔离 incognito context，cookie 互不可见（本机 Playwright 实测通过）。
  3. `self._stream_queues` 按 `request_id` 寻址，`expose_function` 是 context 级——每个新 context 需要自己注册一次流桥。
  4. `_tracks_page_stream` 装饰器计的是「客户端长流」，与哪个 page 无关，池化后语义不变。
- **风格**：中文日志/注释，`logging` 模块，deliberate simplification 用 `ponytail:` 注释标注上限。

## 背景：为什么这样设计（实现者需知）

现状：所有 API 请求共享一个 `self._page`。换账号 = `switch_to_account` 清 cookie + reload（杀所有在途流，现有排空护栏只是止血）。

三个目标的公共底座是 context 池：并行 = 不同账号同时在自己 page 上跑；Tier-3 逃生 = 风控账号不再路由新请求（不 reload、不断流）；内存 = context 惰性开启，`DOUBAO_POOL_SIZE` 缺省 1 时与今天等价。

明确不做的（YAGNI，触发条件记录在案）：每账号 inflight 限流/最小堆选号（qwen2api-rs 的 16k 账号才需要，个人池 ≤5 账号用 `get_next_available_account` 遍历足够）；headless 下 Tier-4 弹窗（无显示时弹窗本来就失败，维持 503）。

---

### Task 1: `_sign_url` 与 `_wait_for_signing` 参数化（page 穿透的地基）

**Files:**
- Modify: `doubao2api/browser_client.py:863-873`（`_wait_for_signing`）、`:1052-1084`（`_sign_url`）
- Test: `tests/test_context_pool.py`（新建）

**Interfaces:**
- Consumes: 无（第一步）。
- Produces:
  - `async def _wait_for_signing(self, page: Optional[Page] = None) -> None`（缺省 `self._page`）
  - `async def _sign_url(self, base_url: str, params: Dict[str, str], page: Optional[Page] = None) -> str`（缺省 `self._page`）
  - 后续所有 Task 依赖这两个签名；**现有调用点不改**（缺省参数保证兼容）。

- [ ] **Step 1: 写失败测试（新建 tests/test_context_pool.py，完整文件）**

```python
"""Tests for the per-account service context pool.

The pool routes each API request to an isolated incognito context on the same
Chromium process tree, keyed by account. The persistent context keeps its
login/admin role untouched (self._page). All new methods must fall back to
self._page when no page is passed, byte-identical with today's behavior.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from doubao2api.browser_client import BrowserClient


def _bc() -> BrowserClient:
    bc = BrowserClient(headless="true")
    bc._ready = True
    return bc


def test_sign_url_accepts_explicit_page_and_falls_back(monkeypatch):
    bc = _bc()
    default_page = MagicMock()
    bc._page = default_page

    captured = {}

    async def fake_evaluate(self, script, *args, **kwargs):
        return {"a_bogus": "sig123"}

    # patch the default page and an explicit page to distinguish which one is used
    async def eval_default(js):
        captured["page"] = "default"
        return {"a_bogus": "sig-default"}

    async def eval_explicit(js):
        captured["page"] = "explicit"
        return {"a_bogus": "sig-explicit"}

    default_page.evaluate = eval_default
    explicit = MagicMock()
    explicit.evaluate = eval_explicit

    url1 = asyncio.run(bc._sign_url("/chat/completion", {"a": "1"}))
    url2 = asyncio.run(bc._sign_url("/chat/completion", {"a": "1"}, page=explicit))

    assert "sig-default" in url1
    assert "sig-explicit" in url2
    assert captured == {"page": "explicit"}  # last call went to explicit page
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: FAIL / ERROR —— `_sign_url()` 不接受 `page` 关键字（`TypeError: _sign_url() got an unexpected keyword argument 'page'`）

- [ ] **Step 3: 最小实现**

`_wait_for_signing`（`browser_client.py:863`）：

```python
    async def _wait_for_signing(self, page: Optional[Page] = None):
        """Wait for bdms.frontierSign to become available (legacy, kept for upload signing)."""
        page = page or self._page
        for i in range(12):  # up to 60s
            has_sign = await page.evaluate(
                "() => typeof window.bdms?.frontierSign === 'function'"
            )
            if has_sign:
                log.info("bdms.frontierSign available after %ds", (i + 1) * 5)
                return
            await asyncio.sleep(5)
        log.warning("bdms.frontierSign not available after 60s - signing may fail")
```

`_sign_url`（`browser_client.py:1052`）——只改签名与 evaluate 目标，循环体原样：

```python
    async def _sign_url(self, base_url: str, params: Dict[str, str], page: Optional[Page] = None) -> str:
        """Sign a URL using bdms.frontierSign with retry on failure."""
        page = page or self._page
        sorted_params = dict(sorted(params.items()))
        query_string = urlencode(sorted_params)

        last_error = None
        for attempt in range(3):
            try:
                sig = await page.evaluate(
                    f'window.bdms.frontierSign("{query_string}")'
                )
                # …以下与现版完全一致（返回 URL 的三分支与重试逻辑），不动…
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: `1 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `61 passed, 1 failed`（仅环境性 SSRF 失败）

- [ ] **Step 5: 提交**

```bash
git add tests/test_context_pool.py doubao2api/browser_client.py
git commit -m "refactor(pool): _sign_url/_wait_for_signing 支持显式 page，缺省回退 self._page"
```

---

### Task 2: 流桥 context 化 —— `_setup_fetch_bridge` 与 `_browser_fetch_stream` 参数化

**Files:**
- Modify: `doubao2api/browser_client.py:875-908`（`_setup_fetch_bridge`）、`:1469-1557`（`_browser_fetch_stream`）
- Test: `tests/test_context_pool.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `page: Optional[Page] = None` 约定。
- Produces:
  - `async def _setup_fetch_bridge(self, page: Optional[Page] = None) -> None`——**注意**：`expose_function` 是 context 级且不可重名，此方法改为「按 page 注册、成功即记」，`_bridge_ready` 语义变为「默认 context 已注册」（启动路径行为不变）。
  - `async def _browser_fetch_stream(self, url: str, payload: Dict[str, Any], request_id: str, page: Optional[Page] = None)`（缺省 `self._page`）。
  - 桥回调 `__doubaoStreamChunk` 是**每个 context 各自注入、共享同一个 Python 队列路由**：`_on_stream_chunk` 按 `request_id` 找队列，与 page 无关，天然多路复用。

- [ ] **Step 1: 写失败测试（追加到 tests/test_context_pool.py）**

```python
def test_setup_fetch_bridge_registers_on_explicit_page(monkeypatch):
    bc = _bc()
    registered = []

    async def fake_expose(name, cb):
        registered.append(name)

    default_page = MagicMock()
    explicit = MagicMock()
    default_page.expose_function = fake_expose
    explicit.expose_function = fake_expose

    bc._page = default_page
    asyncio.run(bc._setup_fetch_bridge())
    assert registered == ["__doubaoStreamChunk"]

    asyncio.run(bc._setup_fetch_bridge(page=explicit))
    assert registered == ["__doubaoStreamChunk", "__doubaoStreamChunk"]  # both registered

    # callback routes by request_id regardless of which page called it
    q_key = "req_test"
    bc._stream_queues[q_key] = asyncio.Queue()
    cb = bc._page_handlers  # produced by implementation; see Step 3


def test_browser_fetch_stream_accepts_page(monkeypatch):
    bc = _bc()
    captured = {}
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=None)

    async def fake_stream(self, url, payload, request_id, page=None):
        captured["page"] = page

    # direct call: default goes to self._page, explicit goes to the passed page
    bc._page = MagicMock()
    bc._page.evaluate = AsyncMock(return_value=None)
    explicit = page

    payload = {"a": 1}
    asyncio.run(bc._browser_fetch_stream("http://x", payload, "r1"))
    asyncio.run(bc._browser_fetch_stream("http://x", payload, "r2", page=explicit))
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: 新增两个测试 FAIL（`TypeError: unexpected keyword 'page'`）

- [ ] **Step 3: 实现**

`_setup_fetch_bridge`（`browser_client.py:875`）——`expose_function` 是 context 级，每个新 context 要注册一次；重复注册同名函数会抛错但含 "already been registered"，现有 except 分支已处理，但那是「同一 page 上重注册」的语义；跨 page 各自注册互不冲突。实现为幂等按页注册：

```python
    async def _setup_fetch_bridge(self, page: Optional[Page] = None) -> None:
        """Register the stream-chunk callback on the page's context.

        expose_function is context-scoped: every new context must register its
        own bridge. The callback routes by request_id, so one Python-side
        router serves all pages/contexts.
        """
        page = page or self._page
        if not page:
            return
        # ponytail: track registered pages on a set; per-page registration is
        # idempotent via this set — escalate to per-context sets if pages get
        # replaced inside a context at runtime.
        if page not in self._bridged_pages:
            try:
                await page.expose_function("__doubaoStreamChunk", self._on_stream_chunk)
                self._bridged_pages.add(page)
                log.info("Fetch bridge registered (page=%s)", page.url if not page.is_closed() else "?")
            except Exception as e:
                if "already been registered" in str(e).lower():
                    self._bridged_pages.add(page)
                else:
                    log.error("Failed to register fetch bridge: %s", e)
                    raise
```

配套改动（三处）：

1. `__init__`（`self._active_streams = 0` 旁）加：

```python
        # Fetch-bridge registration is per-context; track which pages have it
        self._bridged_pages: set = set()
```

2. 把 `_setup_fetch_bridge` 现有的内嵌 `async def _on_stream_chunk(request_id, chunk_json)` 提升为实例方法（供 expose_function 引用与测试访问）：

```python
    async def _on_stream_chunk(self, request_id: str, chunk_json: str):
        """Called from browser JS for each SSE chunk or completion signal (any context)."""
        queue = self._stream_queues.get(request_id)
        if queue:
            await queue.put(chunk_json)
```

3. 旧 `self._bridge_ready` 标志：`start()` 里对默认页的注册维持现状（`_bridged_pages` 集合承担同等职责）；`_bridge_ready` 属性如仍被外部引用则保留字段赋值（grep 确认仅 `browser_client.py:878` 赋值、`:806` 初始化、`stop()` 清零——无读取方，**直接删除该字段**）。

`_browser_fetch_stream`（`:1469`）——签名加 `page=None`，body 末尾 `await self._page.evaluate(...)` 改：

```python
    async def _browser_fetch_stream(
        self, url: str, payload: Dict[str, Any], request_id: str,
        page: Optional[Page] = None,
    ):
        """Execute fetch() inside the given page and stream SSE chunks via callback."""
        # js_code 原样不动（页面内 JS 与 page 无关）
        payload_json = json.dumps(payload, ensure_ascii=False)
        await (page or self._page).evaluate(js_code, [url, payload_json, request_id])
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: `3 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `62 passed, 1 failed`

- [ ] **Step 5: 提交**

```bash
git add tests/test_context_pool.py doubao2api/browser_client.py
git commit -m "refactor(pool): 流桥按 context 注册，_browser_fetch_stream 支持显式 page"
```

---

### Task 3: 服务 context 池核心 —— `acquire_service_page` / `release_service_context`

**Files:**
- Modify: `doubao2api/browser_client.py`（`__init__` 加池字段；`_samantha_request`、`_thread_list`、`delete_conversation`、`chat_completion`、`generate_image`、`generate_music`、`generate_video`、`chat`、`chat_with_file` 加 `page=None` 穿透；新增池方法；`stop()` 清池）
- Test: `tests/test_context_pool.py`（追加）

**Interfaces:**
- Consumes: Task 1 `_sign_url(..., page=)`、Task 2 `_setup_fetch_bridge(page=)` / `_browser_fetch_stream(..., page=)`。
- Produces（后续 Task 与 unified_server 依赖的精确签名）:
  - `async def acquire_service_page(self, acc: "Account") -> Optional[Page]`——按 `acc.id` 取/建隔离 incognito context 的 page：建 = `self._context.browser.new_context()` → 注入 `acc.cookies`（`_to_playwright_cookies`）→ `new_page()` → `goto(CHAT_URL)` → `_setup_fetch_bridge(page)` → `_wait_for_signing(page)`；失败记日志返回 None。池上限 `DOUBAO_POOL_SIZE`（缺省 1）——超限时复用最近最少使用的既有账号 page（**不排队、不报错**：单机服务，宁可串行也不堆积）。
  - `def release_service_context(self, account_id: str) -> None`——关闭并移除某账号的 context（风控/失效时调用；在途流靠 `_tracks_page_stream` 计数不收账，context.close 会令其页面 fetch 立即失败、事件流自然终止——**先关后记**的语义见 Step 3 注释）。
  - `_to_playwright_cookies(cookies: Dict[str, str]) -> List[dict]`——把 `acc.cookies` 变 Playwright add_cookies 列表（domain `.doubao.com`），**从 `inject_cookies_and_reload` 里抽出复用**（那里现有同样的 7 行内联构造）。
  - `chat_completion(..., page: Optional[Page] = None)`；`generate_image/generate_music/generate_video(..., page: Optional[Page] = None)`——签名追加，内部所有 `self._page` 的 evaluate/sign/bridge 调用改用 `page or self._page` 局部变量。`_samantha_request/_thread_list/delete_conversation` 同样加 `page=None` 并穿透。
- 设计铁律：**缺省 `page=None` 时走 `self._page`，行为与今天逐字节一致**；`restart()`/`stop()` 关池中所有 context。

- [ ] **Step 1: 写失败测试（追加到 tests/test_context_pool.py）**

```python
def test_to_playwright_cookies_shape():
    bc = _bc()
    pw = bc._to_playwright_cookies({"sessionid": "s1", "msToken": "m1"})
    assert pw == [
        {"name": "sessionid", "value": "s1", "domain": ".doubao.com", "path": "/"},
        {"name": "msToken", "value": "m1", "domain": ".doubao.com", "path": "/"},
    ]


def test_acquire_service_page_creates_per_account_context(monkeypatch):
    bc = _bc()
    from doubao2api.account_manager import Account, AccountManager
    am = AccountManager(filepath=":")  # never saved in this test
    acc = Account(id="a1", name="acc1", cookies={"sessionid": "s1"})

    created = []

    class FakeCtx:
        def __init__(self):
            self.cookies_added = None
            self.page = MagicMock()
            self.page.is_closed = lambda: False

        async def add_cookies(self, c):
            self.cookies_added = c

        async def new_page(self):
            return self.page

        async def close(self):
            created.remove(self) if self in created else None

    class FakeBrowser:
        async def new_context(self, **kw):
            ctx = FakeCtx()
            created.append(ctx)
            return ctx

    bc._context = MagicMock()
    bc._context.browser = FakeBrowser()
    bc._ready = True

    async def fake_goto(url, **kw):
        return None

    async def fake_bridge(page=None):
        return None

    async def fake_signing(page=None):
        return None

    monkeypatch.setattr("doubao2api.browser_client.BrowserClient._setup_fetch_bridge", fake_bridge)
    monkeypatch.setattr("doubao2api.browser_client.BrowserClient._wait_for_signing", fake_signing)
    acc_page = MagicMock()
    acc_page.is_closed = lambda: False
    acc_page.goto = fake_goto

    async def fake_new_page(self_inner):
        return acc_page

    FakeCtx.new_page = fake_new_page

    page1 = asyncio.run(bc.acquire_service_page(acc))
    page2 = asyncio.run(bc.acquire_service_page(acc))
    assert page1 is page2                      # same account -> cached page
    assert len(created) == 1                   # one context for that account
    assert created[0].cookies_added[0]["name"] == "sessionid"

    acc2 = Account(id="a2", name="acc2", cookies={"sessionid": "s2"})
    page3 = asyncio.run(bc.acquire_service_page(acc2))
    assert len(created) == 2                    # second account -> second context

    # release closes and evicts
    bc.release_service_context("a1")
    assert "a1" not in bc._svc


def test_acquire_returns_none_when_context_dead():
    bc = _bc()
    bc._context = None   # browser not started
    from doubao2api.account_manager import Account
    acc = Account(id="a1", name="acc1", cookies={"sessionid": "s1"})
    assert asyncio.run(bc.acquire_service_page(acc)) is None
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: 新增三个测试 FAIL（`AttributeError: no attribute '_to_playwright_cookies'` 等）

- [ ] **Step 3: 实现（browser_client.py，按序五块）**

**3a. `__init__` 字段**（`self._active_streams = 0` 之后）：

```python
        # Per-account service contexts (incognito, same Chromium tree).
        # Lazy: created on first routed request, capped by DOUBAO_POOL_SIZE.
        self._svc: Dict[str, dict] = {"": None}  # placeholder removed below; use plain dict
```

实际写法（去掉占位，直接）：

```python
        # Per-account service contexts: account_id -> {"context", "page"}
        self._svc: Dict[str, dict] = {}
        self._pool_size = max(1, int(os.environ.get("DOUBAO_POOL_SIZE", "1")))
```

**3b. `_to_playwright_cookies`**（模块级或静态方法，放 `switch_to_account` 附近）：

```python
    @staticmethod
    def _to_playwright_cookies(cookies: Dict[str, str]) -> List[dict]:
        return [
            {"name": name, "value": value, "domain": ".doubao.com", "path": "/"}
            for name, value in cookies.items()
        ]
```

并把 `inject_cookies_and_reload`（`:967` 附近）里的内联 `pw_cookies = [...]` 七行替换为 `pw_cookies = self._to_playwright_cookies(cookies)`（DRY：同一构造只此一份）。

**3c. 池方法**（放 `switch_to_account` 之后）：

```python
    async def acquire_service_page(self, acc) -> Optional[Page]:
        """Get or create this account's isolated service page (incognito context
        on the same Chromium tree). Returns None if the browser isn't up.

        ponytail: over-pool-size requests reuse the LRU account page instead of
        queueing — single-user service, serial beats backlog.
        """
        if not self._context or not getattr(self._context, "browser", None):
            return None

        s = self._svc.get(acc.id)
        if s and not s["page"].is_closed():
            return s["page"]

        # Enforce pool size by evicting the least-recently-used entry
        while len(self._svc) >= self._pool_size:
            lru_id = next(iter(self._svc))
            if lru_id == acc.id and len(self._svc) == 1:
                break
            self.release_service_context(lru_id)

        try:
            ctx = await self._context.browser.new_context(
                viewport={"width": 1280, "height": 720}, locale="zh-CN",
            )
            await ctx.add_cookies(self._to_playwright_cookies(acc.cookies))
            page = await ctx.new_page()
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            await self._setup_fetch_bridge(page)
            await self._wait_for_signing(page)
            self._svc[acc.id] = {"context": ctx, "page": page}
            log.info("Service context ready for account %s (%s), pool=%d/%d",
                     acc.id, acc.name, len(self._svc), self._pool_size)
            return page
        except Exception as e:
            log.error("acquire_service_page failed for %s: %s", acc.id, e)
            try:
                await ctx.close()
            except Exception:
                pass
            return None

    def release_service_context(self, account_id: str) -> None:
        """Close and evict an account's service context (captcha / invalid cookie)."""
        s = self._svc.pop(account_id, None)
        if not s:
            return
        ctx = s["context"]
        # close() is sync-scheduled by Playwright; in-flight fetches on that
        # page fail fast and their event streams end naturally.
        try:
            asyncio.get_running_loop().create_task(ctx.close())
        except RuntimeError:
            pass
        log.info("Service context released for account %s", account_id)
```

注：`ctx.close()` 用 `create_task` 惰性执行以避免在同步方法里 await；若调用点总是异步上下文，实现者可把签名改为 `async def` 并 `await`（**二选一，选定后 Task 5 调用点保持一致**——本计划统一用上述惰性版）。

**3d. 穿透改造**——以下方法签名各加 `page: Optional[Page] = None`，方法体顶部 `pg = page or self._page`，把体内 **evaluate/sign/bridge/thread-list 调用**的 `self._page`/无参调用改为 `pg`/显式传 `page=pg`：

| 方法（行号提示） | 体内需要改的调用 |
|---|---|
| `chat_completion` (:1145) | `self._sign_url(..., page=pg)`；`self._browser_fetch_stream(url, payload, request_id, page=pg)`。开头 `self._page` 存活检查保持针对 `self._page`（自愈逻辑只属默认页；服务页死亡由池重建处理，不触发全浏览器 restart） |
| `chat` (:1502) | 转调 `chat_completion(..., page=page)` |
| `delete_conversation` (:1582) | `_sign_url(..., page=pg)`；`self._page.evaluate` → `pg.evaluate` |
| `_samantha_request` (:1658) | `_sign_url(..., page=pg)`；`self._page.evaluate` → `pg.evaluate` |
| `generate_image` (:1754) / `generate_music` (:1889) / `generate_video` (:2051) | `_samantha_request(payload, ..., page=pg)`；`generate_video` 内部 `_thread_list(..., page=pg)`、`_wait_for_generated_video` 与 `_extract_video_from_thread` 的 page 参数照 3e 传 |
| `_thread_list` (:2245) | `_sign_url(..., page=pg)`；evaluate → `pg` |
| `_wait_for_generated_video` (:2261) / `_extract_video_from_thread` (:2333) / `_poll_video_result` | 加 `page=None` 并把内部 `_thread_list`/goto 调用传 `page=pg` |
| `chat_with_file` (:2696) | `_sign_url(..., page=pg)`；evaluate → `pg` |

`upload_file`/`upload_image` **不改**——它们走 `self._http`（httpx 直连），唯一 page 依赖是 `_sign_url`/`_get_cookies_string`；多账号场景下签名在任一豆包页面都成立（站点 JS），cookie 属上传的 httpx 头。第一版让上传发生在默认账号（`self._page` 签名）——TOS 上传的文件由 URL 引用，跨账号会话可直接引用（豆包图片上传走全局 TOS，不挂会话）。`# ponytail: uploads sign on the default page; per-account signing when a cross-account upload ever fails.`

**3e. `stop()` 清池**（`stop()` 里 `self._context = None` 之前）：

```python
        for acc_id in list(self._svc):
            self.release_service_context(acc_id)
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: `6 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `63 passed, 1 failed`（既有套件全部不受影响——所有穿透缺省 `self._page`）

- [ ] **Step 5: 提交**

```bash
git add tests/test_context_pool.py doubao2api/browser_client.py
git commit -m "feat(pool): 每账号隔离 incognito context 池，长流方法 page 穿透，缺省行为不变"
```

---

### Task 4: 验证码防线 —— Tier-2 重试 + Tier-3 池化逃生

**Files:**
- Modify: `doubao2api/browser_client.py:326-341`（`try_auto_solve_captcha`）
- Modify: `doubao2api/unified_server.py:893-945`（`_resolve_captcha_or_failover`）
- Test: `tests/test_context_pool.py`（追加）

**Interfaces:**
- Consumes: Task 3 `release_service_context(account_id)`。
- Produces: `try_auto_solve_captcha` 单账号尝试 2 次；`_resolve_captcha_or_failover(client, *, page=None)` Tier-3 从「reload 换号」变为「释放该账号服务 context + 标记 captcha 状态」，**不再触碰 `self._page`**（默认页保持登录人视角，风控账号的 context 一次性报废，在途请求零影响）。

- [ ] **Step 1: 写失败测试（追加到 tests/test_context_pool.py）**

```python
def test_try_auto_solve_retries_once(monkeypatch):
    bc = _bc()
    bc._page = MagicMock()
    calls = {"n": 0}

    class FlakySolver:
        async def detect_and_solve(self, page, timeout=8.0):
            calls["n"] += 1
            return calls["n"] >= 2   # fails first, succeeds second

    import doubao2api.browser_client as bcmod
    monkeypatch.setattr(bcmod, "_captcha_solver_cls", FlakySolver, raising=False)
    # implementation imports inside the method; patch the import target instead:
    import doubao2api.captcha_solver as cs
    monkeypatch.setattr(cs, "SliderCaptchaSolver", FlakySolver)

    assert asyncio.run(bc.try_auto_solve_captcha()) is True
    assert calls["n"] == 2

    calls["n"] = 0

    class DeadSolver:
        async def detect_and_solve(self, page, timeout=8.0):
            calls["n"] += 1
            return False

    monkeypatch.setattr(cs, "SliderCaptchaSolver", DeadSolver)
    assert asyncio.run(bc.try_auto_solve_captcha()) is False
    assert calls["n"] == 2          # tried twice, not once, not forever


def test_captcha_failover_releases_service_context_no_reload(tmp_path, monkeypatch):
    """Tier-3 with the pool must NOT reload the shared page; it evicts the
    account's service context and lets routing move on."""
    from doubao2api.account_manager import AccountManager
    from doubao2api.unified_server import create_app

    am = AccountManager(filepath=str(tmp_path / "a.json"), strategy="failover")
    acc1 = am.add_or_update_account("主号", {"sessionid": "s1"})
    acc2 = am.add_or_update_account("副号", {"sessionid": "s2"})
    am.set_active_account(acc1.id)

    from tests.test_fatal_regressions import _mock_client
    m = _mock_client()
    m.account_manager = am
    m.needs_captcha = True
    m.is_captcha_visible = AsyncMock(return_value=True)
    m.try_auto_solve_captcha = AsyncMock(return_value=False)
    m.release_service_context = MagicMock()
    # switch_to_account must NOT be called anymore in pool mode
    m.switch_to_account = AsyncMock(return_value=True)

    async def fake_chat(*a, **kw):
        yield {"_event": "CHUNK_DELTA", "text": "ok"}

    m.chat_completion = fake_chat

    from fastapi.testclient import TestClient
    tc = TestClient(create_app(browser_client=m))
    resp = tc.post("/v1/chat/completions", json={
        "model": "doubao-2.1-turbo", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200, resp.text
    assert am.get_account(acc1.id).status == "captcha"
    m.release_service_context.assert_called_once_with(acc1.id)
    m.switch_to_account.assert_not_called()      # no reload, no stream kill
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: 两个新测试 FAIL（重试测试失败于 `calls["n"] == 2` 实际为 1；failover 测试失败于 `release_service_context` 未被调用 / `switch_to_account` 被调用）

- [ ] **Step 3: 实现**

`try_auto_solve_captcha`（`browser_client.py:326`）——重试 2 次（轨迹含随机性，单次失败≠账号死）：

```python
    async def try_auto_solve_captcha(self) -> bool:
        """Attempt to automatically detect and solve slider captcha.

        The solver's trajectory is randomized, so a single miss is not a verdict:
        retry once before giving up on this account.
        """
        if not self._page:
            return False
        from .captcha_solver import SliderCaptchaSolver
        for attempt in range(2):
            try:
                solved = await SliderCaptchaSolver().detect_and_solve(self._page)
            except Exception as e:
                log.warning("Automated captcha solver error (attempt %d): %s", attempt + 1, e)
                continue
            if solved:
                self.clear_captcha()
                self.record_success()
                self._ready = True
                log.info("Auto-solved slider captcha (attempt %d)!", attempt + 1)
                return True
        return False
```

`_resolve_captcha_or_failover` Tier-3（`unified_server.py:917-936` 附近）——替换为池化逃生（保留 `switch_to_account` 作为**无池回退**：`client` 无 `release_service_context` 属性时走旧路径，兼容测试 mock 与旧部署）：

```python
        # Tier 3: Account pool failover
        # Pool mode: evict the captcha account's service context — routing moves
        # to other accounts, in-flight streams untouched, no page reload.
        # Fallback mode (no pool attr): legacy reload-based switch.
        if client.account_manager and client.account_manager.accounts:
            current_acc = client.account_manager.get_active_account()
            if current_acc:
                client.account_manager.mark_captcha_required(current_acc.id)
                if hasattr(client, "release_service_context"):
                    client.release_service_context(current_acc.id)
            next_acc = client.account_manager.get_next_available_account()
            if next_acc and (not current_acc or next_acc.id != current_acc.id):
                log.warning(
                    "Captcha hit on account %s. Failing over to account %s (%s).",
                    current_acc.id if current_acc else "unknown",
                    next_acc.id, next_acc.name,
                )
                client.clear_captcha()
                client._ready = True
                return
            if current_acc and not hasattr(client, "release_service_context"):
                ok = await client.switch_to_account(current_acc.id)
                if ok:
                    client.clear_captcha()
                    return
```

（逻辑说明：池模式下「失败转移」= 状态标记 + context 报废，下个请求由 Task 5 的路由自然落到别的账号；`switch_to_account` 只在无池属性时兜底。`needs_captcha` 旗标清掉以免 Tier-4 误触发。）

同时把 `tests/test_fatal_regressions.py` 中 `_mock_client()` 补一行（其他测试不感知）：

```python
    m.release_service_context = MagicMock()
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: `8 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `65 passed, 1 failed`（旧 `test_captcha_failover_in_chat_completions` 语义变化：见其断言更新）

**同步更新旧测试语义**：`tests/test_captcha_defense.py::test_captcha_failover_in_chat_completions` 断言 `switch_to_account.assert_called_once_with(acc2.id)` 在池模式下不再成立。改为：

```python
    # Pool mode: failover evicts the captcha account's context, no reload
    mock_client.release_service_context.assert_called_once_with(acc1.id)
    mock_client.switch_to_account.assert_not_called()
```

（该 mock client 需相应加 `mock_client.release_service_context = MagicMock()`。）

- [ ] **Step 5: 提交**

```bash
git add tests/test_context_pool.py tests/test_captcha_defense.py tests/test_fatal_regressions.py doubao2api/browser_client.py doubao2api/unified_server.py
git commit -m "feat(captcha): Tier-2 求解重试一次；Tier-3 池化逃生（释放 context 而非 reload）"
```

---

### Task 5: 请求路由 —— 按账号选页 + 会话粘性

**Files:**
- Modify: `doubao2api/unified_server.py`（chat 端点路由段 :996-1055；`_stream_chat`/`_collect_chat_response` 调用点）
- Test: `tests/test_context_pool.py`（追加）

**Interfaces:**
- Consumes: Task 3 `acquire_service_page(acc) -> Optional[Page]`、`client.chat_completion(..., page=)`、`generate_*(..., page=)`、`upload_*(...)`；`AccountManager.get_next_available_account()`。
- Produces:
  - 模块级路由器（unified_server.py 内，闭包外）：

```python
def _route_account(client, conversation_id: Optional[str]) -> Optional["Account"]:
    """Pick the account for a request: sticky by conversation, else next-available.

    Returns None -> caller falls back to the default page (single-account mode,
    or pool not initialized, or pool disabled by env).
    """
    if os.environ.get("DOUBAO_POOL_SIZE", "1") == "1" or not (client.account_manager and client.account_manager.accounts):
        return None
    if conversation_id:
        owner = _conv_owner.get(conversation_id)
        if owner:
            acc = client.account_manager.get_account(owner)
            if acc and acc.status == "active":
                return acc
    return client.account_manager.get_next_available_account()
```

  - `_conv_owner: Dict[str, str]`（create_app 闭包内或模块级，见 Step 3）+ 应答后记录归属：从 chat 响应事件提取的 `conversation_id` 记到路由时的账号。
  - 路由启用条件：`DOUBAO_POOL_SIZE > 1` 且账号池非空。**缺省（=1）时整个路由层是 no-op，行为与今天完全一致**——这就是「支持多账号」和「同时跑多账号」两个开关的分界。

- [ ] **Step 1: 写失败测试（追加到 tests/test_context_pool.py）**

```python
def test_routing_sticky_by_conversation_and_falls_back(monkeypatch):
    import doubao2api.unified_server as us
    from doubao2api.account_manager import Account, AccountManager

    am = AccountManager(filepath=":")
    acc1 = Account(id="a1", name="1", cookies={"sessionid": "s"})
    acc2 = Account(id="a2", name="2", cookies={"sessionid": "s"})
    am.accounts = {"a1": acc1, "a2": acc2}
    am.active_account_id = "a1"

    client = MagicMock()
    client.account_manager = am

    # pool disabled (default) -> None regardless
    monkeypatch.setenv("DOUBAO_POOL_SIZE", "1")
    assert us._route_account.__wrapped__(client, None) is None if hasattr(us._route_account, "__wrapped__") else True
    # ^ helper defined at module level with functools.wraps? call directly:
    monkeypatch.delenv("DOUBAO_POOL_SIZE", raising=False)
    assert us._route_account(client, None) is None        # default env = 1 -> disabled

    monkeypatch.setenv("DOUBAO_POOL_SIZE", "2")
    assert us._route_account(client, "c_new") is not None  # new conv -> any account
    assert us._route_account(client, "c_new").id in ("a1", "a2")

    # sticky: existing conversation owned by a2 returns a2 even if a1 active
    us._conv_owner["c_sticky"] = "a2"
    assert us._route_account(client, "c_sticky").id == "a2"

    # owner flagged captcha -> falls through to next available
    am.mark_captcha_required("a2")
    routed = us._route_account(client, "c_sticky")
    assert routed is None or routed.id != "a2"


def test_chat_endpoint_routes_to_account_page(monkeypatch):
    from doubao2api.account_manager import Account, AccountManager
    from doubao2api.unified_server import create_app
    from fastapi.testclient import TestClient

    am = AccountManager(filepath=":")
    acc1 = Account(id="a1", name="1", cookies={"sessionid": "s"})
    am.accounts = {"a1": acc1}
    am.active_account_id = "a1"
    monkeypatch.setenv("DOUBAO_POOL_SIZE", "2")

    from tests.test_fatal_regressions import _mock_client
    m = _mock_client()
    m.account_manager = am
    svc_page = MagicMock()
    m.acquire_service_page = AsyncMock(return_value=svc_page)

    async def fake_chat(*args, **kwargs):
        assert kwargs.get("page") is svc_page
        yield {"_event": "CHUNK_DELTA", "text": "ok"}

    m.chat_completion = fake_chat

    tc = TestClient(create_app(browser_client=m))
    resp = tc.post("/v1/chat/completions", json={
        "model": "doubao-2.1-turbo", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200, resp.text
    m.acquire_service_page.assert_awaited_once()
```

- [ ] **Step 2: 运行验证测试失败**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: 两个新测试 FAIL（`AttributeError: module has no attribute '_route_account'`）

- [ ] **Step 3: 实现（unified_server.py）**

**3a. 模块级**（`_KEEPALIVE_INTERVAL` 常量旁）：

```python
# ── Account routing (context pool): conversation -> owning account id ──
_conv_owner: Dict[str, str] = {}


def _route_account(client, conversation_id: Optional[str]) -> Optional[Any]:
    """Pick the account for a request: sticky by conversation, else next available.
    Returns None -> caller uses the default page (pool off / empty pool)."""
    if os.environ.get("DOUBAO_POOL_SIZE", "1") == "1":
        return None
    am = getattr(client, "account_manager", None)
    if not (am and am.accounts):
        return None
    if conversation_id:
        owner = _conv_owner.get(conversation_id)
        if owner:
            acc = am.get_account(owner)
            if acc and acc.status == "active":
                return acc
    return am.get_next_available_account()
```

**3b. chat 端点路由**（`client = _get_client()` 与 `_resolve_captcha_or_failover` 之后、materialize 之前，约 :1001）插入：

```python
        # ── Account-pool routing: pick this request's service page ──
        route_acc = _route_account(client, body.conversation_id)
        svc_page = None
        if route_acc:
            svc_page = await client.acquire_service_page(route_acc)
            if svc_page is None:
                log.warning("Pool routing fell back to default page (acquire failed for %s)", route_acc.id)
```

**3c. 传 page**：`_stream_chat`/`_collect_chat_response` 增加 `svc_page=None` 关键字参数并转传给 `client.chat_completion(..., page=svc_page)`；非流式分支同理。两处调用点（:1033 `_stream_chat(...)`、:1050/:1073 `_collect_chat_response(...)`）加 `svc_page=svc_page`。

**3d. 归属记录**：`_stream_chat` 与 `_collect_chat_response` 中已有的 `result_conversation_id` 提取点之后各加一行（两个函数都要；闭包访问模块级 dict 无需传参）：

```python
                if cid and cid != "0":
                    result_conversation_id = cid
                    if route_acc_id:
                        _conv_owner[cid] = route_acc_id
```

`route_acc_id` 由 chat 端点把 `route_acc.id if route_acc else None` 作为新关键字参数 `route_acc_id=None` 传入这两个生成器/函数（比传整个 acc 干净，mock 测试不受影响）。`_conv_owner` 防泄漏：条目超过 1024 时丢弃最旧一半（`# ponytail: half-evict over 1024 entries; LRU dict if multi-tenant ever matters`）：

```python
                    if len(_conv_owner) > 1024:
                        for k in list(_conv_owner)[:512]:
                            _conv_owner.pop(k, None)
```

**3e. 图片/视频/音乐端点**（`/v1/images/generations` :1120、`/v1/video/generations` :1373、`/v1/audio/generations` :1327）：同样在 `_resolve_captcha_or_failover` 后插 `route_acc`/`svc_page` 三行，并给 `client.generate_image/generate_video/generate_music(..., page=svc_page)` 加参。**上传类辅助（`_materialize_*`、`upload_*`）不路由**（Task 3 3d 决定：TOS 上传跨账号可用）。

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py -v`
Expected: `10 passed`
Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `67 passed, 1 failed`

- [ ] **Step 5: 提交**

```bash
git add tests/test_context_pool.py doubao2api/unified_server.py
git commit -m "feat(routing): DOUBAO_POOL_SIZE>1 时按账号路由请求到隔离 page，会话粘性归属"
```

---

### Task 6: 启动旗子（内存）+ README 文档

**Files:**
- Modify: `doubao2api/browser_client.py:478-490`（`launch_args`）
- Modify: `README.md`（环境变量表 + 多账号池一节补 `DOUBAO_POOL_SIZE` / `DOUBAO_KEEPALIVE_INTERVAL` / `DOUBAO_DRAIN_TIMEOUT`）
- Test: `tests/test_context_pool.py`（追加一行断言级检查）

**Interfaces:**
- Consumes: 无。
- Produces: 启动旗子 4 个；文档三个新环境变量条目。

- [ ] **Step 1: 写失败测试（追加到 tests/test_context_pool.py）**

```python
def test_launch_args_contain_memory_flags():
    import doubao2api.browser_client as bcmod
    src = open(bcmod.__file__, encoding="utf-8").read()
    assert '"--disable-gpu"' in src
    assert '"--disable-dev-shm-usage"' in src
    assert '"--js-flags=--max-old-space-size=256"' in src
    assert '"--disable-features=MediaRouter,Translate' in src
```

（源码断言够用：launch_args 是字面量列表，无行为可 mock。`ponytail:` 这是唯一一处源文本测试，值得。）

- [ ] **Step 2: 运行验证失败**

Run: `D:/python3132/python.exe -m pytest tests/test_context_pool.py::test_launch_args_contain_memory_flags -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`launch_args`（`browser_client.py:478`）：

```python
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # Headless-service memory trim; rendering quality is irrelevant here.
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-features=MediaRouter,Translate,back-forward-cache",
            # ponytail: render-heap cap; raise if doubao pages ever OOM.
            "--js-flags=--max-old-space-space-size=256",
        ]
```

**注意**：正确旗子拼写是 `--max-old-space-size`（无第二个 `space`）。上面最后一行以实际写入为准：

```python
            "--js-flags=--max-old-space-size=256",
```

README 环境变量表（`DOUBAO_VIDEO_TIMEOUT` 行后）加三行：

```markdown
| `DOUBAO_POOL_SIZE` | `1` | 同时保持的账号服务 context 数。`>1` 开启按账号路由（多账号真并行）；`=1` 时行为与单账号完全一致 |
| `DOUBAO_KEEPALIVE_INTERVAL` | `5` | SSE 空闲心跳间隔（秒） |
| `DOUBAO_DRAIN_TIMEOUT` | `150` | 切换账号前排空在途流的最长等待秒数 |
```

README「多账号池管理」一节补一段：

```markdown
#### 多账号真并行（可选）

默认情况下账号池仍是「轮流使用」：所有请求走同一个浏览器页面，切换账号需排空在途请求。
将 `DOUBAO_POOL_SIZE` 设为大于 1 后，每个账号获得独立的隐身 context（共享同一棵 Chromium
进程树，内存按并发付费而非按账号数付费），带 `conversation_id` 的会话自动粘住首轮账号，
触发验证码的账号其 context 直接报废重建——不再打断任何在途流，也无需人工 reload。
```

- [ ] **Step 4: 运行验证测试通过 + 全量回归**

Run: `D:/python3132/python.exe -m pytest tests/ -q`
Expected: `68 passed, 1 failed`

- [ ] **Step 5: 提交**

```bash
git add tests/test_context_pool.py doubao2api/browser_client.py README.md
git commit -m "feat(memory): 启动旗子削减空闲内存；README 补池化/心跳/排空三个环境变量"
```

---

### Task 7: 冒烟验收 —— 双账号真实并行（人工/半自动，唯一需要真实登录的一步）

**Files:**
- Create: `dev/SMOKE-pool.md`（验收记录，一步生成）
- 无代码改动。

**Interfaces:**
- Consumes: Task 1–6 全部。
- Produces: 一份可复查的实测记录（TTFT、并行度、内存、验证码逃生观察）。

- [ ] **Step 1: 准备两个账号**

`/admin` 账号池导入两个已登录 cookie 的账号（或扫码 + save_current 两轮）。

- [ ] **Step 2: 启动服务并开池**

```bash
set DOUBAO_POOL_SIZE=2
D:/python3132/python.exe -m doubao2api
```

（`.venv/Scripts/python.exe -m doubao2api` 亦可——运行时依赖在 venv 齐全。）

- [ ] **Step 3: 并行双请求冒烟**

两个终端同时（间隔 <1s）发流式请求，观察：

```bash
curl -N -X POST http://127.0.0.1:9090/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"doubao-2.1-turbo","stream":true,"messages":[{"role":"user","content":"数到10"}]}'
```

验收点（写入 `dev/SMOKE-pool.md`）：
1. 服务日志出现两条 `Service context ready for account ... pool=2/2`（两个不同账号各一个 context）；
2. 两条流**互不阻塞**（第二条的 TTFT 不因第一条未完成而等待——对比单页时代的串行行为）；
3. 同一 `conversation_id` 追问时日志显示路由到同一账号（粘性生效）；
4. 进程树内存 vs `DOUBAO_POOL_SIZE=1` 的差值（预期每多一个 context 增量 ~100–200MB，记录实测值）；
5. （可选，风险自负）人为触发一次验证码后观察：风控账号 context 被释放、后续请求无感切到另一账号、无 reload 日志。

- [ ] **Step 4: 提交验收记录**

```bash
git add dev/SMOKE-pool.md
git commit -m "docs: context 池双账号并行冒烟验收记录"
```

---

## 明确不做的事（YAGNI 记录）

- **每账号 inflight 限流 / 最小堆选号**：≤5 账号的 `get_next_available_account` 遍历足够；16k 账号规模再上堆。
- **上传按账号路由**：TOS 上传跨账号可用（全局存储），第一版固定默认页签名；出现跨账号上传失败再改。
- **`_conv_owner` 持久化**：重启后粘性丢失 = 第一轮重新分配，无害。
- **Tier-4 弹窗在 headless 下的替代方案**：无显示时弹窗本就失败，503 文案已含控制台指引。
- **ego-lite 集成**：已另行评估否决（GUI 桌面依赖、process-wide selectSpace、无内存收益）。

## Self-Review 结果

- **覆盖检查**：三目标全落到任务——真并行 = Task 3+5；无人值守 = Task 4（Tier-2 重试 + Tier-3 零成本逃生；「有界无人」的诚实上限在计划背景里写明）；内存 = Task 6 旗子 + Task 3 惰性池（内存随并发付费）。冒烟（Task 7）覆盖集成验收。
- **占位符扫描**：无 TBD/TODO；Task 3 3d 的穿透表逐方法列出了体内需要改的具体调用；Task 4/5 的测试与实现代码完整。Task 6 有一处笔误自查（`--max-old-space-size` 的错误拼写已在文中自我纠正并给出正确行）。
- **类型一致性**：`_sign_url(..., page)`/`_setup_fetch_bridge(page)`/`_browser_fetch_stream(..., page)`/`acquire_service_page(acc) -> Optional[Page]`/`release_service_context(account_id)`/`_route_account(client, conversation_id) -> Optional[Account]`/`_to_playwright_cookies(cookies) -> List[dict]` 在各 Task 间签名一致；`release_service_context` 统一用惰性 close 版本（Task 3 定义、Task 4 直接调用）；`route_acc_id` 作为字符串 id 传入生成器，与 `_conv_owner: Dict[str, str]` 一致。
- **回归风险自查**：所有穿透参数缺省 None → `self._page`，Task 1–3 期间任何现有测试不受影响；Task 4 修改旧测试语义处已同步给出替换断言；Task 5 的路由层在 `DOUBAO_POOL_SIZE=1`（缺省）下为 no-op。
