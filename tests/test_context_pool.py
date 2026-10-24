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
