"""
ui/state.py  –  el sounder
=============================
Centralized animation and display state.

VisualState       – a plain data container for everything the renderers need
AnimationController – owns all frame-to-frame mutation logic (tick())

Keeping state separate from QWidget and from rendering means:
  - paintEvent never mutates state
  - _tick() never draws
  - renderers only read from VisualState
"""

from __future__ import annotations

import time

import numpy as np


# ──────────────────────────────────────────────────────────────────
# VisualState – the single source of truth for one rendered frame
# ──────────────────────────────────────────────────────────────────
class VisualState:
    """
    Plain data container.  All fields are written by AnimationController
    and read by renderer modules.  No logic lives here.
    """

    def __init__(self, num_bars: int = 90):
        # Audio
        self.bands:    np.ndarray = np.zeros(num_bars)
        self.db_pct:   float      = 0.0
        self.db_smooth: float     = 0.0           # EMA-smoothed dB for bar scaling

        # Window lifecycle
        self.phase:       str = "intro_in"    # intro_in | intro_hold | intro_text_out | running | outro
        self.phase_frame: int = 0
        self.text_alpha:  int = 255           # intro text opacity (0–255)
        self.closing:    bool = False

        # Song display (current / next for cross-fade)
        self.disp_title:      str       = ""
        self.disp_artist:     str       = ""
        self.disp_pixmap                = None   # QPixmap | None
        self.disp_cover_dark: bool      = False
        self.disp_img_bytes:  bytes | None = None

        self.next_title:      str       = ""
        self.next_artist:     str       = ""
        self.next_pixmap                = None
        self.next_cover_dark: bool      = False
        self.next_img_bytes:  bytes | None = None
        self.next_is_ad:      bool      = False   # True when Spotify plays an ad

        self.last_cover_hash: int       = -1
        self.last_is_playing: bool      = True
        self.song_has_pending: bool     = False
        self.song_alpha:       float    = 0.0     # 0–255
        self.disp_is_ad:      bool      = False   # True when current display is an ad

        # Cover cross-blend (old → new when track changes)
        self.cover_old_pixmap            = None
        self.cover_old_dark:  bool       = False
        self.cover_blend:     float      = 1.0
        self.cover_blending:  bool       = False

        # Cover style cross-fade
        self.rendered_cover_style: str   = "flat"
        self.next_cover_style:     str   = "flat"
        self.cover_alpha:          float = 255.0
        self.cover_changing:       bool  = False

        # Bar style cross-fade
        self.rendered_bar_style: str     = "bar"
        self.next_bar_style:     str     = "bar"
        self.bars_alpha:         float   = 255.0
        self.bars_changing:      bool    = False

        # Bar color cross-blend
        self.rendered_color_mode: str    = "rainbow"
        self.rendered_color_hex:  str    = "#89dceb"
        self.rendered_grad_from:  str    = "#89b4fa"
        self.rendered_grad_to:    str    = "#f38ba8"
        self.rendered_grad_dir:   str    = "freq"
        self.prev_color_mode:     str    = "rainbow"
        self.prev_color_hex:      str    = "#89dceb"
        self.prev_grad_from:      str    = "#89b4fa"
        self.prev_grad_to:        str    = "#f38ba8"
        self.prev_grad_dir:       str    = "freq"
        self.color_blend:         float  = 1.0
        self.color_blending:      bool   = False

        # Song name visibility + marquee
        self.song_name_alpha:  float = 255.0
        self.song_name_hiding: bool  = False
        self.mq_offset:        float = 0.0
        self.mq_dir:           int   = 1
        self.mq_pause:         int   = 60
        self.mq_prev_title:    str   = ""

        # Vinyl animation
        self.vinyl_angle: float = 0.0
        self.vinyl_speed: float = 0.0

        # Tone arm
        self.arm_alpha:         float = 0.0
        self.arm_angle:         float = 100.0
        self.arm_target:        float = 100.0
        self.arm_lifting:       bool  = False
        self.arm_lift_frames:   int   = 0
        self.arm_lift_requested: bool = False

        # Playback position (interpolated from SMTC)
        self.song_pos_s:      float = 0.0
        self.song_dur_s:      float = 0.0
        self.smtc_known_pos:  float = 0.0
        self.smtc_known_time: float = 0.0
        self.smtc_is_playing: bool  = False

        # Opacity mode (read by background renderer)
        self.opacity_mode: str = "milky"

        # Show-song-name flag (mirrored from settings)
        self.show_song_name: bool = True


