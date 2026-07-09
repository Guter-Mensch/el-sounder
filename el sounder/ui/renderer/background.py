"""
ui/renderer/background.py  –  el sounder
===========================================
Draws the circular background behind everything else.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui  import QBrush, QColor, QPainter, QPen


def draw_background(p: QPainter, cx: float, cy: float, r: float, opacity_mode: str) -> None:
    """
    Draw the background circle (or full-window chroma fill).

    Args:
        p:            Active QPainter.
        cx, cy:       Widget center coordinates.
        r:            Circle radius.
        opacity_mode: One of milky | gray | transparent | chroma_green | chroma_black
    """
    if opacity_mode == "chroma_green":
        p.setBrush(QColor(0, 255, 0, 255))
        p.setPen(Qt.PenStyle.NoPen)
        # Fill entire widget — caller should pass widget dimensions for rect
        # We draw a very large ellipse as a practical equivalent
        p.drawRect(int(cx - r * 10), int(cy - r * 10), int(r * 20), int(r * 20))
        return

    if opacity_mode == "chroma_black":
        p.setBrush(QColor(0, 0, 0, 255))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(int(cx - r * 10), int(cy - r * 10), int(r * 20), int(r * 20))
        return

    if opacity_mode == "transparent":
        p.setBrush(QColor(0, 0, 0, 1))
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
    elif opacity_mode == "gray":
        p.setBrush(QColor(60, 60, 60, 255))
        p.setPen(QPen(QColor(140, 140, 140, 255), 2))
    else:  # milky (default)
        p.setBrush(QColor(255, 255, 255, 25))
        p.setPen(QPen(QColor(255, 255, 255, 80), 2))

    p.drawEllipse(int(cx - r), int(cy - r), r * 2, r * 2)
