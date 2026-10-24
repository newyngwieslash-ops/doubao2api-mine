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
