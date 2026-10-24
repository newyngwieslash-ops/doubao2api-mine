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
