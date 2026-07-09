"""
config/settings.py  –  el sounder
=====================================
Everything settings-related:

  - Default values, load/save (atomic write via tmp file)
  - SettingsController  – owns all settings state + fires callbacks
  - GradientDialog      – color-picker dialog
  - PersistentMenu      – QMenu that stays open on submenu interaction
  - Preset system       – save / load / delete named presets

The UI layer (Overlay) instantiates SettingsController and wires callbacks.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from PyQt6.QtCore    import Qt, QTimer, QEvent
from PyQt6.QtGui     import QColor, QAction, QActionGroup
from PyQt6.QtWidgets import (
    QApplication, QColorDialog, QDialog, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
    QRadioButton, QSlider, QToolTip,
    QVBoxLayout, QWidget, QWidgetAction,
)

from config.translations import tr, LANG_ORDER, TRANSLATIONS

# ──────────────────────────────────────────────────────────────────
# UI font stack (also imported by renderer modules)
# ──────────────────────────────────────────────────────────────────
UI_FONT_FAMILIES = [
    "Inter",
    "Segoe UI", "Segoe UI Variable", "Yu Gothic UI",
    "Microsoft YaHei UI", "Malgun Gothic", "Noto Sans",
]
UI_FONT_CSS = ", ".join(f"'{f}'" for f in UI_FONT_FAMILIES) + ", sans-serif"


# ──────────────────────────────────────────────────────────────────
# Color helpers (also used by renderer modules)
# ──────────────────────────────────────────────────────────────────
def hex_to_qcolor(h: str) -> QColor:
    try:
        h = h.strip()
        if not h.startswith("#"):
            h = "#" + h
        return QColor(h)
    except Exception:
        return QColor(255, 255, 255)


def lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    r = int(c1.red()   + (c2.red()   - c1.red())   * t)
    g = int(c1.green() + (c2.green() - c1.green()) * t)
    b = int(c1.blue()  + (c2.blue()  - c1.blue())  * t)
    return QColor(r, g, b)


# ──────────────────────────────────────────────────────────────────
# Storage paths and defaults
# ──────────────────────────────────────────────────────────────────
SETTINGS_DIR  = Path(os.environ.get("APPDATA", Path.home())) / "ElSounder"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

DEFAULT_SETTINGS: dict = {
    "language":             "en_us",
    "sensitivity":          "normal",
    "opacity_mode":         "milky",
    "always_on_top":        True,
    "always_on_bottom":     False,
    "num_bars":             90,
    "window_x":             300,
    "window_y":             150,
    "bar_color_mode":       "rainbow",
    "bar_color_hex":        "#89dceb",
    "bar_gradient_from":    "#89b4fa",
    "bar_gradient_to":      "#f38ba8",
    "bar_gradient_dir":     "freq",
    "bar_style":            "bar",
    "cover_style":          "flat",
    "show_song_name":       True,
    "song_name_size":       22,
    "arm_visible":          False,
    "discord_enabled":      False,
    "discord_show_title":   True,
    "discord_show_artist":  True,
    "discord_show_cover":   True,
    "discord_show_source":  True,
    "audio_source":         "spotify",   # "spotify" = only Spotify, "all" = whole PC
    "audio_source_auto":    False,       # auto-switch spotify<->all based on whether Spotify.exe is running
}

_save_lock = threading.Lock()


def load_settings() -> dict:
    """Load settings from disk, merging with defaults for any missing keys."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrate old German values
            _sens_map = {"Kaum": "low", "Normal": "normal", "Sehr stark": "high"}
            if data.get("sensitivity") in _sens_map:
                data["sensitivity"] = _sens_map[data["sensitivity"]]
            _op_map = {"milchig": "milky", "grau": "gray", "transparent": "transparent"}
            if data.get("opacity_mode") in _op_map:
                data["opacity_mode"] = _op_map[data["opacity_mode"]]
            # Fill missing keys with defaults
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            # Type sanity checks
            for k in ("num_bars", "window_x", "window_y", "song_name_size"):
                if not isinstance(data.get(k), int):
                    data[k] = DEFAULT_SETTINGS[k]
            for k in ("always_on_top", "always_on_bottom", "show_song_name",
                       "arm_visible", "discord_enabled", "discord_show_title",
                       "discord_show_artist", "discord_show_cover",
                       "discord_show_source", "audio_source_auto"):
                if not isinstance(data.get(k), bool):
                    data[k] = DEFAULT_SETTINGS[k]
            for k in ("language", "sensitivity", "opacity_mode", "bar_color_mode",
                       "bar_style", "cover_style", "bar_gradient_dir", "audio_source"):
                if not isinstance(data.get(k), str):
                    data[k] = DEFAULT_SETTINGS[k]
            if data.get("audio_source") not in ("spotify", "all"):
                data["audio_source"] = DEFAULT_SETTINGS["audio_source"]
            return data
    except Exception as ex:
        print(f"[Settings] load failed: {ex}")
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict) -> None:
    """Thread-safe atomic write via tmp file."""
    with _save_lock:
        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2, ensure_ascii=False)
            tmp.replace(SETTINGS_FILE)
        except Exception as ex:
            print(f"[Settings] save failed: {ex}")


