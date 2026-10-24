"""Tests for the per-account service context pool.

The pool routes each API request to an isolated incognito context on the same
Chromium process tree, keyed by account. The persistent context keeps its
login/admin role untouched (self._page). All new methods must fall back to
self._page when no page is passed, byte-identical with today's behavior.
"""

import asyncio
import time
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
    assert default_page in bc._bridged_pages

    # expose_function is context-scoped: a new page must register its own bridge
    asyncio.run(bc._setup_fetch_bridge(page=explicit))
    assert registered == ["__doubaoStreamChunk", "__doubaoStreamChunk"]
    assert explicit in bc._bridged_pages
    assert default_page in bc._bridged_pages

    # re-registering the same page is idempotent (no duplicate expose_function)
    asyncio.run(bc._setup_fetch_bridge(page=explicit))
    assert registered == ["__doubaoStreamChunk", "__doubaoStreamChunk"]

    # the hoisted callback routes chunks by request_id, page-agnostic
    q_key = "req_test"
    bc._stream_queues[q_key] = asyncio.Queue()
    asyncio.run(bc._on_stream_chunk(q_key, "chunk"))
    assert bc._stream_queues[q_key].get_nowait() == "chunk"


def test_browser_fetch_stream_accepts_page(monkeypatch):
    bc = _bc()
    default_page = MagicMock()
    default_page.evaluate = AsyncMock(return_value=None)
    explicit = MagicMock()
    explicit.evaluate = AsyncMock(return_value=None)

    bc._page = default_page
    payload = {"a": 1}

    # default goes to self._page, explicit goes to the passed page
    asyncio.run(bc._browser_fetch_stream("http://x", payload, "r1"))
    default_page.evaluate.assert_awaited()

    asyncio.run(bc._browser_fetch_stream("http://x", payload, "r2", page=explicit))
    explicit.evaluate.assert_awaited()


def test_to_playwright_cookies_shape():
    bc = _bc()
    pw = bc._to_playwright_cookies({"sessionid": "s1", "msToken": "m1"})
    assert pw == [
        {"name": "sessionid", "value": "s1", "domain": ".doubao.com", "path": "/"},
        {"name": "msToken", "value": "m1", "domain": ".doubao.com", "path": "/"},
    ]


def test_acquire_service_page_creates_per_account_context(monkeypatch):
    # pool of 2: both accounts fit, so no LRU eviction interferes with the
    # per-account-context assertions below.
    monkeypatch.setenv("DOUBAO_POOL_SIZE", "2")
    bc = _bc()
    from doubao2api.account_manager import Account

    created = []

    async def fake_goto(url, **kw):
        return None

    class FakeCtx:
        def __init__(self):
            self.cookies_added = None

        async def add_cookies(self, c):
            self.cookies_added = c

        async def new_page(self):
            page = MagicMock()
            page.is_closed = lambda: False
            page.goto = fake_goto
            return page

        async def close(self):
            if self in created:
                created.remove(self)

    class FakeBrowser:
        async def new_context(self, **kw):
            ctx = FakeCtx()
            created.append(ctx)
            return ctx

    async def fake_bridge(page=None):
        return None

    async def fake_signing(page=None):
        return None

    # instance-level fakes: acquire_service_page still routes through these
    bc._setup_fetch_bridge = fake_bridge
    bc._wait_for_signing = fake_signing

    bc._context = MagicMock()
    bc._context.browser = FakeBrowser()
    bc._ready = True

    acc = Account(id="a1", name="acc1", cookies={"sessionid": "s1"})

    page1 = asyncio.run(bc.acquire_service_page(acc))
    page2 = asyncio.run(bc.acquire_service_page(acc))
    assert page1 is page2                      # same account -> cached page
    assert len(created) == 1                   # one context for that account
    assert created[0].cookies_added[0]["name"] == "sessionid"
    assert created[0].cookies_added[0]["domain"] == ".doubao.com"

    acc2 = Account(id="a2", name="acc2", cookies={"sessionid": "s2"})
    page3 = asyncio.run(bc.acquire_service_page(acc2))
    assert len(created) == 2                    # second account -> second context
    assert page3 is not page1

    # release closes and evicts only the targeted account's context
    bc.release_service_context("a1")
    assert "a1" not in bc._svc
    assert bc._svc["a2"]["page"] is page3       # untouched account stays pooled


