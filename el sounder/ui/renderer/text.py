"""
ui/renderer/text.py  –  el sounder
=====================================
Draws text overlays:
  - Intro splash text
  - Song title + artist (with marquee scrolling)
  - Error message
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui  import QColor, QFontMetrics, QPainter, QPen

from config.settings     import UI_FONT_FAMILIES
from config.translations import tr


def draw_intro_text(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    text_alpha: int,
    language: str,
) -> None:
    """Render the intro splash text inside the circle."""
    if text_alpha <= 0:
        return
    font = p.font()
    font.setFamilies(UI_FONT_FAMILIES)
    font.setPointSize(11)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QPen(QColor(255, 255, 255, text_alpha), 1))
    p.drawText(
        int(cx - r + 10), int(cy - r), int(r * 2 - 20), int(r * 2),
        Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
        tr(language, "intro_text"),
    )


def draw_error(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    error_msg: str,
) -> None:
    """Render an error message inside the circle."""
    if not error_msg:
        return
    p.setPen(QPen(QColor(255, 80, 80, 220), 1))
    font = p.font()
    font.setFamilies(UI_FONT_FAMILIES)
    font.setPointSize(11)
    font.setBold(True)
    p.setFont(font)
    p.drawText(
        int(cx - r), int(cy - r), int(r * 2), int(r * 2),
        Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
        error_msg,
    )


def draw_song_name(
    p: QPainter,
    cx: float,
    cy: float,
    r: float,
    s,                  # VisualState
    song_name_size: int,
) -> None:
    """
    Render title + artist text below the circle with marquee scrolling.

    Args:
        s:              VisualState (read-only).
        song_name_size: Font size in points (from settings).
    """
    if not (s.disp_title or s.disp_artist or s.disp_is_ad):
        return
    if s.song_name_alpha <= 0:
        return

    # ── Ad mode: only show a single "Werbung" label ──────────────
    if s.disp_is_ad:
        fs   = song_name_size
        tw   = min(int(r * 1.8), max(180, fs * 20))
        th   = int(fs * 2.6)
        tx   = int(cx - tw // 2)
        ty   = int(cy - th - 2)

        eff_sa = (s.song_alpha / 255.0) * (s.song_name_alpha / 255.0)
        if s.rendered_cover_style == "flat" and s.disp_cover_dark:
            t_col = QColor(18, 18, 18, int(215 * eff_sa))
        else:
            t_col = QColor(255, 255, 255, int(210 * eff_sa))

        font = p.font()
        font.setFamilies(UI_FONT_FAMILIES)
        font.setPointSize(fs)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(t_col, 1))
        p.drawText(tx, ty, tw, th, Qt.AlignmentFlag.AlignCenter, "Werbung")
        return

    fs   = song_name_size
    fs_a = max(6, fs - 1)
    tw   = min(int(r * 1.8), max(180, fs * 20))
    th   = int(fs * 2.6)
    ah   = int(fs_a * 2.6)
    tx   = int(cx - tw // 2)
    ty   = int(cy - th - 2)
    ay   = int(cy + 2)

    sa_f   = s.song_alpha / 255.0
    eff_sa = sa_f * s.song_name_alpha / 255.0

    if s.rendered_cover_style == "flat" and s.disp_cover_dark:
        t_col = QColor(18,  18,  18,  int(215 * eff_sa))
        a_col = QColor(40,  40,  40,  int(160 * eff_sa))
    else:
        t_col = QColor(255, 255, 255, int(210 * eff_sa))
        a_col = QColor(200, 200, 200, int(150 * eff_sa))

    # --- Title ---
    font = p.font()
    font.setFamilies(UI_FONT_FAMILIES)
    font.setPointSize(fs)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QPen(t_col, 1))

    fm_t     = QFontMetrics(font)
    t_px     = fm_t.horizontalAdvance(s.disp_title)
    overflow = t_px - tw

    if overflow > 0:
        if s.mq_pause > 0:
            pass  # AnimationController handles mq_pause decrement
        p.save()
        p.setClipRect(tx, ty, tw, th)
        p.drawText(
            int(tx - s.mq_offset), ty, t_px + 8, th,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            s.disp_title,
        )
        p.restore()
    else:
        p.drawText(tx, ty, tw, th, Qt.AlignmentFlag.AlignCenter, s.disp_title)

    # --- Artist ---
    font.setPointSize(fs_a)
    font.setBold(False)
    p.setFont(font)
    p.setPen(QPen(a_col, 1))

    fm_a       = QFontMetrics(font)
    a_px       = fm_a.horizontalAdvance(s.disp_artist)
    overflow_a = a_px - tw

    if overflow_a > 0:
        off_a = min(s.mq_offset, float(overflow_a))
        p.save()
        p.setClipRect(tx, ay, tw, ah)
        p.drawText(
            int(tx - off_a), ay, a_px + 8, ah,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            s.disp_artist,
        )
        p.restore()
    else:
        p.drawText(tx, ay, tw, ah, Qt.AlignmentFlag.AlignCenter, s.disp_artist)