# ──────────────────────────────────────────────────────────────────
# Preset system
# ──────────────────────────────────────────────────────────────────
PRESETS_FILE = SETTINGS_DIR / "presets.json"

_PRESET_KEYS = (
    "sensitivity", "opacity_mode", "num_bars",
    "bar_color_mode", "bar_color_hex",
    "bar_gradient_from", "bar_gradient_to", "bar_gradient_dir",
    "bar_style", "cover_style",
    "show_song_name", "song_name_size", "arm_visible",
)


def list_presets() -> dict[str, dict]:
    try:
        if PRESETS_FILE.exists():
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as ex:
        print(f"[Presets] load failed: {ex}")
    return {}


def save_preset(name: str, current_settings: dict) -> None:
    with _save_lock:
        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            presets = list_presets()
            presets[name] = {k: current_settings[k] for k in _PRESET_KEYS if k in current_settings}
            tmp = PRESETS_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(presets, f, indent=2, ensure_ascii=False)
            tmp.replace(PRESETS_FILE)
        except Exception as ex:
            print(f"[Presets] save failed: {ex}")


def delete_preset(name: str) -> None:
    with _save_lock:
        try:
            presets = list_presets()
            if name in presets:
                del presets[name]
                tmp = PRESETS_FILE.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(presets, f, indent=2, ensure_ascii=False)
                tmp.replace(PRESETS_FILE)
        except Exception as ex:
            print(f"[Presets] delete failed: {ex}")


# ──────────────────────────────────────────────────────────────────
# PersistentMenu
# ──────────────────────────────────────────────────────────────────
class PersistentMenu(QMenu):
    _STYLE = (
        "QMenu {background-color:#1e1e2e;color:#cdd6f4;border:1px solid #45475a;"
        "border-radius:8px;padding:4px;}"
        "QMenu::item{padding:5px 20px 5px 10px;border-radius:4px;}"
        "QMenu::item:selected{background-color:#313244;}"
        "QMenu::item:disabled{color:#585b70;}"
        "QMenu::separator{height:1px;background:#45475a;margin:4px 8px;}"
    )

    def __init__(self, title_or_parent=None, parent=None):
        if isinstance(title_or_parent, str):
            super().__init__(title_or_parent, parent)
        elif title_or_parent is not None:
            super().__init__(title_or_parent)
        else:
            super().__init__(parent)
        self.setStyleSheet(self._STYLE)

    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.Type.ToolTip:
            action = self.activeAction()
            if action:
                tip = action.toolTip()
                if tip and tip != action.text():
                    QToolTip.showText(ev.globalPos(), tip, self)
                    return True
        return super().event(ev)


