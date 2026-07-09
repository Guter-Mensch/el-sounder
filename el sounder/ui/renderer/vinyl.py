"""
ui/renderer/vinyl.py  –  el sounder
======================================
Draws the vinyl record (rotating disc + label art + groove rings).
"""

from __future__ import annotations

from PyQt6.QtCore    import Qt, QPointF
from PyQt6.QtGui     import QColor, QPainter, QPainterPath, QPen, QPixmap

from config.settings import UI_FONT_FAMILIES


def draw_vinyl(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    vinyl_angle: float,
    cover_alpha: float,
    cover_blend: float,
    cover_blending: bool,
    cover_old_pixmap,
    song_has_pending: bool,
    next_pixmap,
    disp_pixmap,
    disp_title: str,
    disp_artist: str,
    show_song_name: bool,
) -> None:
    """Render the vinyl disc centred at (cx, cy) with radius r."""

    p.save()
    p.translate(cx, cy)
    p.rotate(vinyl_angle)
    p.setOpacity(cover_alpha / 255.0)

    # Vinyl disc body
    p.setBrush(QColor(20, 16, 16, 248))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(-r, -r, r * 2, r * 2)

    # Groove rings
    p.setBrush(Qt.BrushStyle.NoBrush)
    for gr in range(int(r * 0.44), r - 1, 3):
        shade = 42 + int((gr % 9) * 2)
        p.setPen(QPen(QColor(shade, shade - 5, shade - 5, 110), 0.8))
        p.drawEllipse(-gr, -gr, gr * 2, gr * 2)

    # Label art
    label_r = int(r * 0.40)
    clip    = QPainterPath()
    clip.addEllipse(QPointF(0.0, 0.0), float(label_r), float(label_r))

    def _draw_label(pixmap, opacity: float) -> None:
        if pixmap is None or opacity <= 0:
            return
        scaled = pixmap.scaled(
            label_r * 2, label_r * 2,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        ox = (scaled.width()  - label_r * 2) // 2
        oy = (scaled.height() - label_r * 2) // 2
        cropped = scaled.copy(ox, oy, label_r * 2, label_r * 2)
        p.save()
        p.setClipPath(clip)
        p.setOpacity(opacity)
        p.drawPixmap(-label_r, -label_r, cropped)
        p.restore()

    # NOTE: the "incoming" pixmap must be selected purely from
    # `song_has_pending` (the text/track-swap flag), NOT from
    # `cover_blending`. The two fades (song_alpha vs. cover_blend) run at
    # slightly different speeds, so `cover_blending` can flip to False one
    # frame *before* `disp_pixmap` is actually swapped over in state.py.
    # Gating on it here caused a 1-frame flash back to the old cover at
    # full opacity right at the end of every transition (the "hard cut").
    have_old = cover_old_pixmap is not None and cover_blend < 1.0
    inc_pm   = next_pixmap if song_has_pending else disp_pixmap
    have_new = inc_pm is not None

    if have_old or have_new:
        if have_old:
            _draw_label(cover_old_pixmap, 1.0 - cover_blend)
        if have_new:
            _draw_label(inc_pm, cover_blend if have_old else 1.0)
        p.setOpacity(cover_alpha / 255.0)
    else:
        p.setBrush(QColor(150, 28, 28, 235))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(-label_r, -label_r, label_r * 2, label_r * 2)

    # Tiny label text (when show_song_name is on)
    if show_song_name and (disp_title or disp_artist):
        def _tv(s: str, n: int = 11) -> str:
            return s[:n] + "…" if len(s) > n else s

        lw = int(label_r * 1.75)
        lx = -lw // 2
        font = p.font()
        font.setFamilies(UI_FONT_FAMILIES)
        font.setPointSize(6)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(QColor(240, 240, 240, 220), 1))
        p.drawText(lx, int(label_r * 0.28), lw, 12,
                   Qt.AlignmentFlag.AlignCenter, _tv(disp_title))

        font.setPointSize(5)
        font.setBold(False)
        p.setFont(font)
        p.setPen(QPen(QColor(200, 200, 200, 170), 1))
        p.drawText(lx, int(label_r * 0.55), lw, 10,
                   Qt.AlignmentFlag.AlignCenter, _tv(disp_artist))

    # Centre hole
    hole_r = 7
    p.setBrush(QColor(5, 4, 4, 255))
    p.setPen(QPen(QColor(70, 60, 60, 180), 1))
    p.drawEllipse(-hole_r, -hole_r, hole_r * 2, hole_r * 2)

    p.setOpacity(1.0)
    p.restore()