# ──────────────────────────────────────────────────────────────────
# AnimationController – owns all frame-to-frame mutation
# ──────────────────────────────────────────────────────────────────
class AnimationController:
    """
    Called once per render tick (~60 fps).
    Reads from settings/core; writes to VisualState.
    Returns True if the application should quit (outro finished).
    """

    # Vinyl
    VINYL_SPEED_MAX = 1.5
    VINYL_ACCEL     = 0.06
    VINYL_DECEL     = 0.965
    VINYL_ALIGN_K   = 0.030

    # Arm
    ARM_LIFT_DEG  = 97.0
    ARM_OUTER_DEG = 105.0
    ARM_INNER_DEG = 132.0
    ARM_ALPHA_STEP = 0.035

    # Track transition (single timer drives cover crossfade + text fade,
    # see TRACK_FADE_STEP below — keep for backwards compat if referenced
    # elsewhere)
    SONG_FADE        = 18.0
    COVER_BLEND_STEP = 0.07
    VFADE            = 18.0
    COLOR_BLEND_STEP = 0.07

    # Single progress step for the unified A→B track transition.
    # 0.0 -> 1.0 over ~16 frames (~270ms @ 60fps). One timer, one source
    # of truth: the cover crossfade AND the text fade are both derived
    # from this single value, so they can never drift apart / desync.
    TRACK_FADE_STEP  = 1.0 / 16.0

    # Intro / outro frame counts
    FRAMES_IN       = 50
    FRAMES_HOLD     = 94
    FRAMES_TEXT_OUT = 37
    FRAMES_OUTRO    = 31

    def __init__(self, state: VisualState):
        self.s = state

    def tick(
        self,
        bands:         np.ndarray,
        db_pct:        float,
        settings,                   # SettingsController
    ) -> bool:
        """
        Advance all animation state by one frame.
        Returns True when the outro animation is complete (quit signal).
        """
        s = self.s

        # ── Audio ────────────────────────────────────────────────
        if len(bands) == len(s.bands):
            s.bands = bands
        s.db_smooth = s.db_smooth * 0.70 + db_pct * 0.30

        song_playing = bool(
            (s.disp_title or s.disp_artist) and s.smtc_is_playing
        )

        # ── Vinyl rotation ───────────────────────────────────────
        if s.phase == "running":
            if song_playing:
                s.vinyl_speed = min(s.vinyl_speed + self.VINYL_ACCEL, self.VINYL_SPEED_MAX)
            else:
                s.vinyl_speed *= self.VINYL_DECEL
                if s.vinyl_speed < 0.005:
                    s.vinyl_speed = 0.0
            s.vinyl_angle = (s.vinyl_angle + s.vinyl_speed) % 360.0
            if not song_playing and s.vinyl_speed < 0.5:
                delta = s.vinyl_angle
                if delta > 180.0:
                    delta -= 360.0
                s.vinyl_angle = (s.vinyl_angle - delta * self.VINYL_ALIGN_K) % 360.0

        # ── Tone arm ─────────────────────────────────────────────
        want_arm = 0.0
        if want_arm > s.arm_alpha:
            s.arm_alpha = min(1.0, s.arm_alpha + self.ARM_ALPHA_STEP)
        elif want_arm < s.arm_alpha:
            s.arm_alpha = max(0.0, s.arm_alpha - self.ARM_ALPHA_STEP)

        if s.arm_lift_requested:
            s.arm_lift_requested = False
            s.arm_lifting        = True
            s.arm_lift_frames    = 15

        if s.smtc_is_playing and s.smtc_known_time > 0:
            s.song_pos_s = s.smtc_known_pos + (time.time() - s.smtc_known_time)
        else:
            s.song_pos_s = s.smtc_known_pos

        if s.arm_lifting:
            s.arm_target      = self.ARM_LIFT_DEG
            s.arm_lift_frames -= 1
            if s.arm_lift_frames <= 0:
                s.arm_lifting = False
        elif not song_playing:
            s.arm_target = self.ARM_LIFT_DEG
        else:
            progress     = 0.0
            if s.song_dur_s > 0:
                progress = max(0.0, min(1.0, s.song_pos_s / s.song_dur_s))
            s.arm_target = self.ARM_OUTER_DEG + (self.ARM_INNER_DEG - self.ARM_OUTER_DEG) * progress
        s.arm_angle += (s.arm_target - s.arm_angle) * 0.06

        # ── Track transition (unified A→B fade) ──────────────────
        # ONE progress value (s.cover_blend, 0→1) drives both the cover
        # crossfade and the text fade. Old cover fades out while the new
        # one fades in *at the same time* (true crossfade, not fade-out-
        # then-fade-in), and the text swaps at the midpoint of the same
        # timer. Because there is only a single variable in play, the two
        # can never drift apart — that drift was the source of the
        # 1-frame flash/"glitch" at the end of the old two-timer version.
        if s.song_has_pending:
            s.cover_blending = True
            s.cover_blend = min(1.0, s.cover_blend + self.TRACK_FADE_STEP)

            # Text: fade out over the first half, swap, fade in over the
            # second half of the very same progress value.
            if s.cover_blend < 0.5:
                s.song_alpha = max(0.0, 255.0 * (1.0 - s.cover_blend / 0.5))
            else:
                if s.disp_title != s.next_title or s.disp_artist != s.next_artist:
                    s.disp_title  = s.next_title
                    s.disp_artist = s.next_artist
                s.song_alpha = min(255.0, 255.0 * ((s.cover_blend - 0.5) / 0.5))

            if s.cover_blend >= 1.0:
                s.disp_pixmap      = s.next_pixmap
                s.disp_cover_dark  = s.next_cover_dark
                s.disp_img_bytes   = s.next_img_bytes
                s.disp_is_ad       = s.next_is_ad
                s.disp_title       = s.next_title
                s.disp_artist      = s.next_artist
                s.song_alpha       = 255.0
                s.song_has_pending = False
                s.cover_blending   = False
                s.cover_old_pixmap = None

        elif s.cover_blending:
            # Cover-only update (same track, artwork refreshed) — no text
            # swap involved, just a plain single-timer image crossfade.
            s.cover_blend = min(1.0, s.cover_blend + self.TRACK_FADE_STEP)
            if s.cover_blend >= 1.0:
                s.cover_blending   = False
                s.cover_old_pixmap = None
            if s.song_alpha < 255:
                s.song_alpha = min(255.0, s.song_alpha + self.SONG_FADE)

        elif s.song_alpha < 255:
            s.song_alpha = min(255.0, s.song_alpha + self.SONG_FADE)

        # ── Cover-style fade ─────────────────────────────────────
        if s.cover_changing:
            if s.cover_alpha > 0:
                s.cover_alpha = max(0.0, s.cover_alpha - self.VFADE)
            else:
                s.rendered_cover_style = s.next_cover_style
                s.cover_changing       = False
        elif s.cover_alpha < 255:
            s.cover_alpha = min(255.0, s.cover_alpha + self.VFADE)

        # ── Bar-style fade ───────────────────────────────────────
        if s.bars_changing:
            if s.bars_alpha > 0:
                s.bars_alpha = max(0.0, s.bars_alpha - self.VFADE)
            else:
                s.rendered_bar_style = s.next_bar_style
                s.bars_changing      = False
        elif s.bars_alpha < 255:
            s.bars_alpha = min(255.0, s.bars_alpha + self.VFADE)

        # ── Color blend ──────────────────────────────────────────
        if s.color_blending:
            s.color_blend = min(1.0, s.color_blend + self.COLOR_BLEND_STEP)
            if s.color_blend >= 1.0:
                s.color_blending = False

        # ── Song-name visibility ─────────────────────────────────
        if s.song_name_hiding:
            if s.song_name_alpha > 0:
                s.song_name_alpha = max(0.0, s.song_name_alpha - self.VFADE)
            else:
                settings.show_song_name = False
                s.song_name_hiding      = False
                s.show_song_name        = False
        elif settings.show_song_name and s.song_name_alpha < 255:
            s.song_name_alpha = min(255.0, s.song_name_alpha + self.VFADE)

        s.show_song_name = settings.show_song_name

        # ── Intro / Outro ─────────────────────────────────────────
        p = s.phase
        f = s.phase_frame

        if p == "intro_in":
            t = min(1.0, f / self.FRAMES_IN)
            s.text_alpha  = 255
            s.phase_frame += 1
            if f >= self.FRAMES_IN:
                s.phase       = "intro_hold"
                s.phase_frame = 0
            return (False, t)  # (should_quit, window_opacity)

        elif p == "intro_hold":
            s.phase_frame += 1
            if f >= self.FRAMES_HOLD:
                s.phase       = "intro_text_out"
                s.phase_frame = 0
            return (False, 1.0)

        elif p == "intro_text_out":
            t = min(1.0, f / self.FRAMES_TEXT_OUT)
            s.text_alpha  = int(255 * (1.0 - t))
            s.phase_frame += 1
            if f >= self.FRAMES_TEXT_OUT:
                s.phase       = "running"
                s.text_alpha  = 0
                s.phase_frame = 0
            return (False, 1.0)

        elif p == "outro":
            t = min(1.0, f / self.FRAMES_OUTRO)
            s.phase_frame += 1
            if f >= self.FRAMES_OUTRO:
                return (True, 0.0)   # signal quit
            return (False, 1.0 - t)

        # "running"
        return (False, 1.0)