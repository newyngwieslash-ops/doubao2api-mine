"""Host-environment workarounds applied before heavy imports.

Two Windows quirks break ``python -m doubao2api`` on some machines and are
handled here:

1. ``platform`` calls the ``_wmi`` accelerator when identifying the OS. On
   hosts where WMI is unavailable-but-hanging, that call blocks forever, and
   since ``aiohttp`` imports ``platform`` at module load the whole process
   hangs before logging starts.
2. Playwright creates temp directories under ``%TEMP%``. If the user profile
   path contains characters its mkdtemp implementation rejects (e.g. ``'``),
   browser launch fails with ``EPERM: operation not permitted, mkdtemp``.

Both are fixed by adjusting state before ``aiohttp``/``playwright`` load.
"""
from __future__ import annotations

import os
import sys
import tempfile


def _disable_wmi_platform_probe() -> None:
    """Make ``platform`` skip the WMI query and use the registry fallback."""
    if os.name != "nt" or "_wmi" in sys.modules:
        return
    try:
        import _wmi  # noqa: F401
    except ImportError:
        return

    import subprocess

    try:
        # The probe is trivial; if it cannot answer quickly it is unsafe to use.
        subprocess.run(
            [sys.executable, "-c", "import _wmi; print(_wmi.exec_query('SELECT Version FROM Win32_OperatingSystem')[:1])"],
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        sys.modules["_wmi"] = None


def _ensure_usable_tempdir() -> None:
    """Point Windows ``TEMP``/``TMP`` at a directory Playwright can use.

    Playwright's driver cannot mkdtemp under a Windows temp path containing
    characters such as ``'`` (common when the user profile name contains one),
    and the failure surfaces as ``EPERM`` at browser launch. Probing the path is
    not an option because the same operation hangs, so this is decided by path
    inspection alone.
    """
    if os.name != "nt":
        return

    win_temp = os.path.expandvars(os.environ.get("TEMP") or os.environ.get("TMP") or "")
    if win_temp and "'" not in win_temp and '"' not in win_temp:
        return

    fallback = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp"
    )
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        return

    os.environ["TEMP"] = fallback
    os.environ["TMP"] = fallback
    tempfile.tempdir = fallback


def _safe_profile_dir() -> None:
    """Replace the default browser profile dir when the home path is unusable.

    Chromium's persistent profile cannot be created under a path containing
    ``'`` (the case for Windows profiles such as ``C:\\Users\\o'brien``), where
    the launch hangs or fails with EPERM. ``DOUBAO_BROWSER_DATA`` is left
    untouched when the user set it explicitly.
    """
    if os.environ.get("DOUBAO_BROWSER_DATA"):
        return

    home_profile = os.path.join(os.path.expanduser("~"), ".doubao_browser")
    if "'" not in home_profile and '"' not in home_profile:
        return

    fallback = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".browser-data"
    )
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        return
    os.environ["DOUBAO_BROWSER_DATA"] = fallback


def apply() -> None:
    _disable_wmi_platform_probe()
    _ensure_usable_tempdir()
    _safe_profile_dir()