def test_acquire_returns_none_when_context_dead():
    bc = _bc()
    bc._context = None   # browser not started
    from doubao2api.account_manager import Account
    acc = Account(id="a1", name="acc1", cookies={"sessionid": "s1"})
    assert asyncio.run(bc.acquire_service_page(acc)) is None


def test_chat_completion_rebinds_page_after_restart():
    """Regression: chat_completion must re-read self._page after the default-page
    self-heal restart, so a fresh default page reaches _sign_url/_browser_fetch_stream.

    Pre-pool behavior bound the default page at call time and recovered
    transparently; binding `pg` before the restart block passed the stale dead
    page to the signer (sign fails 3x -> RuntimeError). An explicit `page`
    must stay untouched by the heal.
    """
    bc = _bc()

    dead_page = MagicMock()
    dead_page.is_closed = lambda: True
    fresh_page = MagicMock()
    fresh_page.is_closed = lambda: False
    fresh_page.evaluate = AsyncMock(return_value={"a_bogus": "fresh"})

    bc._page = dead_page
    captured = {}

    async def fake_restart():
        bc._page = fresh_page

    async def fake_sign_url(path, params, page=None):
        captured["page"] = page
        return "http://signed"

    bc.is_alive = AsyncMock(return_value=False)
    bc.restart = AsyncMock(side_effect=fake_restart)
    bc._sign_url = AsyncMock(side_effect=fake_sign_url)
    bc._browser_fetch_stream = AsyncMock()

    async def scenario():
        gen = bc.chat_completion("hi")
        try:
            # Generator suspends on the 180s queue wait; 0.5s in, _sign_url has
            # already been awaited with whichever page `pg` resolved to.
            await asyncio.wait_for(gen.__anext__(), timeout=0.5)
        except TimeoutError:
            pass
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass
        return captured.get("page")

    page_used = asyncio.run(scenario())
    assert page_used is fresh_page, (
        "chat_completion signed with a stale dead page instead of the "
        "post-restart default page"
    )


def test_try_auto_solve_retries_once(monkeypatch):
    bc = _bc()
    bc._page = MagicMock()
    calls = {"n": 0}

    class FlakySolver:
        async def detect_and_solve(self, page, timeout=8.0):
            calls["n"] += 1
            return calls["n"] >= 2   # fails first, succeeds second

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


def test_routing_sticky_by_conversation_and_falls_back(monkeypatch):
    import doubao2api.unified_server as us
    from doubao2api.account_manager import Account, AccountManager

    # filepath=":" makes load() a no-op (path never exists); accounts are set
    # by hand and save() must never be triggered — ":" is unwritable.
    am = AccountManager(filepath=":")
    acc1 = Account(id="a1", name="1", cookies={"sessionid": "s"})
    acc2 = Account(id="a2", name="2", cookies={"sessionid": "s"})
    am.accounts = {"a1": acc1, "a2": acc2}
    am.active_account_id = "a1"

    client = MagicMock()
    client.account_manager = am

    # default env (no DOUBAO_POOL_SIZE) -> routing disabled -> None
    monkeypatch.delenv("DOUBAO_POOL_SIZE", raising=False)
    assert us._route_account(client, None) is None

    monkeypatch.setenv("DOUBAO_POOL_SIZE", "1")
    assert us._route_account(client, None) is None        # pool off -> no-op

    monkeypatch.setenv("DOUBAO_POOL_SIZE", "2")
    assert us._route_account(client, "c_new") is not None  # new conv -> any account
    assert us._route_account(client, "c_new").id in ("a1", "a2")

    # sticky: existing conversation owned by a2 returns a2 even if a1 active
    us._conv_owner["c_sticky"] = "a2"
    assert us._route_account(client, "c_sticky").id == "a2"

    # owner flagged captcha -> falls through to next available
    acc2.status = "captcha"   # bypass mark_captcha_required's save()
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


