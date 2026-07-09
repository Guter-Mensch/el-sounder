"""
backstage/windows.py  –  el sounder

Everything that only makes sense on Windows: taskbar/app-ID quirks,
forcing the icon to actually show up, and finding it on disk (or
faking one) whether we're running from source or a PyInstaller build.
"""

from __future__ import annotations

import ctypes
import os
import sys

from PyQt6.QtCore    import Qt, QRectF
from PyQt6.QtGui     import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget


# ──────────────────────────────────────────────────────────────────
# Resource path helper
# ──────────────────────────────────────────────────────────────────
def resource_path(*parts: str) -> str:
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "resources")  # PyInstaller bundle root
    else:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources")
    return os.path.join(base, *parts)


# ──────────────────────────────────────────────────────────────────
# Fallback icon
# ──────────────────────────────────────────────────────────────────
def make_fallback_icon() -> QIcon:
    """
    Programmatic purple-circle icon at multiple sizes.
    Used when the .ico file cannot be found.
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        m = size * 0.0625
        painter.setBrush(QBrush(QColor("#cba6f7")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(m, m, size - 2 * m, size - 2 * m))
        d = size * 0.3125
        o = (size - d) / 2 - size * 0.046875
        painter.setBrush(QBrush(QColor(255, 255, 255, 160)))
        painter.drawEllipse(QRectF(o, o, d, d))
        painter.end()
        icon.addPixmap(pm)
    return icon


# ──────────────────────────────────────────────────────────────────
# Icon loader (cached)
# ──────────────────────────────────────────────────────────────────
_icon_cache: QIcon | None = None


def load_app_icon() -> QIcon:
    """
    Load the application icon from resources/icons/app.ico.
    Falls back to a programmatic icon if the file is missing or invalid.
    Result is cached after the first call.
    """
    global _icon_cache
    if _icon_cache is not None and not _icon_cache.isNull():
        return _icon_cache

    for candidate in ("icons/app.ico", "icons/app_icon.ico", "icons/icon.ico"):
        path = resource_path(candidate)
        if not os.path.exists(path):
            continue
        icon = QIcon(path)
        if icon.isNull():
            pm = QPixmap(path)
            if not pm.isNull():
                icon = QIcon(pm)
        if not icon.isNull():
            _icon_cache = icon
            return icon

    _icon_cache = make_fallback_icon()
    return _icon_cache


# ──────────────────────────────────────────────────────────────────
# AppUserModelID
# ──────────────────────────────────────────────────────────────────
APP_USER_MODEL_ID = "elsounder.overlay.1"


def apply_windows_app_id() -> None:
    """
    Set the Windows AppUserModelID.
    MUST be called before QApplication() is created.
    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────
# Taskbar icon fix
# ──────────────────────────────────────────────────────────────────
def force_taskbar_icon(widget: QWidget) -> None:
    """
    Forces Windows to display the correct icon in the taskbar immediately,
    even when the window has opacity=0 (during the intro fade).

    Qt's FramelessWindowHint + WA_TranslucentBackground causes Windows to
    treat the window as a "ghost" on startup and defer the taskbar icon
    until the window becomes visible. Fix: send WM_SETICON directly to the
    native HWND and refresh via ITaskbarList.
    """
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())

        icon_path = resource_path("icons", "app.ico")
        if not os.path.exists(icon_path):
            return

        IMAGE_ICON      = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE  = 0x00000040

        hicon_big = ctypes.windll.user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 256, 256, LR_LOADFROMFILE,
        )
        hicon_small = ctypes.windll.user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )

        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG   = 1

        if hicon_big:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG,   hicon_big)
        if hicon_small:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)

        # Force taskbar to re-read the icon via ITaskbarList
        try:
            ITaskbarList_CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
            ITaskbarList_IID   = "{56FDF342-FD6D-11d0-958A-006097C9A090}"
            import comtypes.client          # type: ignore
            tbl = comtypes.client.CreateObject(ITaskbarList_CLSID)
            tbl.HrInit()
            tbl.DeleteTab(hwnd)
            tbl.AddTab(hwnd)
        except Exception:
            pass  # comtypes not available — WM_SETICON is usually sufficient

    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────
# Convenience: set icon on all surfaces at once
# ──────────────────────────────────────────────────────────────────
def set_app_icon(app: QApplication, overlay: QWidget) -> QIcon:
    """
    Load and apply the app icon to QApplication, the overlay widget,
    and its system tray icon (if present).
    Returns the loaded icon.
    """
    icon = load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
        overlay.setWindowIcon(icon)
        tray = getattr(overlay, "_tray", None)
        if tray:
            tray.setIcon(icon)
    return icon
