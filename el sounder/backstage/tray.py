"""
backstage/tray.py  –  el sounder

Builds the system tray icon + its little right-click menu. Kept out of
Overlay so that file can stay focused on drawing, not OS plumbing.
"""

from __future__ import annotations

from PyQt6.QtGui     import QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon, QWidget
from PyQt6.QtCore    import QObject

from config.settings    import PersistentMenu
from config.translations import tr


def build_tray(
    icon: QIcon,
    parent: QWidget,
    on_show_hide,
    on_quit,
    language: str = "en_us",
) -> QSystemTrayIcon:
    """
    Build and return a fully configured QSystemTrayIcon.

    Args:
        icon:         Application icon.
        parent:       Parent widget (Overlay).
        on_show_hide: Callable – toggles window visibility.
        on_quit:      Callable – starts the outro / quit sequence.
        language:     Current UI language key.
    """
    tray = QSystemTrayIcon(icon, parent)

    tray_menu = QMenu()
    tray_menu.setStyleSheet(PersistentMenu._STYLE)

    show_act = tray_menu.addAction(f"el sounder")
    show_act.triggered.connect(on_show_hide)

    tray_menu.addSeparator()

    quit_act = tray_menu.addAction(tr(language, "quit"))
    quit_act.triggered.connect(on_quit)

    tray.setContextMenu(tray_menu)
    tray.setToolTip("el sounder")
    tray.activated.connect(
        lambda reason: on_show_hide()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )

    return tray
