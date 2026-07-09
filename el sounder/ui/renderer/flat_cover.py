"""
ui/renderer/flat_cover.py  –  el sounder
===========================================
Draws the flat semi-transparent album art behind the bars.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui  import QPainter, QPainterPath


def draw_flat_cover(
    p: QPainter,
    cx: float,
    cy: float,
    r: int,
    cover_alpha: float,
    cover_blend: float,
    cover_blending: bool,
    cover_old_pixmap,
    song_has_pending: bool,
    next_pixmap,
    disp_pixmap,
) -> None:
    """
    Draw album art clipped to the circle with cross-blend support.
    """
    side = r * 2
    clip = QPainterPath()
    clip.addEllipse(cx - r, cy - r, float(side), float(side))

    def _draw_flat(pixmap, opacity: float) -> None:
        if pixmap is None or opacity <= 0:
            return
        scaled = pixmap.scaled(
            side, side,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        ox = (scaled.width()  - side) // 2
        oy = (scaled.height() - side) // 2
        cropped = scaled.copy(ox, oy, side, side)
        p.save()
        p.setClipPath(clip)
        p.setOpacity(opacity)
        p.drawPixmap(int(cx - r), int(cy - r), cropped)
        p.restore()

    base_opacity = 0.32 * cover_alpha / 255.0

    # Same fix as in vinyl.py: gate on cover_blend/cover_old_pixmap directly
    # instead of the cover_blending flag, so a 1-frame desync between the
    # image-blend timer and the track-swap timer can't cause a visible pop
    # back to the un-blended old/new cover right at the end of the fade.
    if cover_old_pixmap is not None and cover_blend < 1.0:
        incoming = next_pixmap if song_has_pending else disp_pixmap
        _draw_flat(cover_old_pixmap, base_opacity * (1.0 - cover_blend))
        _draw_flat(incoming,         base_opacity * cover_blend)
    elif disp_pixmap:
        _draw_flat(disp_pixmap, base_opacity)

    p.setOpacity(1.0)