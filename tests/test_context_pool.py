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
