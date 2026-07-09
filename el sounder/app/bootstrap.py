"""
app/bootstrap.py  –  el sounder

Wires everything together once at startup: settings, audio capture,
the engine, and the overlay window. main.py just calls bootstrap()
and runs the Qt loop.
"""

from __future__ import annotations

import argparse
import sys

from PyQt6.QtWidgets import QApplication

from core.audio      import AudioCapture, print_device_list
from core.engine     import CoreEngine
from config.settings import load_settings
from backstage.windows import apply_windows_app_id
from ui.overlay      import Overlay


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="el sounder – audio visualizer overlay")
    parser.add_argument("--list-devices", action="store_true",
                        help="Print all audio devices and exit")
    parser.add_argument("--device", type=int, default=None,
                        help="Force a specific device index")
    return parser.parse_args()


def bootstrap() -> tuple[QApplication, Overlay]:
    args = parse_args()

    if args.list_devices:
        print_device_list()
        sys.exit(0)

    apply_windows_app_id()

    app = QApplication(sys.argv)
    cfg = load_settings()

    capture   = None
    error_msg = None
    try:
        capture = AudioCapture(device_index=args.device)
    except Exception as ex:
        error_msg = f"Audio Error:\n{ex}"

    core = CoreEngine(
        capture         = capture,
        samplerate      = capture.samplerate if capture else 44100,
        num_bars        = cfg["num_bars"],
        sensitivity_key = cfg["sensitivity"],
        audio_source    = cfg.get("audio_source", "spotify"),
    )

    # Always sync these via the setter (not direct attributes), so the
    # engine's Discord field-visibility matches saved settings even
    # before Discord itself gets switched on below.
    core.set_discord_options(
        show_title  = cfg.get("discord_show_title",  True),
        show_artist = cfg.get("discord_show_artist", True),
        show_cover  = cfg.get("discord_show_cover",  True),
        show_source = cfg.get("discord_show_source", True),
    )
    if cfg.get("discord_enabled"):
        core.set_discord_enabled(True)

    if cfg.get("audio_source_auto"):
        core.set_audio_source_auto(True)

    overlay = Overlay(core=core, capture=capture, error_msg=error_msg)

    from backstage.windows import set_app_icon
    set_app_icon(app, overlay)

    overlay.show()
    core.start()

    return app, overlay