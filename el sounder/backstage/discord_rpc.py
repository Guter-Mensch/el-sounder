"""
backstage/discord_rpc.py  –  el sounder

Puts the current song on your Discord profile. Handles its own
reconnects, and uploads cover art on a dedicated worker thread so a
slow upload never blocks anything — if the track changes mid-upload,
that result just gets thrown away instead of appearing late and wrong.
"""

from __future__ import annotations

import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request

_LOUD = os.environ.get("EL_SOUNDER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _murmur(msg: str) -> None:
    if _LOUD:
        print(msg)


def _holler(msg: str) -> None:
    print(msg)


try:
    from pypresence import Presence as _Presence
    try:
        from pypresence import ActivityType as _ActivityType
        _LISTENING_TYPE = _ActivityType.LISTENING
    except (ImportError, AttributeError):
        # Older pypresence versions use the raw integer 2
        _LISTENING_TYPE = 2
    _PYPRESENCE = True
except ImportError:
    _PYPRESENCE = False
    _LISTENING_TYPE = 2

DISCORD_CLIENT_ID = "1518252235555999916"

# Cache: hash(bytes) → URL string
_cover_url_cache: dict[int, str] = {}
# In-progress uploads: hash(bytes) → threading.Event (set when done)
_cover_upload_pending: dict[int, threading.Event] = {}
_cover_cache_lock = threading.Lock()


try:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt as _Qt
    from PyQt6.QtGui  import QImage
    _QT_IMG_OK = True
except Exception:
    _QT_IMG_OK = False


def _shrink_image_bytes(image_bytes: bytes, max_bytes: int) -> bytes | None:
    """
    Re-encode + downscale an oversized cover until it fits under max_bytes.
    Used instead of just discarding covers over the catbox.moe size limit —
    non-Spotify apps (browsers, other players) often deliver much larger
    thumbnails than Spotify does, which was the main reason covers so often
    never showed up at all outside of Spotify.
    Returns None if PyQt6 image decoding isn't available or the image can't
    be decoded.
    """
    if not _QT_IMG_OK:
        return None
    img = QImage.fromData(image_bytes)
    if img.isNull():
        return None

    quality = 85
    scale   = 1.0
    best: bytes | None = None
    for _ in range(6):
        w = max(64, int(img.width()  * scale))
        h = max(64, int(img.height() * scale))
        scaled = img.scaled(
            w, h,
            _Qt.AspectRatioMode.KeepAspectRatio,
            _Qt.TransformationMode.SmoothTransformation,
        )
        buf  = QByteArray()
        qbuf = QBuffer(buf)
        qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(qbuf, "JPG", quality)
        qbuf.close()
        data = bytes(buf)
        best = data
        if len(data) <= max_bytes:
            return data
        quality = max(40, quality - 15)
        scale  *= 0.75
    return best  # best-effort smallest attempt, may still be slightly over


def upload_cover_bytes(image_bytes: bytes, timeout: float = 25.0) -> str | None:
    """
    Upload cover art to catbox.moe.
    Thread-safe with automatic deduplication: if an upload for the same hash
    is already in progress, this call waits for it and returns the cached URL.
    Images larger than 2 MB are downscaled/re-encoded to fit instead of being
    skipped outright.
    """
    if not image_bytes:
        return None

    MAX_BYTES = 2 * 1024 * 1024  # 2 MB
    if len(image_bytes) > MAX_BYTES:
        shrunk = _shrink_image_bytes(image_bytes, MAX_BYTES)
        if shrunk:
            _murmur(f"[Discord] Cover shrunk {len(image_bytes)//1024}KB -> {len(shrunk)//1024}KB")
            image_bytes = shrunk
        else:
            _holler(f"[Discord] Cover skipped: {len(image_bytes) // 1024} KB exceeds "
                  f"2 MB limit and could not be downscaled.")
            return None
    cache_key = hash(image_bytes)

    with _cover_cache_lock:
        if cache_key in _cover_url_cache:
            return _cover_url_cache[cache_key]
        if cache_key in _cover_upload_pending:
            event = _cover_upload_pending[cache_key]
        else:
            event = threading.Event()
            _cover_upload_pending[cache_key] = event
            event = None  # we are the first → we do the upload

    if event is not None:
        # Wait for the other upload (max 30 s)
        event.wait(timeout=30.0)
        with _cover_cache_lock:
            return _cover_url_cache.get(cache_key)

    # --- We are the upload owner ---
    done_event = _cover_upload_pending[cache_key]

    # Format detection
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        ext, mime = "png", "image/png"
    elif image_bytes[:3] == b"\xff\xd8\xff":
        ext, mime = "jpg", "image/jpeg"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        ext, mime = "webp", "image/webp"
    elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        ext, mime = "gif", "image/gif"
    else:
        ext, mime = "jpg", "image/jpeg"

    boundary = b"----ElsounderBoundary"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="reqtype"\r\n\r\n'
        b"fileupload\r\n"
        b"--" + boundary + b"\r\n"
        + (
            b'Content-Disposition: form-data; name="fileToUpload"; filename="cover.'
            + ext.encode()
            + b'"\r\n'
        )
        + b"Content-Type: " + mime.encode() + b"\r\n\r\n"
        + image_bytes
        + b"\r\n--" + boundary + b"--\r\n"
    )
    req = urllib.request.Request(
        "https://catbox.moe/user/api.php",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
            "User-Agent": "el-sounder/2.0",
        },
    )

    link = None
    for attempt in range(3):
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                result = resp.read().decode("utf-8", errors="replace").strip()
                if result and result.startswith("https://"):
                    link = result
                    break
                else:
                    _holler(f"[Discord] Upload attempt {attempt + 1}: unexpected response: {result!r}")
        except ssl.SSLError as e:
            _holler(f"[Discord] Upload attempt {attempt + 1} SSL error: {e}. "
                  "Check your system certificates; skipping cover upload.")
            break  # don't retry SSL failures without verification
        except Exception as e:
            _holler(f"[Discord] Upload attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    with _cover_cache_lock:
        if link:
            _cover_url_cache[cache_key] = link
            _murmur(f"[Discord] Cover uploaded: {link}")
        _cover_upload_pending.pop(cache_key, None)

    done_event.set()
    return link


class DiscordRPC:
    MIN_PUSH_INTERVAL = 3.0

    IDLE_DETAILS = "el sounder"
    IDLE_STATE   = "Listening"

    # Alt-text shown when hovering the cover art. Discord's newer profile UI
    # renders large_text as its own line, so mirroring state (=artist) there
    # just duplicated the artist visually. Instead this shows *where* el
    # sounder is actually capturing/processing the audio from right now —
    # derived generically from the SMTC session's app id, so it works the
    # same regardless of which app (Spotify, YouTube, Apple Music, browser,
    # ...) happens to be the source, with no per-source special-casing.
    _SOURCE_LABELS: tuple[tuple[str, str], ...] = (
        ("spotify",       "Spotify"),
        ("applemusic",    "Apple Music"),
        ("apple.music",   "Apple Music"),
        ("music.apple",   "Apple Music"),
        ("youtubemusic",  "YouTube Music"),
        ("youtube",       "YouTube"),
        ("tidal",         "Tidal"),
        ("deezer",        "Deezer"),
        ("soundcloud",    "SoundCloud"),
        ("amazonmusic",   "Amazon Music"),
        ("napster",       "Napster"),
        ("foobar2000",    "foobar2000"),
        ("wmplayer",      "Windows Media Player"),
        ("groove",        "Groove Music"),
        ("vlc",           "VLC"),
        ("chrome",        "Chrome"),
        ("msedge",        "Edge"),
        ("firefox",       "Firefox"),
        ("opera",         "Opera"),
        ("brave",         "Brave"),
        ("vivaldi",       "Vivaldi"),
    )

    @classmethod
    def _friendly_source(cls, source_app_id: str) -> str:
        aid = (source_app_id or "").lower()
        for hint, label in cls._SOURCE_LABELS:
            if hint in aid:
                return label
        return "el sounder"

    def __init__(self, client_id: str = DISCORD_CLIENT_ID):
        self._client_id    = client_id
        self._rpc          = None
        self._connected    = False
        self._lock         = threading.Lock()
        self._start_ts     = int(time.time())

        self._last_title   = ""
        self._last_artist  = ""
        self._last_push_ts = 0.0

        # Reconnect guard
        self._reconnect_lock   = threading.Lock()
        self._reconnect_active = False

        # Single worker queue: all update requests go here, processed in order.
        # This eliminates every race between concurrent update() calls.
        self._work_queue: queue.Queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

        # Sequence counter: incremented on each new track.
        # The worker uses it to discard stale cover results.
        self._track_seq = 0

    # ── Connection ──────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        if not _PYPRESENCE:
            _holler("[Discord] pypresence not installed")
            return False
        with self._lock:
            if self._rpc:
                try:
                    self._rpc.close()
                except Exception:
                    pass
            try:
                self._rpc = _Presence(self._client_id)
                self._rpc.connect()
                self._connected    = True
                self._start_ts     = int(time.time())
                self._last_push_ts = 0.0
                _murmur("[Discord] Connected.")
                return True
            except Exception as e:
                _holler(f"[Discord] Connection failed: {e}")
                self._rpc       = None
                self._connected = False
                return False

    def disconnect(self):
        with self._lock:
            if self._rpc:
                try:
                    self._rpc.clear()
                except Exception:
                    pass
                try:
                    self._rpc.close()
                except Exception:
                    pass
            self._connected = False
            self._rpc       = None
        _murmur("[Discord] Disconnected.")

    def _try_reconnect(self) -> bool:
        with self._reconnect_lock:
            if self._reconnect_active:
                return False
            self._reconnect_active = True
        try:
            return self.connect()
        finally:
            with self._reconnect_lock:
                self._reconnect_active = False

    # ── Public update methods ────────────────────────────────────
    #
    # All calls are posted to _work_queue and handled by a single worker
    # thread.  This means update() never blocks the caller and there are
    # no concurrent pushes racing each other.

    def update(
        self,
        title: str,
        artist: str,
        show_title: bool,
        show_artist: bool,
        show_cover: bool,
        image_bytes: bytes | None = None,
        track_changed: bool = False,
        source_app_id: str = "",
        show_source: bool = True,
    ) -> None:
        self._work_queue.put({
            "kind":          "update",
            "title":         title,
            "artist":        artist,
            "show_title":    show_title,
            "show_artist":   show_artist,
            "show_cover":    show_cover,
            "image_bytes":   image_bytes,
            "track_changed": track_changed,
            "source_app_id": source_app_id,
            "show_source":   show_source,
        })

    def update_idle(self) -> None:
        self._work_queue.put({"kind": "idle"})

    def prefetch_cover(self, image_bytes: bytes | None) -> None:
        """
        Kick off the cover upload immediately in the background, decoupled
        from the rate-limited push pipeline. upload_cover_bytes() already
        dedups by image hash (cache hit / join-in-progress), so calling
        this liberally is cheap — it's a no-op if that cover is already
        cached or already being uploaded.

        Without this, the upload only ever started *after* the "no cover
        yet" placeholder push had already sat through the rate-limit wait,
        serializing wait -> upload -> wait -> push into one long chain.
        Starting the upload here means it overlaps with that first wait
        instead of happening after it — by the time the worker gets to
        push for this track, the cover is often already uploaded.
        """
        if not image_bytes:
            return
        threading.Thread(
            target=upload_cover_bytes,
            args=(image_bytes,),
            daemon=True,
        ).start()

    # ── Single worker ────────────────────────────────────────────

    def _worker(self):
        """
        Processes all update/idle requests sequentially.

        For each job:
          1. Drain the queue — only process the *latest* job.
             This means rapid-fire calls (title_changed + playing_changed)
             collapse into one push, eliminating duplicate cover=no pushes.
          2. If the cover is already cached → push immediately with cover.
          3. If not cached → push cover=no, then upload, then push cover=yes.
             The sequence guard ensures stale cover results are discarded.
        """
        while True:
            try:
                job = self._work_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Drain — keep only the latest job
            while True:
                try:
                    job = self._work_queue.get_nowait()
                except queue.Empty:
                    break

            if job["kind"] == "idle":
                with self._lock:
                    self._track_seq += 1
                if not self._connected and not self._try_reconnect():
                    continue
                self._rpc_push(
                    self.IDLE_DETAILS, self.IDLE_STATE,
                    "app_icon", "el sounder", "", ""
                )
                continue

            # --- kind == "update" ---
            title        = job["title"]
            artist       = job["artist"]
            show_title   = job["show_title"]
            show_artist  = job["show_artist"]
            show_cover   = job["show_cover"]
            image_bytes  = job["image_bytes"]
            track_changed = job["track_changed"]
            show_source   = job.get("show_source", True)
            source_label  = self._friendly_source(job.get("source_app_id", "")) if show_source else "el sounder"

            def _rpc_str(s):
                if not s:
                    return None
                s = s.strip()
                if len(s) < 2 or all(c in "-\u2013\u2014\u00b7\u2022.\u2026_ " for c in s):
                    return None
                return s

            details = _rpc_str(title)  if show_title  else None
            state   = _rpc_str(artist) if show_artist else None

            with self._lock:
                if track_changed:
                    self._track_seq += 1
                ticket = self._track_seq

            if not self._connected and not self._try_reconnect():
                continue

            cache_key = hash(image_bytes) if image_bytes else None
            with _cover_cache_lock:
                cached_url = _cover_url_cache.get(cache_key) if cache_key else None

            if cached_url:
                # Cover already known — single push with art
                self._rpc_push(details, state, cached_url, source_label, title, artist)
            else:
                # Push without cover first, then upload in background
                self._rpc_push(details, state, None, None, title, artist)
                if show_cover and image_bytes:
                    # Upload happens here in the worker thread (blocking).
                    # The rate-limit sleep in _rpc_push already ran, so the
                    # upload time is "free" — no extra delay for the user.
                    url = upload_cover_bytes(image_bytes)

                    # Like a deli counter: compare our ticket to the number
                    # now being served. Mismatch means a newer track arrived
                    # while we were uploading — throw this result away.
                    with self._lock:
                        now_serving = self._track_seq
                    if ticket != now_serving:
                        _murmur(f"[Discord] Cover stale after upload (ticket {ticket} != {now_serving}), discarding.")
                        continue

                    if url:
                        if not self._connected and not self._try_reconnect():
                            continue
                        self._rpc_push(details, state, url, source_label, title, artist)
                    else:
                        _murmur("[Discord] Cover upload failed, staying without cover.")

    # ── Internal RPC push ────────────────────────────────────────

    def _rpc_push(self, details, state, large_image, large_text, title, artist):
        # Rate-limit: enforce MIN_PUSH_INTERVAL between Discord API calls.
        # Called only from the single worker thread, so no concurrency here.
        now  = time.time()
        wait = self.MIN_PUSH_INTERVAL - (now - self._last_push_ts)
        if wait > 0:
            time.sleep(wait)

        with self._lock:
            if not self._connected or not self._rpc:
                return False
            try:
                kwargs: dict = {
                    "details":       details,
                    "state":         state,
                    "start":         self._start_ts,
                    "activity_type": _LISTENING_TYPE,
                }
                if large_image:
                    kwargs["large_image"] = large_image
                    kwargs["large_text"]  = large_text
                self._rpc.update(**kwargs)
                self._last_title   = title
                self._last_artist  = artist
                self._last_push_ts = time.time()
                _murmur(f"[Discord] Pushed: {details!r} / {state!r} cover={'yes' if large_image else 'no'}")
                return True
            except Exception as e:
                _holler(f"[Discord] RPC push failed: {e}")
                if any(k in str(e).lower() for k in ("pipe", "connect", "broken", "closed", "winapi")):
                    self._connected = False
                return False