# ──────────────────────────────────────────────────────────────────
# GradientDialog
# ──────────────────────────────────────────────────────────────────
class GradientDialog(QDialog):
    def __init__(self, parent, from_hex: str, to_hex: str, direction: str, lang: str):
        super().__init__(parent)
        self.setWindowTitle(tr(lang, "grad_title"))
        self.setMinimumWidth(380)
        self.setStyleSheet(f"""
            QDialog{{background:#1e1e2e;color:#cdd6f4;font-family:{UI_FONT_CSS};}}
            QLabel{{color:#cdd6f4;}}
            QPushButton{{background:#313244;color:#cdd6f4;border:1px solid #45475a;
                border-radius:6px;padding:6px 14px;}}
            QPushButton:hover{{background:#45475a;}}
            QGroupBox{{color:#cdd6f4;border:1px solid #45475a;border-radius:6px;
                margin-top:8px;padding:6px;}}
            QGroupBox::title{{subcontrol-origin:margin;left:8px;}}
            QRadioButton{{color:#cdd6f4;}}
        """)
        self._from_hex = from_hex
        self._to_hex   = to_hex
        self._lang     = lang
        layout = QVBoxLayout(self)

        row1 = QHBoxLayout()
        self._btn_from = QPushButton()
        self._update_btn_color(self._btn_from, self._from_hex)
        self._btn_from.clicked.connect(self._pick_from)
        row1.addWidget(QLabel(tr(lang, "grad_from")))
        row1.addWidget(self._btn_from)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self._btn_to = QPushButton()
        self._update_btn_color(self._btn_to, self._to_hex)
        self._btn_to.clicked.connect(self._pick_to)
        row2.addWidget(QLabel(tr(lang, "grad_to")))
        row2.addWidget(self._btn_to)
        layout.addLayout(row2)

        grp     = QGroupBox(tr(lang, "grad_dir"))
        grp_lay = QVBoxLayout(grp)
        self._rb_freq   = QRadioButton(tr(lang, "grad_freq"))
        self._rb_radius = QRadioButton(tr(lang, "grad_radius"))
        self._rb_freq.setChecked(direction == "freq")
        self._rb_radius.setChecked(direction == "radius")
        grp_lay.addWidget(self._rb_freq)
        grp_lay.addWidget(self._rb_radius)
        layout.addWidget(grp)

        btn_row = QHBoxLayout()
        ok_btn  = QPushButton(tr(lang, "ok"))
        ok_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _update_btn_color(self, btn, hex_str):
        c   = hex_to_qcolor(hex_str)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg  = "#000000" if lum > 140 else "#ffffff"
        btn.setText(hex_str)
        btn.setStyleSheet(
            f"background:{hex_str};color:{fg};border:1px solid #45475a;"
            f"border-radius:6px;padding:6px 14px;"
        )

    def _pick_from(self):
        c = QColorDialog.getColor(hex_to_qcolor(self._from_hex), self)
        if c.isValid():
            self._from_hex = c.name()
            self._update_btn_color(self._btn_from, self._from_hex)

    def _pick_to(self):
        c = QColorDialog.getColor(hex_to_qcolor(self._to_hex), self)
        if c.isValid():
            self._to_hex = c.name()
            self._update_btn_color(self._btn_to, self._to_hex)

    def result_values(self) -> tuple[str, str, str]:
        direction = "freq" if self._rb_freq.isChecked() else "radius"
        return self._from_hex, self._to_hex, direction


