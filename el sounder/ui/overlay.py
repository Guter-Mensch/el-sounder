"""
ui/overlay.py  –  el sounder

The window itself. Owns settings, tray, timers and Qt events; hands the
actual per-frame math to AnimationController and the pixels to the
renderer modules. If it's on screen, it goes through here.
"""

from __future__ import annotations

import math
import time

import numpy as np

from PyQt6.QtCore    import Qt, QEvent, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui     import QColor, QFont, QFontMetrics, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

from core.engine     import CoreEngine
from core.audio      import AudioCapture
from config.settings import (
    UI_FONT_FAMILIES,
    SettingsController, load_settings, save_settings, PersistentMenu,
)
from config.translations import tr
from backstage.windows    import load_app_icon, force_taskbar_icon
from backstage.tray       import build_tray

from ui.state import VisualState, AnimationController
from ui.renderer.background  import draw_background
from ui.renderer.flat_cover  import draw_flat_cover
from ui.renderer.vinyl       import draw_vinyl
from ui.renderer.arm         import draw_arm
from ui.renderer.bars        import draw_bars
from ui.renderer.text        import draw_intro_text, draw_error, draw_song_name


# Circle radius (in pixels)
_RADIUS = 120


class Overlay(QWidget):
    # Signal used to safely marshal track updates from the SMTC thread
    # to the Qt main thread (QTimer.singleShot is NOT thread-safe in PyQt6).
    _track_signal = pyqtSignal(
        str,    # title
        str,    # artist
        object, # img_bytes (bytes | None)
        int,    # new_cover_hash
        object, # qimg (QImage | None)
        bool,   # cover_dark
        bool,   # title_changed
        bool,   # cover_changed
        float,  # pos_s
        float,  # dur_s
        bool,   # is_playing
    )

    # Marshals CoreEngine's auto audio-source switch (fired from a
    # background polling thread) onto the Qt main thread.
    _auto_source_signal = pyqtSignal(str)

    def __init__(
        self,
        core: CoreEngine,
        capture: AudioCapture | None = None,
        error_msg: str | None = None,
    ):
        super().__init__()
        self._core      = core
        self._capture   = capture
        self._error_msg = error_msg

        # ── Settings ─────────────────────────────────────────────
        cfg       = load_settings()
        self._sc  = SettingsController(cfg)
        self._wire_settings_callbacks()

        # ── Visual State + Animation Controller ──────────────────
        self._state = VisualState(num_bars=self._sc.num_bars)
        self._state.opacity_mode        = self._sc.opacity_mode
        self._state.show_song_name      = self._sc.show_song_name
        self._state.rendered_cover_style = self._sc.cover_style
        self._state.next_cover_style    = self._sc.cover_style
        self._state.rendered_bar_style  = self._sc.bar_style
        self._state.next_bar_style      = self._sc.bar_style
        self._state.rendered_color_mode = self._sc.bar_color_mode
        self._state.rendered_color_hex  = self._sc.bar_color_hex
        self._state.rendered_grad_from  = self._sc.bar_gradient_from
        self._state.rendered_grad_to    = self._sc.bar_gradient_to
        self._state.rendered_grad_dir   = self._sc.bar_gradient_dir
        self._state.song_name_alpha     = 255.0 if self._sc.show_song_name else 0.0

        self._anim = AnimationController(self._state)

        # ── Drag state ───────────────────────────────────────────
        self._dragging = False
        self._drag_pos = QPoint()

        # ── Window flags + geometry ──────────────────────────────
        icon = load_app_icon()
        self.setWindowIcon(icon)
        self.setWindowTitle("el sounder")
        self._suppress_pos  = False
        self._closing       = False
        self._save_pending  = False
        self._apply_window_flags()
        self.setWindowOpacity(0.0)
        self.resize(700, 700)
        self.move(cfg["window_x"], cfg["window_y"])

        # ── System tray ──────────────────────────────────────────
        self._tray = build_tray(
            icon=icon,
            parent=self,
            on_show_hide=self._tray_show_hide,
            on_quit=self._start_outro,
            language=self._sc.language,
        )
        self._tray.show()

        # Retry force_taskbar_icon several times at startup.
        # On first launch the native HWND may not be ready yet, so one call
        # is often not enough — this mimics the manual refresh previously
        # needed via the "Always on Top" settings toggle.
        for _delay_ms in (200, 600, 1200, 2500, 4500):
            QTimer.singleShot(_delay_ms, lambda: force_taskbar_icon(self))

        # ── SMTC callback ────────────────────────────────────────
        self._core.on_track_update(self._on_track_update)
        self._track_signal.connect(self._apply_cover)

        # ── Auto audio-source switching (engine -> UI sync) ────────
        self._auto_source_signal.connect(self._on_engine_audio_source_auto_switch)
        self._core.on_audio_source_auto_switch = self._auto_source_signal.emit

        # ── Timers ───────────────────────────────────────────────
        self._render_timer = QTimer()
        self._render_timer.timeout.connect(self._tick)
        self._render_timer.start(16)                    # ~60 fps

        self._save_timer = QTimer()
        self._save_timer.timeout.connect(self._schedule_save)
        self._save_timer.start(10_000)                  # fallback every 10 s

        self._discord_ka_timer = QTimer(self)
        self._discord_ka_timer.timeout.connect(self._core.discord_keepalive)
        self._discord_ka_timer.start(45_000)

    # ── Settings callbacks ───────────────────────────────────────

    def _wire_settings_callbacks(self) -> None:
        sc = self._sc
        sc.on_save_needed        = self._schedule_save
        sc.on_opacity_change     = self._on_opacity_change
        sc.on_bar_style_change   = self._on_bar_style_change
        sc.on_cover_style_change = self._on_cover_style_change
        sc.on_bar_color_change   = self._on_bar_color_change
        sc.on_num_bars_change    = self._on_num_bars_change
        sc.on_sensitivity_change = self._on_sensitivity_change
        sc.on_audio_source_change = self._on_audio_source_change
        sc.on_audio_source_auto_change = self._on_audio_source_auto_change
        sc.on_always_on_top      = self._on_always_on_top
        sc.on_always_on_bottom   = self._on_always_on_bottom
        sc.on_show_song_name     = self._on_show_song_name
        sc.on_song_name_size     = lambda _s: self.update()
        sc.on_language_change    = lambda _l: None
        sc.on_discord_enabled    = self._on_discord_enabled
        sc.on_discord_options    = self._on_discord_options
        sc.on_quit               = self._start_outro
        sc.on_reset              = self._on_reset

    def _on_opacity_change(self, mode: str) -> None:
        self._state.opacity_mode = mode
        self.update()

    def _on_bar_style_change(self, style: str) -> None:
        s = self._state
        if style == s.rendered_bar_style and not s.bars_changing:
            return
        # Same fade-out -> swap -> fade-in handled by AnimationController.tick()
        # (mirrors _on_cover_style_change). Previously this set
        # rendered_bar_style immediately, which popped the old style away
        # instantly instead of fading it out first.
        s.next_bar_style = style
        s.bars_changing  = True

    def _on_cover_style_change(self, style: str) -> None:
        s = self._state
        s.next_cover_style = style
        s.cover_changing   = True

    def _on_bar_color_change(self, mode, hex_col, grad_from, grad_to, grad_dir) -> None:
        s = self._state
        # Save "before" snapshot
        s.prev_color_mode = s.rendered_color_mode
        s.prev_color_hex  = s.rendered_color_hex
        s.prev_grad_from  = s.rendered_grad_from
        s.prev_grad_to    = s.rendered_grad_to
        s.prev_grad_dir   = s.rendered_grad_dir
        s.color_blend     = 0.0
        s.color_blending  = True
        # Apply new values
        s.rendered_color_mode = mode
        s.rendered_color_hex  = hex_col
        s.rendered_grad_from  = grad_from
        s.rendered_grad_to    = grad_to
        s.rendered_grad_dir   = grad_dir

    def _on_num_bars_change(self, n: int) -> None:
        self._state.bands = np.zeros(n)
        self._core.set_num_bars(n)

    def _on_sensitivity_change(self, key: str) -> None:
        self._core.set_sensitivity(key)

    def _on_audio_source_change(self, key: str) -> None:
        self._core.set_audio_source(key)
        # Switching sources makes the levels jump instantly, which looks
        # like a glitch. Reuse the bar-style fade (out -> swap -> in) as a
        # quick dip-to-zero-and-back to mask it, even though the style
        # itself isn't actually changing.
        s = self._state
        s.next_bar_style = s.rendered_bar_style
        s.bars_changing  = True

    def _on_audio_source_auto_change(self, checked: bool) -> None:
        self._core.set_audio_source_auto(checked)

    def _on_engine_audio_source_auto_switch(self, mode: str) -> None:
        # CoreEngine switched spotify<->all on its own (Spotify.exe opened/
        # closed). Keep the settings/menu state and the visual fade in sync,
        # without re-triggering set_audio_source() a second time.
        self._sc.audio_source = mode
        if self._sc.on_save_needed:
            self._sc.on_save_needed()
        s = self._state
        s.next_bar_style = s.rendered_bar_style
        s.bars_changing  = True

    def _on_always_on_top(self, checked: bool) -> None:
        pos = self.pos()
        self._apply_window_flags()
        self.move(pos)

    def _on_always_on_bottom(self, checked: bool) -> None:
        pos = self.pos()
        self._apply_window_flags()
        self.move(pos)

    def _on_show_song_name(self, checked: bool) -> None:
        s = self._state
        if checked:
            s.song_name_hiding = False
            s.song_name_alpha  = 0.0
        else:
            if not s.song_name_hiding:
                s.song_name_hiding = True

    def _on_discord_enabled(self, checked: bool) -> None:
        if checked:
            sc = self._sc
            # Push the saved show_title/artist/cover settings into the engine
            # before connecting — otherwise toggling Discord on mid-session
            # ignores them and falls back to the hardcoded "all on" defaults.
            self._core.set_discord_options(
                show_title  = sc.discord_show_title,
                show_artist = sc.discord_show_artist,
                show_cover  = sc.discord_show_cover,
                show_source = sc.discord_show_source,
            )
        self._core.set_discord_enabled(checked)

    def _on_discord_options(
        self, show_title: bool, show_artist: bool, show_cover: bool, show_source: bool,
    ) -> None:
        self._core.set_discord_options(
            show_title=show_title,
            show_artist=show_artist,
            show_cover=show_cover,
            show_source=show_source,
        )

    def _on_reset(self, defaults: dict) -> None:
        s = self._state
        s.opacity_mode = defaults["opacity_mode"]
        s.bands        = np.zeros(defaults["num_bars"])
        self._core.set_num_bars(defaults["num_bars"])
        self._core.set_sensitivity(defaults["sensitivity"])
        self._core.set_audio_source(defaults.get("audio_source", "spotify"))
        self._core.set_audio_source_auto(defaults.get("audio_source_auto", False))

        # Color blend reset
        s.prev_color_mode = s.rendered_color_mode
        s.prev_color_hex  = s.rendered_color_hex
        s.prev_grad_from  = s.rendered_grad_from
        s.prev_grad_to    = s.rendered_grad_to
        s.prev_grad_dir   = s.rendered_grad_dir
        s.color_blend     = 0.0
        s.color_blending  = True
        s.rendered_color_mode = defaults["bar_color_mode"]
        s.rendered_color_hex  = defaults["bar_color_hex"]
        s.rendered_grad_from  = defaults["bar_gradient_from"]
        s.rendered_grad_to    = defaults["bar_gradient_to"]
        s.rendered_grad_dir   = defaults["bar_gradient_dir"]

        s.next_bar_style = defaults["bar_style"]
        s.bars_changing  = (defaults["bar_style"] != s.rendered_bar_style)

        s.next_cover_style = defaults["cover_style"]
        s.cover_changing   = True

        s.song_name_hiding = False
        s.song_name_alpha  = 255.0 if defaults["show_song_name"] else 0.0
        s.show_song_name   = defaults["show_song_name"]

        pos = self.pos()
        self._apply_window_flags()
        self.move(pos)
        self.move(defaults["window_x"], defaults["window_y"])
        self.update()

    # ── SMTC track update (called from SMTC thread) ──────────────

    def _on_track_update(
        self,
        title: str,
        artist: str,
        img_bytes: bytes | None,
        pos_s: float,
        dur_s: float,
        is_playing: bool,
        title_changed: bool,
        cover_changed: bool,
    ) -> None:
        """Runs in the SMTC thread — builds QImage here (thread-safe),
        then schedules _apply_cover on the Qt main thread via QTimer.singleShot
        so that QPixmap.fromImage() always executes on the GUI thread."""
        s = self._state
        new_cover_hash = hash(img_bytes) if img_bytes else 0

        # QImage construction is thread-safe; compute brightness here too.
        qimg:       QImage | None = None
        cover_dark: bool          = False
        if img_bytes:
            qimg = QImage()
            qimg.loadFromData(img_bytes)
            if not qimg.isNull():
                w, h       = qimg.width(), qimg.height()
                cx_i, cy_i = w // 2, h // 2
                total, count = 0.0, 0
                for dy in range(-28, 28, 5):
                    for dx in range(-72, 72, 8):
                        x = max(0, min(w - 1, cx_i + dx))
                        y = max(0, min(h - 1, cy_i + dy))
                        c = QColor(qimg.pixel(x, y))
                        total += c.red() * 0.299 + c.green() * 0.587 + c.blue() * 0.114
                        count += 1
                cover_dark = (total / count > 145) if count else False
            else:
                # Qt couldn't decode the image bytes (e.g. Spotify WDP stream).
                # Discard them so they don't get cached as a valid cover hash
                # and block future retries.
                qimg        = None
                img_bytes   = None

        # Snapshot plain-Python values that the main-thread slot will need.
        _title         = title or ""
        _artist        = artist or ""
        _img_bytes     = img_bytes
        _new_cover_hash = hash(img_bytes) if img_bytes else 0
        _title_changed  = title_changed
        _cover_changed  = cover_changed
        _pos_s          = pos_s
        _dur_s          = dur_s
        _is_playing     = is_playing

        # Emit signal → marshalled safely to Qt main thread via queued connection.
        # QTimer.singleShot() must NOT be called from a non-Qt thread (PyQt6).
        self._track_signal.emit(
            _title, _artist, _img_bytes, _new_cover_hash,
            qimg, cover_dark,
            _title_changed, _cover_changed,
            _pos_s, _dur_s, _is_playing,
        )

    def _apply_cover(
        self,
        title: str,
        artist: str,
        img_bytes: bytes | None,
        new_cover_hash: int,
        qimg: "QImage | None",
        cover_dark: bool,
        title_changed: bool,
        cover_changed: bool,
        pos_s: float,
        dur_s: float,
        is_playing: bool,
    ) -> None:
        """Runs on the Qt main thread — safe to call QPixmap.fromImage()."""
        s = self._state

        new_pm: QPixmap | None = None
        if qimg is not None and not qimg.isNull():
            new_pm = QPixmap.fromImage(qimg)

        if title_changed:
            # Spotify ads have no title/artist but still deliver a cover image.
            # Detect this and suppress title/artist display.
            is_ad = bool(img_bytes) and not title and not artist
            s.next_title      = title
            s.next_artist     = artist
            s.next_pixmap     = new_pm
            s.next_cover_dark = cover_dark
            s.next_img_bytes  = img_bytes
            s.next_is_ad      = is_ad
            s.last_cover_hash = new_cover_hash
            s.song_has_pending    = True
            s.arm_lift_requested  = True
            s.cover_old_pixmap    = s.disp_pixmap
            s.cover_old_dark      = s.disp_cover_dark
            s.cover_blend         = 0.0
            s.cover_blending      = True

        elif cover_changed and new_pm is not None:
            s.last_cover_hash  = new_cover_hash
            s.next_img_bytes   = img_bytes
            s.disp_img_bytes   = img_bytes
            s.cover_old_pixmap = s.disp_pixmap
            s.cover_old_dark   = s.disp_cover_dark
            s.disp_pixmap      = new_pm
            s.disp_cover_dark  = cover_dark
            s.cover_blend      = 0.0
            s.cover_blending   = True

        s.smtc_known_pos  = pos_s
        s.smtc_known_time = time.time()
        s.smtc_is_playing = is_playing
        s.song_dur_s      = dur_s
        s.last_is_playing = is_playing

    # ── Tick (Qt render timer, ~60 fps) ──────────────────────────

    def _tick(self) -> None:
        bands, db_pct = self._core.get_bands_db()
        should_quit, win_opacity = self._anim.tick(bands, db_pct, self._sc)

        # Marquee ticker (kept here because it needs VisualState + settings together)
        s = self._state
        if s.phase == "running" and (s.disp_title or s.disp_artist):
            fs     = self._sc.song_name_size
            tw     = min(int(_RADIUS * 1.8), max(180, fs * 20))
            font = QFont()
            font.setFamilies(UI_FONT_FAMILIES)
            font.setPointSize(fs)
            font.setBold(True)
            fm   = QFontMetrics(font)
            t_px = fm.horizontalAdvance(s.disp_title)
            overflow = t_px - tw
            if s.disp_title != s.mq_prev_title:
                s.mq_offset     = 0.0
                s.mq_dir        = 1
                s.mq_pause      = 60
                s.mq_prev_title = s.disp_title
            if overflow > 0:
                if s.mq_pause > 0:
                    s.mq_pause -= 1
                else:
                    s.mq_offset += 0.8 * s.mq_dir
                    if s.mq_offset >= overflow:
                        s.mq_offset = float(overflow)
                        s.mq_dir    = -1
                        s.mq_pause  = 60
                    elif s.mq_offset <= 0.0:
                        s.mq_offset = 0.0
                        s.mq_dir    = 1
                        s.mq_pause  = 60

        # Also handle save-needed from song_name_hiding completion
        if s.song_name_hiding is False and not self._sc.show_song_name:
            self._schedule_save()

        if should_quit:
            self._do_quit()
            return

        # Update window opacity during intro/outro
        if s.phase in ("intro_in", "outro"):
            self.setWindowOpacity(win_opacity)

        self.update()

    # ── Window flags ─────────────────────────────────────────────

    def _apply_window_flags(self) -> None:
        self._suppress_pos = True
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
        if self._sc.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        elif self._sc.always_on_bottom:
            flags |= Qt.WindowType.WindowStaysOnBottomHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowIcon(load_app_icon())
        self.show()
        QTimer.singleShot(200, lambda: setattr(self, "_suppress_pos", False))

    # ── Save helpers ─────────────────────────────────────────────

    def _schedule_save(self) -> None:
        if not self._save_pending:
            self._save_pending = True
            QTimer.singleShot(800, self._save_now)

    def _save_now(self) -> None:
        if self._suppress_pos:
            return
        pos = self.pos()
        self._save_pending = False
        save_settings(self._sc.as_dict(pos))

    # ── Qt events ────────────────────────────────────────────────

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            cx = self.width()  / 2
            cy = self.height() / 2
            if math.hypot(e.position().x() - cx, e.position().y() - cy) < _RADIUS + 20:
                self._dragging = True
                self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._dragging:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e) -> None:
        if self._dragging:
            self._dragging = False
            self._schedule_save()
        else:
            self._dragging = False

    def contextMenuEvent(self, e) -> None:
        self._sc.show_context_menu(e.globalPos(), self)

    def changeEvent(self, e) -> None:
        if (e.type() == QEvent.Type.WindowStateChange
                and bool(self.windowState() & Qt.WindowState.WindowMinimized)):
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
            e.ignore()
            return
        super().changeEvent(e)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        icon = load_app_icon()
        self.setWindowIcon(icon)
        app = QApplication.instance()
        if app:
            app.setWindowIcon(icon)
        if hasattr(self, "_tray"):
            self._tray.setIcon(icon)
        force_taskbar_icon(self)

    def closeEvent(self, e) -> None:
        if self._closing:
            self._save_now()
            super().closeEvent(e)
        else:
            e.ignore()
            self.hide()

    # ── Tray helpers ─────────────────────────────────────────────

    def _tray_show_hide(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    # ── Outro / quit ─────────────────────────────────────────────

    def _start_outro(self) -> None:
        self.show()
        s = self._state
        if s.phase != "outro":
            s.phase       = "outro"
            s.phase_frame = 0

    def _do_quit(self) -> None:
        self._closing = True
        self._save_now()
        self._core.shutdown()
        self._tray.hide()
        QApplication.instance().quit()

    # ── Paint ────────────────────────────────────────────────────

    def paintEvent(self, _e) -> None:                          # noqa: N802
        s  = self._state
        p  = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = self.width()  / 2
        cy = self.height() / 2
        r  = _RADIUS

        # 1. Background circle
        draw_background(p, cx, cy, r, s.opacity_mode)

        # 2. Flat cover art (behind bars)
        if s.rendered_cover_style == "flat" and s.phase == "running":
            draw_flat_cover(
                p, cx, cy, r,
                s.cover_alpha, s.cover_blend, s.cover_blending,
                s.cover_old_pixmap, s.song_has_pending, s.next_pixmap, s.disp_pixmap,
            )

        # 3. Vinyl disc
        if s.rendered_cover_style == "vinyl" and s.phase == "running":
            draw_vinyl(
                p, cx, cy, r,
                s.vinyl_angle, s.cover_alpha,
                s.cover_blend, s.cover_blending,
                s.cover_old_pixmap, s.song_has_pending, s.next_pixmap, s.disp_pixmap,
                s.disp_title, s.disp_artist, s.show_song_name,
            )

        # 4. Tone arm
        if s.phase == "running":
            draw_arm(p, cx, cy, r, s.arm_alpha, s.arm_angle)

        # 5. Intro text
        if s.text_alpha > 0:
            draw_intro_text(p, cx, cy, r, s.text_alpha, self._sc.language)

        # 6. Song name
        if (s.phase == "running"
                and s.rendered_cover_style != "vinyl"
                and (s.show_song_name or s.song_name_hiding)
                and s.song_name_alpha > 0
                and (s.disp_title or s.disp_artist)):
            draw_song_name(p, cx, cy, r, s, self._sc.song_name_size)

        # 7. Error overlay (short-circuits bar rendering)
        if self._error_msg:
            draw_error(p, cx, cy, r, self._error_msg)
            return

        # 8. Frequency bars
        draw_bars(p, cx, cy, r, s)