def test_chat_completion_skips_default_page_restart_for_explicit_page():
    """C1: with an explicit (pool) page, chat_completion must NOT run the
    default-page liveness check / restart — restart() stops the whole browser
    and would destroy the caller's pool page. Pool-page death is handled by
    the pool (acquire recreates; a dead page mid-request fails the request)."""
    bc = _bc()

    dead_page = MagicMock()
    dead_page.is_closed = lambda: True
    explicit_page = MagicMock()
    explicit_page.is_closed = lambda: False

    bc._page = dead_page
    captured = {}

    async def boom_restart():
        raise AssertionError("restart() must not be called for explicit page")

    async def fake_sign_url(path, params, page=None):
        captured["page"] = page
        return "http://signed"

    bc.is_alive = AsyncMock(return_value=False)
    bc.restart = AsyncMock(side_effect=boom_restart)
    bc._sign_url = AsyncMock(side_effect=fake_sign_url)
    bc._browser_fetch_stream = AsyncMock()

    async def scenario():
        gen = bc.chat_completion("hi", page=explicit_page)
        try:
            # Generator suspends on the 180s queue wait; 0.5s in, _sign_url has
            # already been awaited with whichever page `pg` resolved to.
            await asyncio.wait_for(gen.__anext__(), timeout=0.5)
        except TimeoutError:
            pass
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass
        return captured.get("page")

    page_used = asyncio.run(scenario())
    bc.restart.assert_not_called()
    assert page_used is explicit_page, (
        "chat_completion must sign with the explicit page, not the default page"
    )


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


def test_launch_args_contain_memory_flags():
    import doubao2api.browser_client as bcmod
    src = open(bcmod.__file__, encoding="utf-8").read()
    assert '"--disable-gpu"' in src
    assert '"--disable-dev-shm-usage"' in src
    assert '"--js-flags=--max-old-space-size=256"' in src
    assert '"--disable-features=MediaRouter,Translate' in src


# ---------------------------------------------------------------------------
# F1 regression: pool-mode 即用即焚 auto-delete must run on the routed page
# ---------------------------------------------------------------------------

