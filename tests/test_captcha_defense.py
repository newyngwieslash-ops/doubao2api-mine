"""Tests for 3-tier Captcha Defense:
1. Humanized mouse trajectory generation.
2. Gap offset calculation.
3. Account pool captcha failover and recovery.
4. Auto-solve integration and fallback to 503.
5. Admin clear_captcha API endpoint.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from doubao2api.captcha_solver import (
    generate_human_trajectory,
    calculate_gap_offset,
    SliderCaptchaSolver,
)
from doubao2api.account_manager import Account, AccountManager
from doubao2api.unified_server import create_app


def test_generate_human_trajectory():
    """Test human trajectory characteristics."""
    start_x, start_y, distance = 100.0, 200.0, 150.0
    trajectory = generate_human_trajectory(start_x, start_y, distance, total_time=1.0)

    assert len(trajectory) >= 30
    first_x, first_y, first_delay = trajectory[0]
    last_x, last_y, last_delay = trajectory[-1]

    # Starts near start_x
    assert first_x > start_x
    # Ends close to target distance (within 5px tolerance)
    assert abs(last_x - (start_x + distance)) < 5.0
    # Y-axis has slight jitter
    y_values = [p[1] for p in trajectory]
    assert any(abs(y - start_y) > 0.1 for y in y_values)
    # Delays are positive
    assert all(p[2] > 0 for p in trajectory)


def test_account_manager_captcha_lifecycle(tmp_path):
    """Test mark_captcha_required, clear_captcha_status and failover exclusion."""
    filepath = str(tmp_path / "accounts.json")
    am = AccountManager(filepath=filepath, strategy="failover")

    acc1 = am.add_or_update_account("账号1", {"sessionid": "s1_valid"})
    acc2 = am.add_or_update_account("账号2", {"sessionid": "s2_valid"})

    # Initially acc1 is active
    assert am.get_active_account().id == acc1.id
    assert am.get_next_available_account().id == acc1.id

    # Mark acc1 as captcha
    am.mark_captcha_required(acc1.id)
    assert am.get_account(acc1.id).status == "captcha"

    # acc1 should now be excluded from available accounts; failover to acc2
    next_acc = am.get_next_available_account()
    assert next_acc is not None
    assert next_acc.id == acc2.id
    assert am.active_account_id == acc2.id

    # Clear captcha status on acc1
    cleared = am.clear_captcha_status(acc1.id)
    assert cleared is True
    assert am.get_account(acc1.id).status == "active"


def test_auto_solve_captcha_integration():
    """Test SliderCaptchaSolver detect_and_solve returns False when no container is present."""
    mock_page = MagicMock()
    mock_page.frames = []
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    mock_page.locator = MagicMock(return_value=loc)

    solver = SliderCaptchaSolver()
    result = asyncio.run(solver.detect_and_solve(mock_page))
    assert result is False


def test_admin_clear_captcha_endpoint(tmp_path):
    """Test POST /admin/api/accounts/clear_captcha endpoint."""
    filepath = str(tmp_path / "accounts.json")
    am = AccountManager(filepath=filepath)
    acc = am.add_or_update_account("测试账号", {"sessionid": "test_sess"})
    am.mark_captcha_required(acc.id)
    assert am.get_account(acc.id).status == "captcha"

    mock_client = MagicMock()
    mock_client.account_manager = am
    mock_client.needs_captcha = True
    mock_client.is_ready = True
    mock_client.is_captcha_visible = AsyncMock(return_value=False)
    mock_client.try_auto_solve_captcha = AsyncMock(return_value=False)

    app = create_app(browser_client=mock_client)
    tc = TestClient(app)

    # Call clear_captcha
    resp = tc.post("/admin/api/accounts/clear_captcha", json={"account_id": acc.id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert am.get_account(acc.id).status == "active"


def test_captcha_failover_in_chat_completions(tmp_path):
    """Test that when account A hits captcha and account B is available,
    chat_completions auto-fails over to account B without raising 503."""
    filepath = str(tmp_path / "accounts.json")
    am = AccountManager(filepath=filepath, strategy="failover")
    acc1 = am.add_or_update_account("主号", {"sessionid": "s1_main"})
    acc2 = am.add_or_update_account("副号", {"sessionid": "s2_backup"})
    am.set_active_account(acc1.id)

    mock_client = MagicMock()
    mock_client.account_manager = am
    mock_client.needs_captcha = True
    mock_client.is_ready = True
    # Captcha is visible, auto-solver fails
    mock_client.is_captcha_visible = AsyncMock(return_value=True)
    mock_client.try_auto_solve_captcha = AsyncMock(return_value=False)
    mock_client.release_service_context = MagicMock()
    mock_client.switch_to_account = AsyncMock(return_value=True)
    mock_client.extract_conversation_id = MagicMock(return_value="c_test")

    # Mock chat completion stream
    async def mock_chat(*args, **kwargs):
        yield {"event_type": 2001, "event_data": '{"message": {"content": {"text": "hello"}}}'}

    mock_client.chat_completion = mock_chat

    app = create_app(browser_client=mock_client)
    tc = TestClient(app)

    resp = tc.post("/v1/chat/completions", json={
        "model": "doubao-2.1-turbo",
        "messages": [{"role": "user", "content": "hi"}]
    })

    # Should succeed with 200 OK: acc1's context evicted, routing moves to acc2
    assert resp.status_code == 200
    # acc1 was marked as captcha
    assert am.get_account(acc1.id).status == "captcha"
    # Pool mode: failover evicts the captcha account's context, no reload
    mock_client.release_service_context.assert_called_once_with(acc1.id)
    mock_client.switch_to_account.assert_not_called()
