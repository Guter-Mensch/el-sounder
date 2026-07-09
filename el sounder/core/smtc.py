"""
core/smtc.py  –  el sounder

Asks Windows once a second "what's playing right now?" via the system
media-session API (SMTC), and fires on_update() when something changes.

"spotify" mode: only the real Spotify desktop app counts — browsers and
everything else get filtered out by app ID.
"all" mode: any app's now-playing info counts, except known call/chat
apps (Discord, Teams, Zoom, OBS, ...) — their "media session" is a call,
not music, so they're always excluded even here.

Callback signature:
    on_update(title, artist, img_bytes, pos_s, dur_s, is_playing,
              title_changed, cover_changed, playing_changed, source_app_id)
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

_LOUD = os.environ.get("EL_SOUNDER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

def _murmur(msg: str) -> None:
    if _LOUD:
        print(msg)

def _holler(msg: str) -> None:
    print(msg)


try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
    )
    from winsdk.windows.storage.streams import (
        Buffer as _WinBuffer,
        InputStreamOptions as _ISO,
    )
    _SMTC = True
except Exception:
    _SMTC = False

SPOTIFY_TELLS:  tuple[str, ...] = ("spotify",)
PARTY_CRASHERS: tuple[str, ...] = (
    "chrome", "firefox", "msedge", "opera", "brave", "vivaldi",
    "discord", "discordcanary", "discordptb", "com.squirrel.discord",
    "webcord", "twitch", "teams", "zoom", "obs", "slack",
)

_smtc_last_session_ids: list[str] = []


def _smtc_app_id(session) -> str:
    try:
        return (session.source_app_user_model_id or "").lower()
    except Exception:
        return ""


def _smtc_is_spotify(session) -> bool:
    aid = _smtc_app_id(session)
    if not any(h in aid for h in SPOTIFY_TELLS):
        return False
    if any(r in aid for r in PARTY_CRASHERS):
        return False
    return True


def _smtc_is_call_app(session) -> bool:
    """Apps whose 'media session' is really a call/chat, not music — skip
    these even in 'all' mode so the display doesn't flash Discord/Teams/
    Zoom call metadata instead of actual music."""
    aid = _smtc_app_id(session)
    return any(r in aid for r in PARTY_CRASHERS)


def _smtc_is_playing(session) -> bool:
    try:
        pb = session.get_playback_info()
        return pb is not None and int(pb.playback_status) == 4
    except Exception:
        return False


def _smtc_pick_session(sessions_mgr, mode: str = "spotify"):
    global _smtc_last_session_ids
    session_list: list = []
    try:
        raw          = sessions_mgr.get_sessions()
        count        = raw.size
        session_list = [raw.get_at(i) for i in range(count)]
        current_ids  = [_smtc_app_id(s) or "?" for s in session_list]
        if current_ids != _smtc_last_session_ids:
            _smtc_last_session_ids = current_ids
            _murmur(f"[smtc] {count} session(s): " + ", ".join(current_ids))
    except Exception as exc:
        _murmur(f"[smtc] get_sessions() failed ({exc})")
        try:
            cur = sessions_mgr.get_current_session()
        except Exception:
            return None
        if mode == "all":
            return cur if not _smtc_is_call_app(cur) else None
        return cur if _smtc_is_spotify(cur) else None

    if mode == "all":
        # "All PC Sound": show whatever app is actually making sound —
        # any media session, not just Spotify — as long as it's not one
        # of the call/chat apps in the reject list.
        candidates = [s for s in session_list if not _smtc_is_call_app(s)]
    else:
        candidates = [s for s in session_list if _smtc_is_spotify(s)]

    if not candidates:
        return None
    candidates.sort(key=lambda s: (0 if _smtc_is_playing(s) else 1))
    return candidates[0]


class SMTCMonitor:

    def __init__(self, on_update=None, poll_interval: float = 1.0, mode: str = "spotify"):
        self.available      = _SMTC
        self._on_update     = on_update
        self._poll_interval = poll_interval
        self._running       = False
        self._thread: threading.Thread | None = None

        # "spotify" = only ever show Spotify's own metadata.
        # "all"     = show whatever app's media session is actually active,
        #             mirroring the "All PC Sound" audio mode.
        self._mode = mode if mode in ("spotify", "all") else "spotify"

        self._last_title      = ""
        self._last_artist     = ""
        self._last_cover_hash = 0
        # None so the very first poll always fires playing_changed correctly
        self._last_is_playing: bool | None = None
        self._last_source_app_id: str = ""

    def set_mode(self, mode: str) -> None:
        """Switch between 'spotify' and 'all' session-picking at runtime."""
        if mode in ("spotify", "all"):
            self._mode = mode

    def start(self) -> None:
        if not self.available or self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        loop = asyncio.new_event_loop()
        while self._running:
            try:
                title, artist, img_bytes, pos_s, dur_s, is_playing, source_app_id = (
                    loop.run_until_complete(self._fetch())
                )
                new_hash        = hash(img_bytes) if img_bytes else 0
                title_changed   = title != self._last_title or artist != self._last_artist
                cover_changed   = new_hash != self._last_cover_hash
                playing_changed = is_playing != self._last_is_playing

                if title_changed or cover_changed:
                    _murmur(f"[smtc] '{title}' / '{artist}'  cover={'yes' if img_bytes else 'no'} "
                         f"source={source_app_id or '?'}")

                self._last_title         = title
                self._last_artist        = artist
                self._last_cover_hash    = new_hash
                self._last_is_playing    = is_playing
                self._last_source_app_id = source_app_id

                if self._on_update:
                    self._on_update(
                        title=title,
                        artist=artist,
                        img_bytes=img_bytes,
                        pos_s=pos_s,
                        dur_s=dur_s,
                        is_playing=is_playing,
                        title_changed=title_changed,
                        cover_changed=cover_changed,
                        playing_changed=playing_changed,
                        source_app_id=source_app_id,
                    )
            except Exception as exc:
                _holler(f"[smtc] loop error: {exc}")
            time.sleep(self._poll_interval)

    async def _fetch(self):
        sessions = await _MediaManager.request_async()
        current  = _smtc_pick_session(sessions, self._mode)
        if current is None:
            return "", "", None, 0.0, 0.0, False, ""

        source_app_id = _smtc_app_id(current)

        info = await current.try_get_media_properties_async()
        if info is None:
            return "", "", None, 0.0, 0.0, False, source_app_id

        img_bytes = None
        try:
            if info.thumbnail:
                stream = await info.thumbnail.open_read_async()
                size   = stream.size

                if size and size > 0:
                    buf = _WinBuffer(size)
                    await stream.read_async(buf, size, _ISO.NONE)
                    raw = bytes(buf)
                else:
                    CHUNK  = 65536
                    chunks: list[bytes] = []
                    while True:
                        chunk_buf = _WinBuffer(CHUNK)
                        n = await stream.read_async(chunk_buf, CHUNK, _ISO.NONE)
                        if n == 0:
                            break
                        chunks.append(bytes(memoryview(chunk_buf)[:n]))
                    raw = b"".join(chunks)

                if len(raw) > 64:
                    img_bytes = raw
        except Exception:
            pass

        pos_s = 0.0
        dur_s = 0.0
        is_playing = True
        try:
            tl = current.get_timeline_properties()
            if tl is not None:
                def _ts(ts):
                    if ts is None:
                        return 0.0
                    if hasattr(ts, "total_seconds"):
                        return float(ts.total_seconds())
                    return float(ts) / 10_000_000
                pos_s = _ts(tl.position)
                dur_s = _ts(tl.end_time)
        except Exception:
            pass

        try:
            pb = current.get_playback_info()
            is_playing = pb is not None and int(pb.playback_status) == 4
        except Exception:
            pass

        return info.title or "", info.artist or "", img_bytes, pos_s, dur_s, is_playing, source_app_id