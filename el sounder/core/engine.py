"""
core/engine.py  –  el sounder

The conductor. Doesn't capture audio or talk to Windows itself — just
tells AudioCapture, AudioAnalyzer, SMTCMonitor and DiscordRPC when to
start, stop, and hand data to each other. The UI only ever talks to
this file.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from core.audio import AudioCapture, AudioAnalyzer
from core.smtc  import SMTCMonitor

try:
    from core.audio_app_loopback import SpotifyAudioManager, _get_spotify_pids
    _APP_LOOPBACK_OK = True
except Exception:
    _APP_LOOPBACK_OK = False
    def _get_spotify_pids():  # type: ignore[no-redef]
        return []

_LOUD = os.environ.get("EL_SOUNDER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

def _murmur(msg: str) -> None:
    if _LOUD:
        print(msg)

def _holler(msg: str) -> None:
    print(msg)


try:
    from backstage.discord_rpc import DiscordRPC, DISCORD_CLIENT_ID
    _DISCORD_OK = True
except ImportError:
    _DISCORD_OK = False
    DISCORD_CLIENT_ID = ""

    class DiscordRPC:          # type: ignore[no-redef]
        connected = False
        def connect(self): return False
        def disconnect(self): pass
        def update(self, *a, **kw): pass
        def update_idle(self): pass
        def prefetch_cover(self, *a, **kw): pass


class CoreEngine:
    """
    Ties together AudioCapture, AudioAnalyzer, SMTCMonitor and DiscordRPC.
    The UI layer talks exclusively to this — nothing below leaks upward.
    """

    def __init__(
        self,
        capture: AudioCapture | None = None,
        samplerate: int = 44100,
        num_bars: int = 90,
        sensitivity_key: str = "normal",
        audio_source: str = "spotify",
    ):
        self.capture  = capture
        self.analyzer = AudioAnalyzer(num_bars, samplerate, sensitivity_key, capture=capture)
        self.discord  = DiscordRPC(DISCORD_CLIENT_ID) if _DISCORD_OK else DiscordRPC()
        self.smtc     = SMTCMonitor(on_update=self._on_smtc_update, mode=audio_source)

        # "spotify" = only Spotify's own audio (no fallback, ever).
        # "all"     = whole PC / system loopback, always on.
        self.audio_source = audio_source if audio_source in ("spotify", "all") else "spotify"

        self.spotify_audio: SpotifyAudioManager | None = None
        if _APP_LOOPBACK_OK:
            # No fallback_capture on purpose: in "spotify" mode we never want
            # to silently slide over to system-wide audio.
            self.spotify_audio = SpotifyAudioManager(fallback_capture=None)

        self._track_callback: object = None
        self._watchdog_running = False
        self._started = False
        self._switch_lock = threading.Lock()

        self._discord_enabled     = False
        self._discord_show_title  = True
        self._discord_show_artist = True
        self._discord_show_cover  = True
        self._discord_show_source = True

        self._cur_title:      str          = ""
        self._cur_artist:     str          = ""
        self._cur_img_bytes:  bytes | None = None
        self._cur_source_app_id: str       = ""

        # Auto audio-source switching (spotify <-> all) based on whether
        # Spotify.exe is currently running.
        self.audio_source_auto: bool = False
        self._auto_source_running    = False
        self.on_audio_source_auto_switch = None  # callback(mode: str) -> None

        # Absolute silence threshold (in db_pct, 0–100). Below this, bars
        # are forced to zero regardless of mode — used both by
        # get_bands_db() (see below) and the "all" mode watchdog to detect
        # a dead/wrong loopback device.
        self._min_loopback_db: float = 1.5

    # ── lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        self._start_audio_path()
        self.analyzer.start()
        self.smtc.start()
        self._started = True

    def shutdown(self) -> None:
        self.analyzer.stop()
        self.smtc.stop()
        self._watchdog_running     = False
        self._auto_source_running  = False
        if self.spotify_audio:
            self.spotify_audio.stop()
        if self.capture is not None:
            self.capture.set_passthrough(False)
        if self.discord.connected:
            self.discord.disconnect()

    # ── audio-path switching ───────────────────────────────────────

    def _start_audio_path(self) -> None:
        """
        Wires up capture/spotify_audio according to self.audio_source.
        Exactly one of them ever writes into the shared latest_audio
        buffer at a time.
        """
        self._watchdog_running = False  # stop any previous watchdog loop

        if self.audio_source == "spotify":
            # Make sure system-wide capture never writes into the shared
            # buffer in this mode — no fallback, ever.
            if self.capture is not None:
                self.capture.set_passthrough(False)
            if self.spotify_audio is not None:
                self.spotify_audio.start()   # probes API + watches for Spotify.exe
                self.analyzer.samplerate = self.spotify_audio.samplerate
            else:
                _holler("[Engine] Spotify-only mode requested but ProcessLoopback "
                      "support is unavailable on this system.")

        else:  # "all"
            if self.spotify_audio is not None:
                self.spotify_audio.stop()
            if self.capture is not None:
                self.analyzer.samplerate = self.capture.samplerate
                self.capture.set_passthrough(True)
                self._watchdog_running = True
                threading.Thread(target=self._watchdog_loop, daemon=True).start()
            else:
                _holler("[Engine] 'All PC Sound' mode requested but no system "
                      "loopback device is available.")

    def set_audio_source(self, mode: str) -> None:
        """Switch between 'spotify' and 'all' at runtime."""
        if mode not in ("spotify", "all") or mode == self.audio_source:
            return
        self.audio_source = mode
        self.smtc.set_mode(mode)
        if self._started:
            # Tearing down the old path blocks for up to ~3s (thread.join(),
            # on purpose — stops two captures racing on a fast toggle). This
            # gets called straight from the Qt click handler, so doing it
            # inline used to freeze the whole window mid-switch. Runs on a
            # background thread instead so rendering never stalls.
            threading.Thread(
                target=self._switch_audio_path_safely,
                args=(mode,),
                daemon=True,
            ).start()

    def _switch_audio_path_safely(self, mode: str) -> None:
        with self._switch_lock:
            # Skip if a later call already changed the target mode again
            # while we were waiting for the lock (rapid double-toggle).
            if self.audio_source == mode:
                self._start_audio_path()

    def set_audio_source_auto(self, enabled: bool) -> None:
        """
        Toggle automatic switching: 'spotify' while Spotify.exe is running,
        'all' (whole PC) whenever it isn't. Purely process-presence based —
        doesn't care what's actually making sound.
        """
        self.audio_source_auto = enabled
        if enabled and not self._auto_source_running:
            self._auto_source_running = True
            threading.Thread(target=self._auto_source_loop, daemon=True).start()
        elif not enabled:
            self._auto_source_running = False

    def _auto_source_loop(self) -> None:
        POLL_INTERVAL = 2.0
        while self._auto_source_running:
            try:
                spotify_running = bool(_get_spotify_pids())
                desired = "spotify" if spotify_running else "all"
                if desired != self.audio_source:
                    self.set_audio_source(desired)
                    if self.on_audio_source_auto_switch:
                        self.on_audio_source_auto_switch(desired)
            except Exception as exc:
                _holler(f"[Engine] auto audio-source check failed: {exc}")
            time.sleep(POLL_INTERVAL)

    # ── public API ──────────────────────────────────────────────

    def on_track_update(self, callback) -> None:
        """
        callback(title, artist, img_bytes, pos_s, dur_s, is_playing,
                 title_changed, cover_changed)
        """
        self._track_callback = callback

    def get_bands_db(self) -> tuple[np.ndarray, float]:
        bands, db_pct = self.analyzer.get_bands_db()

        # Hard silence gate. The analyzer normalizes each band to its own
        # recent max, so even near-silent hiss eventually looks "full" —
        # this forces bars to zero when there's genuinely nothing playing.
        if db_pct < self._min_loopback_db:
            return np.zeros_like(bands), 0.0

        if self.audio_source == "all":
            # Whole-PC mode: show whatever is actually coming out of the
            # speakers, no dependency on Spotify/SMTC at all.
            return bands, db_pct

        # "spotify" mode: only ever show real Spotify-process audio.
        # No fallback — if ProcessLoopback isn't active, bars stay silent.
        if self.spotify_audio is not None and self.spotify_audio.using_app_loopback:
            return bands, db_pct

        return np.zeros_like(bands), 0.0

    def set_num_bars(self, n: int) -> None:
        self.analyzer.set_num_bars(n)

    def set_sensitivity(self, key: str) -> None:
        self.analyzer.set_sensitivity(key)

    def scan_for_active_device(self) -> bool:
        if self.capture is not None:
            return self.capture.scan_for_active_device()
        return False

    def set_discord_enabled(self, enabled: bool) -> None:
        self._discord_enabled = enabled
        if enabled:
            def _connect():
                if not self.discord.connect():
                    self._discord_enabled = False
                    return
                time.sleep(1.0)
                self._push_discord()
            threading.Thread(target=_connect, daemon=True).start()
        else:
            threading.Thread(target=self.discord.disconnect, daemon=True).start()

    def set_discord_options(
        self,
        show_title:  bool | None = None,
        show_artist: bool | None = None,
        show_cover:  bool | None = None,
        show_source: bool | None = None,
    ) -> None:
        if show_title  is not None: self._discord_show_title  = show_title
        if show_artist is not None: self._discord_show_artist = show_artist
        if show_cover  is not None: self._discord_show_cover  = show_cover
        if show_source is not None: self._discord_show_source = show_source
        self._push_discord()

    def discord_keepalive(self) -> None:
        if not self._discord_enabled or self.discord.connected:
            return
        def _reconnect():
            if not self.discord.connect():
                return
            self._push_discord()
        threading.Thread(target=_reconnect, daemon=True).start()

    # ── internals ───────────────────────────────────────────────

    def _push_discord(self) -> None:
        if not self._discord_enabled or not self.discord.connected:
            return
        if self._cur_title or self._cur_artist:
            threading.Thread(
                target=self.discord.update,
                args=(
                    self._cur_title, self._cur_artist,
                    self._discord_show_title, self._discord_show_artist,
                    self._discord_show_cover, self._cur_img_bytes,
                ),
                kwargs={
                    "track_changed": False,
                    "source_app_id": self._cur_source_app_id,
                    "show_source":   self._discord_show_source,
                },
                daemon=True,
            ).start()
        else:
            threading.Thread(target=self.discord.update_idle, daemon=True).start()

    def _on_smtc_update(
        self,
        title: str,
        artist: str,
        img_bytes: bytes | None,
        pos_s: float,
        dur_s: float,
        is_playing: bool,
        title_changed: bool,
        cover_changed: bool,
        playing_changed: bool,
        source_app_id: str = "",
    ) -> None:
        self._cur_title         = title
        self._cur_artist        = artist
        self._cur_img_bytes     = img_bytes
        self._cur_source_app_id = source_app_id

        if self._discord_enabled and self.discord.connected:
            if cover_changed and self._discord_show_cover and img_bytes:
                # Start uploading the new cover right away, in parallel with
                # whatever push happens below — instead of only starting the
                # upload after the (rate-limited) "no cover yet" push has
                # already gone out. See DiscordRPC.prefetch_cover().
                self.discord.prefetch_cover(img_bytes)

            if title_changed:
                if title or artist:
                    threading.Thread(
                        target=self.discord.update,
                        args=(title, artist,
                              self._discord_show_title, self._discord_show_artist,
                              self._discord_show_cover, img_bytes),
                        kwargs={
                            "track_changed": True,
                            "source_app_id": source_app_id,
                            "show_source":   self._discord_show_source,
                        },
                        daemon=True,
                    ).start()
                else:
                    threading.Thread(target=self.discord.update_idle, daemon=True).start()
            elif playing_changed:
                if is_playing and (title or artist):
                    threading.Thread(
                        target=self.discord.update,
                        args=(title, artist,
                              self._discord_show_title, self._discord_show_artist,
                              self._discord_show_cover, img_bytes),
                        kwargs={
                            "source_app_id": source_app_id,
                            "show_source":   self._discord_show_source,
                        },
                        daemon=True,
                    ).start()
                else:
                    threading.Thread(target=self.discord.update_idle, daemon=True).start()

        if self._track_callback:
            self._track_callback(
                title, artist, img_bytes,
                pos_s, dur_s, is_playing,
                title_changed, cover_changed,
            )

    def _watchdog_loop(self) -> None:
        """
        Only runs in "all" (whole-PC) mode. Pure device-health watchdog:
        if the loopback stream goes quiet for a while, try rescanning for
        the actually-active output device. No SMTC-based muting/gating —
        in "all" mode the visualizer always reflects whatever is playing.
        """
        NO_SIGNAL_TIMEOUT = 4.0
        SCAN_COOLDOWN     = 20.0

        no_signal_since = None
        last_scan       = 0.0

        while self._watchdog_running:
            time.sleep(1.5)
            if not self._watchdog_running:
                return

            _, db = self.analyzer.get_bands_db()
            now   = time.time()

            if db > self._min_loopback_db:
                no_signal_since = None
                continue

            if no_signal_since is None:
                no_signal_since = now
            if (now - no_signal_since) >= NO_SIGNAL_TIMEOUT and (now - last_scan) >= SCAN_COOLDOWN:
                _murmur("[watchdog] no signal — scanning devices…")
                if self.capture is not None:
                    found = self.capture.scan_for_active_device()
                    _murmur("[watchdog] " + ("device found." if found else "nothing found."))
                    if found:
                        no_signal_since = None
                last_scan = time.time()