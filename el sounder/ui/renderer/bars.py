"""
ui/renderer/bars.py  –  el sounder
=====================================
Draws the frequency bar / wave visualizer ring.
"""

from __future__ import annotations

import math

import numpy as np

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui  import QColor, QPainter, QPen

from config.settings import hex_to_qcolor, lerp_color

HUE_MAX = 0.80


def _color_for_config(
    mode: str,
    solid_hex: str,
    grad_from: str,
    grad_to: str,
    grad_dir: str,
    i: int,
    nb: int,
    v: float,
) -> QColor:
    if mode == "solid":
        return hex_to_qcolor(solid_hex)
    if mode == "gradient":
        c_from = hex_to_qcolor(grad_from)
        c_to   = hex_to_qcolor(grad_to)
        t      = (i / max(nb - 1, 1)) if grad_dir == "freq" else float(v)
        return lerp_color(c_from, c_to, t)
    # rainbow
    hue = (i / nb) * HUE_MAX
    return QColor.fromHsvF(hue, 0.85, 1.0)


def _bar_color(s, i: int, nb: int, v: float) -> QColor:
    """Resolve the bar colour for index i, optionally blending two configs."""
    new_col = _color_for_config(
        s.rendered_color_mode, s.rendered_color_hex,
        s.rendered_grad_from,  s.rendered_grad_to, s.rendered_grad_dir,
        i, nb, v,
    )
    if not s.color_blending:
        return new_col
    old_col = _color_for_config(
        s.prev_color_mode, s.prev_color_hex,
        s.prev_grad_from,  s.prev_grad_to, s.prev_grad_dir,
        i, nb, v,
    )
    return lerp_color(old_col, new_col, s.color_blend)


def draw_bars(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    s,                      # VisualState
) -> None:
    """
    Draw frequency bars (or wave) around the circle.

    Args:
        p:      Active QPainter.
        cx, cy: Widget centre.
        r:      Circle radius.
        s:      VisualState instance (read-only).
    """
    nb        = len(s.bands)
    if nb == 0:
        return

    bar_w     = max(0.8, 3.0 * (90 / nb))
    bands_now = s.bands
    vol_scale = (max(0.0, s.db_smooth) / 100.0) ** 0.55

    p.save()
    p.setOpacity(s.bars_alpha / 255.0)

    if s.rendered_bar_style == "wave":
        pts = []
        for i, v in enumerate(bands_now):
            a   = (i / nb) * math.pi * 2 - math.pi / 2
            rad = r + 5 + float(v) * 120 * vol_scale
            pts.append(QPointF(cx + math.cos(a) * rad, cy + math.sin(a) * rad))
        for i in range(nb):
            p1  = pts[i]
            p2  = pts[(i + 1) % nb]
            col = _bar_color(s, i, nb, float(bands_now[i]))
            p.setPen(QPen(col, 2.0, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawLine(p1, p2)
    else:
        for i, v in enumerate(bands_now):
            a  = (i / nb) * math.pi * 2 - math.pi / 2
            r1 = r + 5
            r2 = r1 + float(v) * 120 * vol_scale
            x1 = cx + math.cos(a) * r1
            y1 = cy + math.sin(a) * r1
            x2 = cx + math.cos(a) * r2
            y2 = cy + math.sin(a) * r2
            col = _bar_color(s, i, nb, float(v))
            p.setPen(QPen(col, bar_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

    p.restore()
