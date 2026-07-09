"""
ui/renderer/arm.py  –  el sounder
====================================
Draws the vinyl tone arm (needle arm + counterweight + cartridge).
"""

from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui  import QColor, QPainter, QPainterPath, QPen


def draw_arm(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    arm_alpha: float,
    arm_angle: float,
) -> None:
    """
    Render the tone arm.

    Args:
        p:          Active QPainter.
        cx, cy:     Widget centre.
        r:          Disc radius (used for geometry scaling).
        arm_alpha:  Opacity 0.0–1.0.
        arm_angle:  Current angle in degrees.
    """
    if arm_alpha < 0.001:
        return

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    px  = cx + r * 1.04
    py  = cy - r * 0.50
    arm_len   = r * 1.05
    angle_rad = math.radians(arm_angle)
    cos_a     = math.cos(angle_rad)
    sin_a     = math.sin(angle_rad)
    perp_x    = -sin_a
    perp_y    =  cos_a
    tx = px + arm_len * cos_a
    ty = py + arm_len * sin_a

    sag  = r * 0.022
    cp1  = QPointF(px + arm_len * 0.28 * cos_a + perp_x * sag,
                   py + arm_len * 0.28 * sin_a + perp_y * sag)
    cp2  = QPointF(px + arm_len * 0.70 * cos_a - perp_x * sag * 0.6,
                   py + arm_len * 0.70 * sin_a - perp_y * sag * 0.6)

    arm_path = QPainterPath()
    arm_path.moveTo(QPointF(px, py))
    arm_path.cubicTo(cp1, cp2, QPointF(tx, ty))

    shadow_path = QPainterPath()
    shadow_path.moveTo(QPointF(px + 1.5, py + 1.5))
    shadow_path.cubicTo(
        QPointF(cp1.x() + 1.5, cp1.y() + 1.5),
        QPointF(cp2.x() + 1.5, cp2.y() + 1.5),
        QPointF(tx  + 1.5, ty  + 1.5),
    )

    # Shadow
    p.setOpacity(arm_alpha * 0.22)
    p.setPen(QPen(QColor(0, 0, 0, 180), 4.5,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(shadow_path)

    # Main arm body
    p.setOpacity(arm_alpha)
    p.setPen(QPen(QColor(205, 200, 190, 230), 2.4,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(arm_path)

    # Specular highlight
    p.setOpacity(arm_alpha * 0.45)
    hi_path = QPainterPath()
    hi_path.moveTo(QPointF(px, py))
    hi_path.cubicTo(
        QPointF(cp1.x() - perp_x * 0.6, cp1.y() - perp_y * 0.6),
        QPointF(cp2.x() - perp_x * 0.6, cp2.y() - perp_y * 0.6),
        QPointF(tx, ty),
    )
    p.setPen(QPen(QColor(240, 238, 232, 200), 0.9,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPath(hi_path)

    p.setOpacity(arm_alpha)

    # Counterweight
    cw_dist = arm_len * 0.17
    cwx = px - cw_dist * cos_a
    cwy = py - cw_dist * sin_a
    p.save()
    p.translate(cwx, cwy)
    p.rotate(arm_angle)
    p.setBrush(QColor(112, 108, 102, 215))
    p.setPen(QPen(QColor(72, 68, 64, 200), 0.7))
    p.drawEllipse(QRectF(-4.5, -3.0, 9.0, 6.0))
    p.restore()

    # Cartridge / stylus
    p.save()
    p.translate(tx, ty)
    p.rotate(arm_angle + 90.0)
    p.setBrush(QColor(62, 58, 55, 220))
    p.setPen(QPen(QColor(95, 90, 84, 190), 0.7))
    p.drawRoundedRect(QRectF(-2.2, -1.6, 4.4, 3.8), 0.7, 0.7)
    p.setBrush(QColor(220, 210, 180, 205))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(0.0, 2.2), 1.0, 1.0)
    p.restore()

    # Pivot
    p.setBrush(QColor(128, 123, 115, 220))
    p.setPen(QPen(QColor(72, 68, 62, 210), 1.0))
    p.drawEllipse(QPointF(px, py), 5.5, 5.5)

    p.setBrush(QColor(60, 57, 52, 230))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(px, py), 2.2, 2.2)

    p.setBrush(QColor(230, 226, 218, 190))
    p.drawEllipse(QPointF(px - 1.4, py - 1.4), 1.3, 1.3)

    p.setOpacity(1.0)
    p.restore()