# ──────────────────────────────────────────────────────────────────
# SettingsController
# ──────────────────────────────────────────────────────────────────
class SettingsController:
    """
    Owns all settings state; builds the context menu; fires callbacks
    when any value changes.

    The Overlay instantiates this and wires all on_* callbacks before
    showing the window.
    """

    def __init__(self, cfg: dict):
        # ── State ────────────────────────────────────────────────
        self.language            = cfg["language"]
        self.sensitivity_key     = cfg["sensitivity"]
        self.opacity_mode        = cfg["opacity_mode"]
        self.always_on_top       = cfg["always_on_top"]
        self.always_on_bottom    = cfg["always_on_bottom"]
        self.num_bars            = cfg["num_bars"]
        self.bar_color_mode      = cfg["bar_color_mode"]
        self.bar_color_hex       = cfg["bar_color_hex"]
        self.bar_gradient_from   = cfg["bar_gradient_from"]
        self.bar_gradient_to     = cfg["bar_gradient_to"]
        self.bar_gradient_dir    = cfg["bar_gradient_dir"]
        self.bar_style           = cfg["bar_style"]
        self.cover_style         = cfg["cover_style"]
        self.show_song_name      = cfg["show_song_name"]
        self.song_name_size      = cfg["song_name_size"]
        self.arm_visible         = cfg.get("arm_visible", False)
        self.discord_enabled     = cfg.get("discord_enabled", False)
        self.discord_show_title  = cfg.get("discord_show_title",  True)
        self.discord_show_artist = cfg.get("discord_show_artist", True)
        self.discord_show_cover  = cfg.get("discord_show_cover",  True)
        self.discord_show_source = cfg.get("discord_show_source", True)
        self.audio_source        = cfg.get("audio_source", "spotify")
        self.audio_source_auto   = cfg.get("audio_source_auto", False)

        # ── Callbacks (wired by Overlay) ─────────────────────────
        self.on_opacity_change      = None
        self.on_bar_style_change    = None
        self.on_cover_style_change  = None
        self.on_bar_color_change    = None   # (mode, hex, grad_from, grad_to, grad_dir)
        self.on_num_bars_change     = None
        self.on_sensitivity_change  = None
        self.on_audio_source_change = None
        self.on_audio_source_auto_change = None
        self.on_always_on_top       = None
        self.on_always_on_bottom    = None
        self.on_show_song_name      = None
        self.on_song_name_size      = None
        self.on_language_change     = None
        self.on_discord_enabled     = None
        self.on_discord_options     = None   # (show_title, show_artist, show_cover, show_source)
        self.on_quit                = None
        self.on_reset               = None
        self.on_save_needed         = None

        self._save_pending = False

    # ── Helpers ─────────────────────────────────────────────────

    def _t(self, key: str) -> str:
        return tr(self.language, key)

    def as_dict(self, window_pos=None) -> dict:
        x, y = (window_pos.x(), window_pos.y()) if window_pos else (300, 150)
        return {
            "language":            self.language,
            "sensitivity":         self.sensitivity_key,
            "opacity_mode":        self.opacity_mode,
            "always_on_top":       self.always_on_top,
            "always_on_bottom":    self.always_on_bottom,
            "num_bars":            self.num_bars,
            "window_x":            x,
            "window_y":            y,
            "bar_color_mode":      self.bar_color_mode,
            "bar_color_hex":       self.bar_color_hex,
            "bar_gradient_from":   self.bar_gradient_from,
            "bar_gradient_to":     self.bar_gradient_to,
            "bar_gradient_dir":    self.bar_gradient_dir,
            "bar_style":           self.bar_style,
            "cover_style":         self.cover_style,
            "show_song_name":      self.show_song_name,
            "song_name_size":      self.song_name_size,
            "arm_visible":         self.arm_visible,
            "discord_enabled":     self.discord_enabled,
            "discord_show_title":  self.discord_show_title,
            "discord_show_artist": self.discord_show_artist,
            "discord_show_cover":  self.discord_show_cover,
            "discord_show_source": self.discord_show_source,
            "audio_source":        self.audio_source,
            "audio_source_auto":   self.audio_source_auto,
        }

    def _fire(self, cb, *args):
        if cb:
            cb(*args)
        if self.on_save_needed:
            self.on_save_needed()

    def _current_settings(self) -> dict:
        return self.as_dict()

    def _apply_preset(self, data: dict) -> None:
        if "sensitivity" in data:
            self.sensitivity_key = data["sensitivity"]
            if self.on_sensitivity_change:
                self.on_sensitivity_change(self.sensitivity_key)
        if "opacity_mode" in data:
            self.opacity_mode = data["opacity_mode"]
            if self.on_opacity_change:
                self.on_opacity_change(self.opacity_mode)
        if "num_bars" in data:
            self.num_bars = data["num_bars"]
            if self.on_num_bars_change:
                self.on_num_bars_change(self.num_bars)
        if "bar_style" in data:
            self.bar_style = data["bar_style"]
            if self.on_bar_style_change:
                self.on_bar_style_change(self.bar_style)
        if "cover_style" in data:
            self.cover_style = data["cover_style"]
            if self.on_cover_style_change:
                self.on_cover_style_change(self.cover_style)
        if "show_song_name" in data:
            self.show_song_name = data["show_song_name"]
            if self.on_show_song_name:
                self.on_show_song_name(self.show_song_name)
        if "song_name_size" in data:
            self.song_name_size = data["song_name_size"]
            if self.on_song_name_size:
                self.on_song_name_size(self.song_name_size)
        for k in ("bar_color_mode", "bar_color_hex",
                  "bar_gradient_from", "bar_gradient_to", "bar_gradient_dir"):
            if k in data:
                setattr(self, k, data[k])
        if self.on_bar_color_change:
            self.on_bar_color_change(
                self.bar_color_mode, self.bar_color_hex,
                self.bar_gradient_from, self.bar_gradient_to, self.bar_gradient_dir,
            )
        if self.on_save_needed:
            self.on_save_needed()

    # ── Setters ─────────────────────────────────────────────────

    def set_sensitivity(self, key: str):
        self.sensitivity_key = key
        self._fire(self.on_sensitivity_change, key)

    def set_audio_source(self, key: str):
        self.audio_source = key
        self._fire(self.on_audio_source_change, key)

    def set_audio_source_auto(self, checked: bool):
        self.audio_source_auto = checked
        self._fire(self.on_audio_source_auto_change, checked)

    def set_opacity(self, mode: str):
        self.opacity_mode = mode
        self._fire(self.on_opacity_change, mode)

    def set_always_on_top(self, checked: bool):
        self.always_on_top = checked
        if checked:
            self.always_on_bottom = False
        self._fire(self.on_always_on_top, checked)

    def set_always_on_bottom(self, checked: bool):
        self.always_on_bottom = checked
        if checked:
            self.always_on_top = False
        self._fire(self.on_always_on_bottom, checked)

    def set_num_bars(self, n: int):
        self.num_bars = n
        self._fire(self.on_num_bars_change, n)

    def set_bar_style(self, style: str):
        if style == self.bar_style:
            return
        self.bar_style = style
        self._fire(self.on_bar_style_change, style)

    def set_bar_color_mode(self, mode: str, parent_widget=None):
        if mode == "solid":
            c = QColorDialog.getColor(hex_to_qcolor(self.bar_color_hex), parent_widget)
            if not c.isValid():
                return
            self.bar_color_hex  = c.name()
            self.bar_color_mode = "solid"
        elif mode == "gradient":
            dlg = GradientDialog(
                parent_widget, self.bar_gradient_from,
                self.bar_gradient_to, self.bar_gradient_dir, self.language
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            f, t, d = dlg.result_values()
            self.bar_gradient_from = f
            self.bar_gradient_to   = t
            self.bar_gradient_dir  = d
            self.bar_color_mode    = "gradient"
        else:
            self.bar_color_mode = mode
        self._fire(
            self.on_bar_color_change,
            self.bar_color_mode, self.bar_color_hex,
            self.bar_gradient_from, self.bar_gradient_to, self.bar_gradient_dir,
        )

    def set_cover_style(self, style: str):
        if style == self.cover_style:
            return
        self.cover_style = style
        self._fire(self.on_cover_style_change, style)

    def set_show_song_name(self, checked: bool):
        self.show_song_name = checked
        self._fire(self.on_show_song_name, checked)

    def set_song_name_size(self, size: int):
        self.song_name_size = size
        self._fire(self.on_song_name_size, size)

    def set_language(self, lang_key: str):
        self.language = lang_key
        self._fire(self.on_language_change, lang_key)

    def set_discord_enabled(self, checked: bool):
        self.discord_enabled = checked
        self._fire(self.on_discord_enabled, checked)

    def set_discord_show_title(self, checked: bool):
        self.discord_show_title = checked
        self._fire(
            self.on_discord_options,
            self.discord_show_title, self.discord_show_artist,
            self.discord_show_cover, self.discord_show_source,
        )

    def set_discord_show_artist(self, checked: bool):
        self.discord_show_artist = checked
        self._fire(
            self.on_discord_options,
            self.discord_show_title, self.discord_show_artist,
            self.discord_show_cover, self.discord_show_source,
        )

    def set_discord_show_cover(self, checked: bool):
        self.discord_show_cover = checked
        self._fire(
            self.on_discord_options,
            self.discord_show_title, self.discord_show_artist,
            self.discord_show_cover, self.discord_show_source,
        )

    def set_discord_show_source(self, checked: bool):
        self.discord_show_source = checked
        self._fire(
            self.on_discord_options,
            self.discord_show_title, self.discord_show_artist,
            self.discord_show_cover, self.discord_show_source,
        )

    def reset_to_defaults(self):
        d = dict(DEFAULT_SETTINGS)
        self.language            = d["language"]
        self.sensitivity_key     = d["sensitivity"]
        self.opacity_mode        = d["opacity_mode"]
        self.always_on_top       = d["always_on_top"]
        self.always_on_bottom    = d["always_on_bottom"]
        self.num_bars            = d["num_bars"]
        self.bar_color_mode      = d["bar_color_mode"]
        self.bar_color_hex       = d["bar_color_hex"]
        self.bar_gradient_from   = d["bar_gradient_from"]
        self.bar_gradient_to     = d["bar_gradient_to"]
        self.bar_gradient_dir    = d["bar_gradient_dir"]
        self.bar_style           = d["bar_style"]
        self.cover_style         = d["cover_style"]
        self.show_song_name      = d["show_song_name"]
        self.song_name_size      = d["song_name_size"]
        self.arm_visible         = d["arm_visible"]
        self.discord_enabled     = d["discord_enabled"]
        self.discord_show_title  = d["discord_show_title"]
        self.discord_show_artist = d["discord_show_artist"]
        self.discord_show_cover  = d["discord_show_cover"]
        self.discord_show_source = d["discord_show_source"]
        self.audio_source        = d["audio_source"]
        self.audio_source_auto   = d["audio_source_auto"]
        self._fire(self.on_reset, d)

    # ── Context menu ─────────────────────────────────────────────

    def show_context_menu(self, global_pos, parent_widget):
        menu = PersistentMenu(parent_widget)
        menu.setStyleSheet(
            f"QMenu{{font-family:{UI_FONT_CSS};font-size:12px;}}" + PersistentMenu._STYLE
        )

        # Style category
        style_cat = PersistentMenu(self._t("style"), parent_widget)

        bar_style_menu = PersistentMenu(self._t("bar_style"), style_cat)
        bar_style_menu.setToolTipsVisible(True)
        bar_style_grp  = QActionGroup(bar_style_menu)
        bar_style_grp.setExclusive(True)
        for key, disp_key in [("bar", "style_bar"), ("wave", "style_wave")]:
            act = QAction(self._t(disp_key), bar_style_menu, checkable=True)
            act.setChecked(self.bar_style == key)
            act.triggered.connect(lambda _, k=key: self.set_bar_style(k))
            bar_style_grp.addAction(act)
            bar_style_menu.addAction(act)
        style_cat.addMenu(bar_style_menu)

        color_menu = PersistentMenu(self._t("bar_color"), style_cat)
        color_menu.setToolTipsVisible(True)
        color_grp  = QActionGroup(color_menu)
        color_grp.setExclusive(True)

        rgb_act = QAction(self._t("color_rgb"), color_menu, checkable=True)
        rgb_act.setChecked(self.bar_color_mode == "rainbow")
        rgb_act.setToolTip("Full hue rotation across bars")
        rgb_act.triggered.connect(lambda _=False: self.set_bar_color_mode("rainbow", parent_widget))
        color_grp.addAction(rgb_act)
        color_menu.addAction(rgb_act)

        sol_act = QAction(self._t("color_solid"), color_menu, checkable=True)
        sol_act.setChecked(self.bar_color_mode == "solid")
        sol_act.setToolTip("Pick one fixed color")
        sol_act.triggered.connect(lambda _=False: self.set_bar_color_mode("solid", parent_widget))
        color_grp.addAction(sol_act)
        color_menu.addAction(sol_act)

        grad_act = QAction(self._t("color_gradient"), color_menu, checkable=True)
        grad_act.setChecked(self.bar_color_mode == "gradient")
        grad_act.setToolTip("Two-color fade by frequency or level")
        grad_act.triggered.connect(lambda _=False: self.set_bar_color_mode("gradient", parent_widget))
        color_grp.addAction(grad_act)
        color_menu.addAction(grad_act)
        style_cat.addMenu(color_menu)

        bars_menu = PersistentMenu(f"{self._t('num_bars')}  ({self.num_bars})", style_cat)
        bars_grp  = QActionGroup(bars_menu)
        bars_grp.setExclusive(True)
        for n in [30, 60, 90, 120, 180, 270, 400, 800]:
            act = QAction(str(n), bars_menu, checkable=True)
            act.setChecked(self.num_bars == n)
            act.triggered.connect(lambda _, nb=n: self.set_num_bars(nb))
            bars_grp.addAction(act)
            bars_menu.addAction(act)
        style_cat.addMenu(bars_menu)

        sens_menu = PersistentMenu(self._t("sensitivity"), style_cat)
        sens_menu.setToolTipsVisible(True)
        sens_grp  = QActionGroup(sens_menu)
        sens_grp.setExclusive(True)
        for key, disp_key in [("low", "sens_low"), ("normal", "sens_normal"), ("high", "sens_high")]:
            act = QAction(self._t(disp_key), sens_menu, checkable=True)
            act.setChecked(self.sensitivity_key == key)
            act.triggered.connect(lambda _, k=key: self.set_sensitivity(k))
            sens_grp.addAction(act)
            sens_menu.addAction(act)
        style_cat.addMenu(sens_menu)
        menu.addMenu(style_cat)

        # Audio source category
        audio_cat = PersistentMenu(self._t("audio_source"), parent_widget)
        audio_cat.setToolTipsVisible(True)

        audio_auto = QAction(self._t("audio_source_auto"), audio_cat, checkable=True)
        audio_auto.setChecked(self.audio_source_auto)
        audio_auto.setToolTip(
            "Automatically switch to 'Only Spotify' while Spotify.exe is "
            "running, and back to 'All PC Sound' once it's closed."
        )
        audio_auto.triggered.connect(self.set_audio_source_auto)
        audio_cat.addAction(audio_auto)
        audio_cat.addSeparator()

        audio_grp = QActionGroup(audio_cat)
        audio_grp.setExclusive(True)
        audio_tips = {
            "spotify": "Only Spotify's own sound — nothing else, no fallback.",
            "all":     "Everything playing on your PC.",
        }
        for key, disp_key in [("spotify", "audio_source_spotify"), ("all", "audio_source_all")]:
            act = QAction(self._t(disp_key), audio_cat, checkable=True)
            act.setChecked(self.audio_source == key)
            act.setEnabled(not self.audio_source_auto)
            act.setToolTip(audio_tips[key])
            act.triggered.connect(lambda _, k=key: self.set_audio_source(k))
            audio_grp.addAction(act)
            audio_cat.addAction(act)
        menu.addMenu(audio_cat)

        # Cover category
        cover_cat = PersistentMenu(self._t("cover"), parent_widget)
        cover_cat.setToolTipsVisible(True)
        cover_grp = QActionGroup(cover_cat)
        cover_grp.setExclusive(True)
        cover_tips = {
            "none":  "No album art",
            "flat":  "Semi-transparent cover behind bars",
            "vinyl": "Rotating vinyl record with label art",
        }
        for key, disp_key in [("none", "cover_off"), ("flat", "cover_flat"), ("vinyl", "cover_vinyl")]:
            act = QAction(self._t(disp_key), cover_cat, checkable=True)
            act.setChecked(self.cover_style == key)
            act.setToolTip(cover_tips[key])
            act.triggered.connect(lambda _, k=key: self.set_cover_style(k))
            cover_grp.addAction(act)
            cover_cat.addAction(act)

        cover_cat.addSeparator()
        sn_act = QAction(self._t("song_name"), cover_cat, checkable=True)
        sn_act.setChecked(self.show_song_name)
        sn_act.setToolTip("Show title & artist text on the visualizer")
        sn_act.triggered.connect(self.set_show_song_name)
        cover_cat.addAction(sn_act)

        if self.cover_style != "vinyl":
            cover_cat.addSeparator()
            _size_w = QWidget()
            _size_w.setStyleSheet("background:transparent;")
            _size_l = QHBoxLayout(_size_w)
            _size_l.setContentsMargins(16, 2, 8, 2)
            _size_l.setSpacing(6)
            _lbl = QLabel(f"Aa  {self.song_name_size}pt")
            _lbl.setStyleSheet(
                f"color:#cdd6f4;font-family:{UI_FONT_CSS};font-size:12px;"
                f"background:transparent;min-width:52px;"
            )
            _slider = QSlider(Qt.Orientation.Horizontal)
            _slider.setRange(7, 22)
            _slider.setValue(self.song_name_size)
            _slider.setFixedWidth(100)
            _slider.setStyleSheet(
                "QSlider::groove:horizontal{background:#45475a;height:4px;border-radius:2px;}"
                "QSlider::sub-page:horizontal{background:#89b4fa;height:4px;border-radius:2px;}"
                "QSlider::handle:horizontal{background:#89b4fa;width:12px;height:12px;"
                "border-radius:6px;margin:-4px 0;}"
            )

            def _on_size(v, lbl=_lbl):
                self.set_song_name_size(v)
                lbl.setText(f"Aa  {v}pt")

            _slider.valueChanged.connect(_on_size)
            _size_l.addWidget(_lbl)
            _size_l.addWidget(_slider)
            _wa = QWidgetAction(cover_cat)
            _wa.setDefaultWidget(_size_w)
            cover_cat.addAction(_wa)
        menu.addMenu(cover_cat)

        # Background category
        bg_cat = PersistentMenu(self._t("background"), parent_widget)
        bg_cat.setToolTipsVisible(True)
        bg_grp = QActionGroup(bg_cat)
        bg_grp.setExclusive(True)
        bg_tips = {
            "transparent":  "Fully see-through circle",
            "milky":        "Frosted glass look",
            "gray":         "Solid gray circle",
            "chroma_green": "Green background — use Chroma Key in OBS",
            "chroma_black": "Black background — use Luma Key in OBS",
        }
        bg_labels = {
            "transparent":  self._t("bg_transparent"),
            "milky":        self._t("bg_milky"),
            "gray":         self._t("bg_gray"),
            "chroma_green": self._t("bg_chroma_green"),
            "chroma_black": self._t("bg_chroma_black"),
        }
        for key in ["transparent", "milky", "gray", "chroma_green", "chroma_black"]:
            act = QAction(bg_labels[key], bg_cat, checkable=True)
            act.setChecked(self.opacity_mode == key)
            act.setToolTip(bg_tips[key])
            act.triggered.connect(lambda _, k=key: self.set_opacity(k))
            bg_grp.addAction(act)
            bg_cat.addAction(act)
        menu.addMenu(bg_cat)
        menu.addSeparator()

        # Discord category
        dc_cat = PersistentMenu(self._t("discord_rp"), parent_widget)
        dc_cat.setToolTipsVisible(True)

        dc_enable = QAction(self._t("discord_enable"), dc_cat, checkable=True)
        dc_enable.setChecked(self.discord_enabled)
        dc_enable.setToolTip("Show current song on your Discord profile")
        dc_enable.triggered.connect(self.set_discord_enabled)
        dc_cat.addAction(dc_enable)
        dc_cat.addSeparator()

        dc_title = QAction(self._t("discord_show_title"), dc_cat, checkable=True)
        dc_title.setChecked(self.discord_show_title)
        dc_title.setEnabled(self.discord_enabled)
        dc_title.setToolTip("Include song title in Discord status")
        dc_title.triggered.connect(self.set_discord_show_title)
        dc_cat.addAction(dc_title)

        dc_artist = QAction(self._t("discord_show_artist"), dc_cat, checkable=True)
        dc_artist.setChecked(self.discord_show_artist)
        dc_artist.setEnabled(self.discord_enabled)
        dc_artist.setToolTip("Include artist name in Discord status")
        dc_artist.triggered.connect(self.set_discord_show_artist)
        dc_cat.addAction(dc_artist)

        dc_cover = QAction(self._t("discord_show_cover"), dc_cat, checkable=True)
        dc_cover.setChecked(self.discord_show_cover)
        dc_cover.setEnabled(self.discord_enabled)
        dc_cover.setToolTip("Show album art via catbox.moe upload")
        dc_cover.triggered.connect(self.set_discord_show_cover)
        dc_cat.addAction(dc_cover)

        dc_source = QAction(self._t("discord_show_source"), dc_cat, checkable=True)
        dc_source.setChecked(self.discord_show_source)
        dc_source.setEnabled(self.discord_enabled)
        dc_source.setToolTip("Show where the audio comes from (Spotify, YouTube, ...) on the cover")
        dc_source.triggered.connect(self.set_discord_show_source)
        dc_cat.addAction(dc_source)
        menu.addMenu(dc_cat)
        menu.addSeparator()

        # Window flags
        aot = QAction(self._t("always_on_top"), parent_widget, checkable=True)
        aot.setChecked(self.always_on_top)
        aot.setToolTip("Keep visualizer window above all other windows")
        aot.triggered.connect(self.set_always_on_top)
        menu.addAction(aot)

        aob = QAction(self._t("always_on_bottom"), parent_widget, checkable=True)
        aob.setChecked(self.always_on_bottom)
        aob.setToolTip("Stay on desktop, below all windows")
        aob.triggered.connect(self.set_always_on_bottom)
        menu.addAction(aob)

        # Language
        lang_menu = PersistentMenu(self._t("language"), parent_widget)
        lang_grp  = QActionGroup(lang_menu)
        lang_grp.setExclusive(True)
        for lang_key in LANG_ORDER:
            lang_name = TRANSLATIONS[lang_key]["lang_name"]
            act = QAction(lang_name, lang_menu, checkable=True)
            act.setChecked(self.language == lang_key)
            act.triggered.connect(lambda _, lk=lang_key: self.set_language(lk))
            lang_grp.addAction(act)
            lang_menu.addAction(act)
        menu.addMenu(lang_menu)
        menu.addSeparator()

        # Presets
        preset_menu = PersistentMenu(self._t("presets"), parent_widget)

        save_preset_act = QAction(self._t("preset_save"), preset_menu)
        def _do_save_preset():
            name, ok = QInputDialog.getText(
                parent_widget,
                self._t("preset_save_title"),
                self._t("preset_save_label"),
            )
            name = name.strip()
            if ok and name:
                save_preset(name, self._current_settings())
        save_preset_act.triggered.connect(_do_save_preset)
        preset_menu.addAction(save_preset_act)

        presets = list_presets()
        if presets:
            preset_menu.addSeparator()
            load_sub = PersistentMenu(self._t("preset_load"), preset_menu)
            del_sub  = PersistentMenu(self._t("preset_delete"), preset_menu)
            for pname, pdata in presets.items():
                load_act = QAction(pname, load_sub)
                def _do_load(checked=False, d=pdata):
                    self._apply_preset(d)
                load_act.triggered.connect(_do_load)
                load_sub.addAction(load_act)

                del_act = QAction(pname, del_sub)
                def _do_del(checked=False, n=pname):
                    if QMessageBox.question(
                        parent_widget,
                        self._t("preset_delete_title"),
                        self._t("preset_delete_confirm").format(name=n),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    ) == QMessageBox.StandardButton.Yes:
                        delete_preset(n)
                del_act.triggered.connect(_do_del)
                del_sub.addAction(del_act)

            preset_menu.addMenu(load_sub)
            preset_menu.addMenu(del_sub)

        menu.addMenu(preset_menu)
        menu.addSeparator()

        # Reset / Quit
        reset_act = QAction(self._t("reset_defaults"), parent_widget)
        reset_act.triggered.connect(self.reset_to_defaults)
        menu.addAction(reset_act)
        menu.addSeparator()

        exit_act = QAction(self._t("quit"), parent_widget)
        exit_act.triggered.connect(lambda: self.on_quit() if self.on_quit else None)
        menu.addAction(exit_act)

        menu.exec(global_pos)