def test_delayed_delete_threads_svc_page_non_stream(monkeypatch):
    """F1: pool-mode auto-delete must call delete_conversation with the request's
    svc_page — the ephemeral conversation lives on the pool account's page, and
    deleting with the default account's cookies silently fails."""
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
    m.extract_conversation_id = MagicMock(side_effect=lambda ev: (
        ev.get("ack_client_meta", {}) or {}).get("conversation_id"))

    captured_delete = {}

    async def fake_delete(conversation_id, page=None):
        captured_delete["conversation_id"] = conversation_id
        captured_delete["page"] = page
        return True

    m.delete_conversation = fake_delete

    async def fake_chat(*args, **kwargs):
        assert kwargs.get("page") is svc_page
        yield {"_event": "CHUNK_DELTA", "text": "ok",
               "ack_client_meta": {"conversation_id": "conv_pool_1"}}

    m.chat_completion = fake_chat

    tc = TestClient(create_app(browser_client=m))
    with tc:
        resp = tc.post("/v1/chat/completions", json={
            "model": "doubao-2.1-turbo", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json().get("conversation_id") == "conv_pool_1"

        # The background delayed-delete task sleeps 0.8s inside the app's event
        # loop; keep the lifespan (its loop) alive until it lands.
        time.sleep(1.2)

    assert captured_delete.get("conversation_id") == "conv_pool_1", (
        "delayed auto-delete never fired"
    )
    assert captured_delete.get("page") is svc_page, (
        "pool-mode auto-delete must pass the routed svc_page to "
        "delete_conversation, not the default page"
    )


def test_delayed_delete_threads_svc_page_stream(monkeypatch):
    """F1 (stream path): _stream_chat's trailing auto-delete must carry svc_page."""
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
    m.extract_conversation_id = MagicMock(side_effect=lambda ev: (
        ev.get("ack_client_meta", {}) or {}).get("conversation_id"))

    captured_delete = {}

    async def fake_delete(conversation_id, page=None):
        captured_delete["page"] = page
        return True

    m.delete_conversation = fake_delete

    async def fake_chat(*args, **kwargs):
        yield {"_event": "CHUNK_DELTA", "text": "ok",
               "ack_client_meta": {"conversation_id": "conv_stream_1"}}

    m.chat_completion = fake_chat

    tc = TestClient(create_app(browser_client=m))
    with tc:
        with tc.stream("POST", "/v1/chat/completions", json={
            "model": "doubao-2.1-turbo", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }) as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_raw())

        assert b"conv_stream_1" in body
        # keep the app's event loop alive for the 0.8s delayed-delete task
        time.sleep(1.2)

    assert captured_delete.get("page") is svc_page, (
        "stream-path auto-delete must run on the routed svc_page"
    )


def test_video_ephemeral_delete_threads_svc_page(monkeypatch):
    """F1 (video endpoint): ephemeral video conversation cleanup must run on
    the routed svc_page."""
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

    captured = {}

    async def fake_generate_video(*args, **kwargs):
        captured["gen_page"] = kwargs.get("page")
        return {"videos": [{"video_url": "https://tos.mock/v.mp4"}],
                "conversation_id": "conv_video_1"}

    async def fake_delete(conversation_id, page=None):
        captured["del_page"] = page
        return True

    m.generate_video = fake_generate_video
    m.delete_conversation = fake_delete

    tc = TestClient(create_app(browser_client=m))
    resp = tc.post("/v1/video/generations", json={
        "prompt": "森林日出", "ratio": "16:9",
    })
    assert resp.status_code == 200, resp.text
    assert captured["gen_page"] is svc_page
    assert captured["del_page"] is svc_page, (
        "video ephemeral cleanup must delete on the routed svc_page"
    )


# ---------------------------------------------------------------------------
# F2 regression: pool-mode video quota must not switch_to_account / mark wrong
# ---------------------------------------------------------------------------

def test_generate_video_quota_on_pool_page_marks_routed_account_only():
    """F2: with an explicit (pool) page, a quota error must mark the ROUTED
    account (reverse-looked-up from _svc), never switch_to_account (which
    reloads the default page and would cascade false quota flags), and raise
    a structured error for the endpoint layer."""
    import json as _json
    bc = _bc()

    pool_page = MagicMock()
    bc._svc["acc_routed"] = {"context": MagicMock(), "page": pool_page}

    from doubao2api.account_manager import Account, AccountManager
    am = AccountManager(filepath=":")
    acc_default = Account(id="acc_default", name="默认", cookies={"sessionid": "s"})
    acc_routed = Account(id="acc_routed", name="路由号", cookies={"sessionid": "s"})
    acc_other = Account(id="acc_other", name="备用", cookies={"sessionid": "s"})
    am.accounts = {a.id: a for a in (acc_default, acc_routed, acc_other)}
    am.active_account_id = "acc_default"
    bc.account_manager = am

    async def fake_samantha_request(payload, timeout=60, page=None):
        # 2005 event carrying the Doubao quota-exceeded text
        sse_event = {"event_type": 2005, "event_data": "今日生成次数已达上限"}
        return f"data: {_json.dumps(sse_event, ensure_ascii=False)}\n\n"

    bc._samantha_request = fake_samantha_request
    bc._list_recent_thread_ids = AsyncMock(return_value=[])
    bc.switch_to_account = AsyncMock(side_effect=AssertionError(
        "switch_to_account must not run for pool-routed video quota errors"))

    with pytest.raises(RuntimeError, match="quota exceeded.*acc_routed"):
        asyncio.run(bc.generate_video(prompt="日落", page=pool_page))

    bc.switch_to_account.assert_not_called()
    # routed account flagged, default/other accounts untouched
    assert am.get_account("acc_routed").status == "quota_exceeded"
    assert am.get_account("acc_default").status == "active"
    assert am.get_account("acc_other").status == "active"


def test_generate_video_quota_on_default_page_keeps_legacy_failover():
    """F2 guard: default-page (page=None) quota errors keep the legacy
    mark-active + switch_to_account failover, byte-identical with pre-pool."""
    import json as _json
    bc = _bc()

    from doubao2api.account_manager import Account, AccountManager
    am = AccountManager(filepath=":")
    acc_default = Account(id="acc_default", name="默认", cookies={"sessionid": "s"})
    acc_other = Account(id="acc_other", name="备用", cookies={"sessionid": "s"})
    am.accounts = {a.id: a for a in (acc_default, acc_other)}
    am.active_account_id = "acc_default"
    bc.account_manager = am

    calls = {"n": 0}

    async def fake_samantha_request(payload, timeout=60, page=None):
        calls["n"] += 1
        if calls["n"] == 1:
            sse_event = {"event_type": 2005, "event_data": "今日生成次数已达上限"}
            return f"data: {_json.dumps(sse_event, ensure_ascii=False)}\n\n"
        # Retry succeeds with an async task
        sse_event = {
            "event_type": 2001,
            "event_data": {"fin_reason": {"reason": 1, "async_task": {"id": "task_ok"}}},
        }
        return f"data: {_json.dumps(sse_event)}\n\n"

    bc._samantha_request = fake_samantha_request
    bc._list_recent_thread_ids = AsyncMock(return_value=[])
    bc.switch_to_account = AsyncMock(return_value=True)
    bc._poll_video_result = AsyncMock(return_value={"videos": [{"video_url": "u.mp4"}], "prompt": "日落"})

    res = asyncio.run(bc.generate_video(prompt="日落"))
    assert res["videos"][0]["video_url"] == "u.mp4"
    bc.switch_to_account.assert_awaited_once()
    assert am.get_account("acc_default").status == "quota_exceeded"


def test_generate_video_quota_full_text_on_pool_page_no_recursion():
    """F2 (full_text branch): quota detected in the completion text on a pool
    page must mark the routed account and raise — no switch, no recursion."""
    import json as _json
    bc = _bc()

    pool_page = MagicMock()
    bc._svc["acc_routed"] = {"context": MagicMock(), "page": pool_page}

    from doubao2api.account_manager import Account, AccountManager
    am = AccountManager(filepath=":")
    acc_default = Account(id="acc_default", name="默认", cookies={"sessionid": "s"})
    acc_routed = Account(id="acc_routed", name="路由号", cookies={"sessionid": "s"})
    am.accounts = {a.id: a for a in (acc_default, acc_routed)}
    am.active_account_id = "acc_default"
    bc.account_manager = am

    async def fake_samantha_request(payload, timeout=60, page=None):
        # 2001 event whose message text carries the quota-exceeded wording
        inner = _json.dumps({"text": "您的视频生成次数已达上限，请明天再试"}, ensure_ascii=False)
        sse_event = {
            "event_type": 2001,
            "event_data": {"message": {"content_type": 2001, "content": inner}},
        }
        return f"data: {_json.dumps(sse_event, ensure_ascii=False)}\n\n"

    bc._samantha_request = fake_samantha_request
    bc._list_recent_thread_ids = AsyncMock(return_value=[])
    bc.switch_to_account = AsyncMock(side_effect=AssertionError(
        "switch_to_account must not run for pool-routed video quota errors"))

    with pytest.raises(RuntimeError, match="quota exceeded.*acc_routed"):
        asyncio.run(bc.generate_video(prompt="日落", page=pool_page))

    bc.switch_to_account.assert_not_called()
    assert am.get_account("acc_routed").status == "quota_exceeded"
    assert am.get_account("acc_default").status == "active"


def test_svc_account_id_for_page_lookup():
    bc = _bc()
    page_a = MagicMock()
    page_b = MagicMock()
    bc._svc["a"] = {"context": MagicMock(), "page": page_a}
    bc._svc["b"] = {"context": MagicMock(), "page": page_b}

    assert bc._svc_account_id_for_page(None) is None     # default page
    assert bc._svc_account_id_for_page(page_a) == "a"
    assert bc._svc_account_id_for_page(page_b) == "b"
    assert bc._svc_account_id_for_page(MagicMock()) is None  # unknown page


def test_pool_size_endpoint_reads_and_sets_runtime(monkeypatch):
    """POOL_SIZE must be a runtime switch: the routing gate reads
    os.environ at each request, so no restart is needed."""
    import os
    from fastapi.testclient import TestClient
    from doubao2api.unified_server import create_app

    monkeypatch.setenv("DOUBAO_POOL_SIZE", "1")
    client = MagicMock()
    client._pool_size = 1
    tc = TestClient(create_app(browser_client=client))

    r = tc.get("/admin/api/config/pool-size")
    assert r.status_code == 200
    assert r.json()["pool_size"] == 1

    r = tc.post("/admin/api/config/pool-size", json={"pool_size": 2})
    assert r.status_code == 200
    assert r.json()["pool_size"] == 2
    assert os.environ["DOUBAO_POOL_SIZE"] == "2"   # routing gate sees it immediately
    assert client._pool_size == 2                  # context pool cap follows

    # invalid values are rejected, state unchanged
    assert tc.post("/admin/api/config/pool-size", json={"pool_size": 0}).status_code == 400
    assert tc.post("/admin/api/config/pool-size", json={"pool_size": "x"}).status_code == 400
    assert os.environ["DOUBAO_POOL_SIZE"] == "2"


def test_acquire_is_single_flight_per_account():
    """Two concurrent acquires for the same account must create ONE context.
    Before the per-account lock they both saw a cache miss and each built a
    context, leaking the loser (observed live: two 'pool=1/2' log lines)."""
    bc = _bc()
    created = []

    class FakeCtx:
        def __init__(self):
            self.page = MagicMock()
            self.page.is_closed = lambda: False
            self.page.goto = AsyncMock(return_value=None)

        async def add_cookies(self, c):
            pass

        async def new_page(self):
            return self.page

        async def close(self):
            pass

    class FakeBrowser:
        async def new_context(self, **kw):
            await asyncio.sleep(0.05)     # widen the race window
            ctx = FakeCtx()
            created.append(ctx)
            return ctx

    bc._context = MagicMock()
    bc._context.browser = FakeBrowser()
    bc._pool_size = 2

    async def fake_goto(url, **kw):
        await asyncio.sleep(0.05)

    async def fake_bridge(page=None):
        return None

    async def fake_signing(page=None):
        return None

    bc._setup_fetch_bridge = fake_bridge
    bc._wait_for_signing = fake_signing

    acc = MagicMock()
    acc.id = "a1"
    acc.name = "acc1"
    acc.cookies = {"sessionid": "s1"}

    async def scenario():
        p1, p2 = await asyncio.gather(
            bc.acquire_service_page(acc),
            bc.acquire_service_page(acc),
        )
        return p1, p2

    p1, p2 = asyncio.run(scenario())
    assert len(created) == 1, f"expected 1 context, got {len(created)} (leak)"
    assert p1 is p2


def test_pool_size_persists_to_env_file(tmp_path, monkeypatch):
    """A UI change must survive a restart: the endpoint writes the key back to
    .env (rewriting that line only, preserving comments and other keys)."""
    import os
    import doubao2api.unified_server as us

    env = tmp_path / ".env"
    env.write_text("# comment stays\nDOUBAO_PORT=9090\nDOUBAO_POOL_SIZE=1\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr(us, "__file__", str(tmp_path / "pkg" / "unified_server.py"))
    (tmp_path / "pkg").mkdir(exist_ok=True)

    us._persist_env("DOUBAO_POOL_SIZE", "3")
    text = env.read_text(encoding="utf-8")
    assert "DOUBAO_POOL_SIZE=3" in text
    assert "# comment stays" in text          # comments preserved
    assert "DOUBAO_PORT=9090" in text         # other keys preserved
    assert "OTHER=keep" in text

    # absent key is appended
    us._persist_env("DOUBAO_POOL_SIZE", "2")
    assert "DOUBAO_POOL_SIZE=2" in env.read_text(encoding="utf-8")
    assert env.read_text(encoding="utf-8").count("DOUBAO_POOL_SIZE") == 1
