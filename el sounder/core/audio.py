"""
core/audio.py  –  el sounder

The ear of the app. Captures whatever is coming out of the speakers
(WASAPI loopback), keeps a small rolling buffer of it, and turns that
into per-band loudness values the UI can draw as bars.
"""

from __future__ import annotations

import math
import os
import threading
import time

import numpy as np

_LOUD = os.environ.get("EL_SOUNDER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

def _murmur(msg: str) -> None:
    """Print only when EL_SOUNDER_DEBUG is on — the app talking to itself quietly."""
    if _LOUD:
        print(msg)

def _holler(msg: str) -> None:
    """Print always — something worth knowing about no matter what."""
    print(msg)


try:
    import pyaudiowpatch as pyaudio
    _PYAUDIO_OK = True
except ImportError:
    pyaudio = None          # type: ignore
    _PYAUDIO_OK = False

BLOCK_SIZE           = 2048
DEVICE_POLL_INTERVAL = 1.5

# How many seconds of audio one FFT window covers. Both audio paths derive
# their sample count from this, so a 192kHz Realtek loopback device and a
# 44.1kHz one still get analyzed over the same real-world time span — a
# fixed sample count alone would make the fast device look "tighter" and
# react differently than the slow one.
ANALYSIS_WINDOW_S    = BLOCK_SIZE / 44100.0

# The shared "what's playing right now" buffer. Exactly one audio path
# (system loopback or the Spotify-only capture) writes into this at a time;
# the analyzer below just reads whatever's currently sitting in it.
on_air      = np.zeros(BLOCK_SIZE, dtype=np.float32)
on_air_lock = threading.Lock()


def _require_pyaudio() -> None:
    if not _PYAUDIO_OK:
        raise RuntimeError("Missing package:\n    pip install PyAudioWPatch")


def print_device_list() -> None:
    _require_pyaudio()
    p = pyaudio.PyAudio()
    print("\n--- Audio Devices ---")
    for i in range(p.get_device_count()):
        d    = p.get_device_info_by_index(i)
        loop = " [LOOPBACK]" if d.get("isLoopbackDevice") else ""
        print(
            f"[{i}] {d['name']}{loop}  "
            f"(in:{d['maxInputChannels']}, out:{d['maxOutputChannels']}, "
            f"sr:{int(d['defaultSampleRate'])})"
        )
    print()
    p.terminate()


def find_loopback_device(p, name_filter=None):
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        return None
    if name_filter is None and hasattr(p, "get_default_wasapi_loopback"):
        try:
            return p.get_default_wasapi_loopback()
        except OSError:
            pass
    default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
    target_name      = name_filter or default_speakers["name"]
    for i in range(p.get_device_count()):
        device = p.get_device_info_by_index(i)
        if device.get("isLoopbackDevice") and target_name in device["name"]:
            return device
    return None


def get_default_output_name(p) -> str | None:
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        dev         = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        return dev["name"]
    except Exception:
        return None


class AudioCapture:
    """
    WASAPI loopback stream → on_air ring buffer.
    Hot-switches when the default output device changes.
    """

    def __init__(self, device_index: int | None = None):
        _require_pyaudio()
        self.p                 = pyaudio.PyAudio()
        self.device            = None
        self._explicit         = device_index is not None
        self._stream_lock      = threading.Lock()
        self._closed           = False
        self._last_out_name    = None
        self._scan_in_progress = False
        # False = stream keeps running (device-watch/hot-switch stays alive)
        # but stops writing into on_air — used so we don't race with the
        # Spotify-only capture path while "Only Spotify" mode is active.
        self._passthrough      = True

        if device_index is not None:
            self.device = self.p.get_device_info_by_index(device_index)
        else:
            self.device = find_loopback_device(self.p)

        if self.device is None:
            self.p.terminate()
            raise RuntimeError(
                "No WASAPI loopback device found.\n"
                "Make sure a playback device (speakers/headset) is connected\n"
                "and that WASAPI loopback is supported."
            )

        _murmur(f"[capture] {'LOOPBACK' if self.device.get('isLoopbackDevice') else 'input'}: "
                f"{self.device['name']} ({int(self.device['defaultSampleRate'])} Hz)")

        if not self._explicit:
            self._last_out_name = get_default_output_name(self.p)

        self._open_stream(self.device)

        if not self._explicit:
            threading.Thread(target=self._watch_default_device, daemon=True).start()

    def _open_stream(self, device) -> None:
        self.samplerate = int(device["defaultSampleRate"])
        self.channels   = min(2, device["maxInputChannels"]) or 1
        # Callback chunks are kept small (~10ms) purely so on_air updates
        # often. The real analysis window (ANALYSIS_WINDOW_S) is built as a
        # sliding accumulator below instead — the same trick the Spotify
        # path uses. Feeding one big non-overlapping chunk straight in here
        # used to make "All PC Sound" update on_air only every ~46ms, which
        # felt noticeably slower/choppier than "Only Spotify" even though
        # the FFT math was identical.
        self._block_frames  = max(128, int(round(0.010 * self.samplerate)))
        self._window_target = max(256, int(round(ANALYSIS_WINDOW_S * self.samplerate)))
        self._accum         = np.zeros(0, dtype=np.float32)
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.samplerate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=self._block_frames,
            stream_callback=self._callback,
        )
        self.stream.start_stream()

    def _callback(self, in_data, frame_count, time_info, status):
        global on_air
        if not self._passthrough:
            return (None, pyaudio.paContinue)
        samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1)
        self._accum = np.concatenate([self._accum, samples])
        max_keep = self._window_target * 4  # bound memory if reads ever pile up
        if len(self._accum) > max_keep:
            self._accum = self._accum[-max_keep:]
        if len(self._accum) >= self._window_target:
            chunk = self._accum[-self._window_target:].astype(np.float32, copy=True)
            with on_air_lock:
                on_air = chunk
        return (None, pyaudio.paContinue)

    def set_passthrough(self, enabled: bool) -> None:
        """Turn writing into on_air on/off without tearing the stream down."""
        self._passthrough = enabled

    def _watch_default_device(self) -> None:
        while not self._closed:
            time.sleep(DEVICE_POLL_INTERVAL)
            if self._closed:
                return
            try:
                current_name = get_default_output_name(self.p)
            except Exception as ex:
                _murmur(f"[capture] device poll failed: {ex}")
                continue
            if current_name is None or current_name == self._last_out_name:
                continue
            if self._scan_in_progress:
                continue

            _murmur(f"[capture] output changed: {self._last_out_name!r} → {current_name!r}")
            time.sleep(1.5)

            new_device = None
            for attempt in range(6):
                # PyAudio reinit must happen inside _stream_lock so the
                # callback never fires against an already-terminated instance.
                with self._stream_lock:
                    try:
                        new_p = pyaudio.PyAudio()
                        self.p.terminate()
                        self.p = new_p
                    except Exception as ex:
                        _murmur(f"[capture] PyAudio reinit failed (attempt {attempt}): {ex}")

                try:
                    new_device = find_loopback_device(self.p, name_filter=current_name)
                except Exception as ex:
                    _murmur(f"[capture] loopback lookup failed (attempt {attempt}): {ex}")

                if new_device is not None:
                    _murmur(f"[capture] loopback found after {attempt + 1} attempt(s)")
                    break

                wait = 0.5 * (attempt + 1)
                _murmur(f"[capture] not yet visible, retry in {wait:.1f}s…")
                time.sleep(wait)

            if new_device is None:
                _holler(f"[capture] no loopback for '{current_name}'; keeping old stream.")
                self._last_out_name = current_name
                continue

            switched_ok = False
            with self._stream_lock:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception as ex:
                    _murmur(f"[capture] error closing old stream: {ex}")
                try:
                    self.device = new_device
                    self._open_stream(new_device)
                    _murmur(f"[capture] now using: {new_device['name']}")
                    switched_ok = True
                except Exception as ex:
                    _holler(f"[capture] failed to open stream on new device: {ex}")
            if switched_ok:
                self._last_out_name = current_name

    def scan_for_active_device(self) -> bool:
        self._scan_in_progress = True
        try:
            return self._scan_impl()
        finally:
            self._scan_in_progress = False

    def _scan_impl(self) -> bool:
        """
        Walk every loopback device and switch to whichever is actually
        carrying sound. Prefers the device matching Windows' default output
        by name, so we never land on a mic-lookalike (those report
        maxOutputChannels == 0 and get skipped in the fallback pass).
        """
        with self._stream_lock:
            try:
                new_p = pyaudio.PyAudio()
                self.p.terminate()
                self.p = new_p
            except Exception:
                pass

        candidates = []
        try:
            for i in range(self.p.get_device_count()):
                d = self.p.get_device_info_by_index(i)
                if d.get("isLoopbackDevice"):
                    candidates.append(d)
        except Exception as ex:
            _holler(f"[scan] device enumeration failed: {ex}")
            return False

        if not candidates:
            _murmur("[scan] no loopback devices found.")
            return False

        # Preferred = matches the current default output name. Everything
        # else falls back to a listen-and-see pass — except pure mic
        # lookalikes (no output channels at all), which get skipped outright.
        win_default = get_default_output_name(self.p)
        preferred: list = []
        fallback:  list = []
        for c in candidates:
            cname = c.get("name", "")
            if win_default and win_default in cname:
                preferred.append(c)
            elif c.get("maxOutputChannels", 0) > 0:
                fallback.append(c)
            else:
                _murmur(f"[scan] skipping '{cname}' — no output channels (likely a mic)")

        ordered = preferred + fallback
        _murmur(f"[scan] {len(ordered)} candidate(s) ({len(preferred)} preferred)")

        for candidate in ordered:
            cname = candidate.get("name", "?")
            _murmur(f"[scan] trying '{cname}'")
            with self._stream_lock:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
                try:
                    self._open_stream(candidate)
                except Exception as ex:
                    _murmur(f"[scan] cannot open '{cname}': {ex}")
                    continue

            # Preferred device: trust the OS, skip the RMS test.
            if candidate in preferred:
                _murmur(f"[scan] '{cname}' matches default output — accepting.")
                self.device = candidate
                self._last_out_name = win_default or cname
                return True

            # Fallback: actually listen for signal. Threshold raised from
            # 1e-4 to 1e-3 so ambient mic bleed can't fake a match.
            time.sleep(1.0)
            with on_air_lock:
                samples = on_air.copy()
            rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size > 0 else 0.0
            if rms > 1e-3:
                _murmur(f"[scan] audio found on '{cname}' (rms={rms:.5f})")
                self.device = candidate
                self._last_out_name = win_default if win_default else cname
                return True
            _murmur(f"[scan] '{cname}' too quiet (rms={rms:.5f}), next…")

        _murmur("[scan] no active loopback device found.")
        return False

    def close(self) -> None:
        self._closed = True
        with self._stream_lock:
            try:
                self.stream.stop_stream()
                self.stream.close()
            finally:
                self.p.terminate()


# ── FFT / band analysis ────────────────────────────────────────────

DB_FLOOR_DBFS    = -60.0
DB_CEIL_DBFS     =   0.0
BAND_FREQ_MIN    =  20.0
BAND_FREQ_MAX    = 16000.0
BAR_NOISE_FLOOR  =  0.04
BAR_GAMMA        =  1.35
BAR_SMOOTH_SIGMA =  0.6
BAR_ATTACK       =  0.75
BAR_DECAY        =  0.28
BPM_FLUX_HISTORY = 100

SENSITIVITY_PRESETS: dict[str, float] = {
    "low":    0.9985,
    "normal": 0.9920,
    "high":   0.9700,
}


def _gaussian_smooth(arr: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return arr
    radius = max(1, int(3 * sigma + 0.5))
    x      = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    return np.convolve(arr, kernel, mode="same")


class AudioAnalyzer:
    """
    Background thread. Reads on_air, produces smoothed frequency
    bands + dB level. Poll via get_bands_db().
    """

    def __init__(
        self,
        num_bars: int,
        samplerate: int,
        sensitivity_key: str = "normal",
        capture: AudioCapture | None = None,
    ):
        self.samplerate     = samplerate
        self.band_max_decay = SENSITIVITY_PRESETS.get(sensitivity_key, 0.9920)
        self._capture       = capture
        self._lock          = threading.Lock()
        self._bands         = np.zeros(num_bars)
        self._db_pct        = 0.0
        self._cur_num_bars  = num_bars
        self._running       = False
        self._thread: threading.Thread | None = None

    def set_capture(self, capture: AudioCapture | None) -> None:
        self._capture = capture

    def set_sensitivity(self, key: str) -> None:
        self.band_max_decay = SENSITIVITY_PRESETS.get(key, 0.9920)

    def set_num_bars(self, n: int) -> None:
        with self._lock:
            self._cur_num_bars = n

    def get_bands_db(self) -> tuple[np.ndarray, float]:
        with self._lock:
            return self._bands.copy(), self._db_pct

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _make_freq_edges(self, nb: int, sr: int) -> np.ndarray:
        return np.logspace(
            np.log10(BAND_FREQ_MIN),
            np.log10(min(BAND_FREQ_MAX, sr / 2)),
            nb + 1,
        )

    def _loop(self) -> None:
        with self._lock:
            nb = self._cur_num_bars
        cur_sr       = self.samplerate
        freq_edges   = self._make_freq_edges(nb, cur_sr)
        local_bands  = np.zeros(nb)
        local_bmax   = np.full(nb, 0.01)
        last_samples = None
        last_change_time = time.time()
        prev_fft     = None
        flux_hist: list[float] = []

        # If on_air stops changing entirely (a capture stream silently
        # stalls after a mode switch, say), don't leave the bars frozen on
        # the last real value forever — fade them out instead.
        STALE_TIMEOUT = 1.2  # seconds

        while self._running:
            time.sleep(0.010)

            if self._capture is not None:
                live_sr = getattr(self._capture, "samplerate", None)
                if live_sr and live_sr != cur_sr:
                    cur_sr          = live_sr
                    self.samplerate = cur_sr
                    freq_edges      = self._make_freq_edges(nb, cur_sr)
                    local_bands     = np.zeros(nb)
                    local_bmax      = np.full(nb, 0.01)
                    last_samples    = None
                    prev_fft        = None

            with self._lock:
                new_nb = self._cur_num_bars
            if new_nb != nb:
                nb          = new_nb
                freq_edges  = self._make_freq_edges(nb, cur_sr)
                local_bands = np.zeros(nb)
                local_bmax  = np.full(nb, 0.01)

            with on_air_lock:
                samples = on_air.copy()

            now = time.time()
            if last_samples is not None and np.array_equal(samples, last_samples):
                if now - last_change_time > STALE_TIMEOUT:
                    # Genuinely stuck (not just a fast poll re-reading the
                    # same still-valid block) — decay toward zero instead
                    # of freezing on the last real value.
                    local_bands = local_bands * (1 - BAR_DECAY)
                    with self._lock:
                        self._bands  = local_bands.copy()
                        self._db_pct = 0.0
                continue
            last_samples      = samples
            last_change_time  = now

            if samples.size == 0:
                continue

            rms = float(np.sqrt(np.mean(samples ** 2)))
            if rms > 1e-10:
                db_val = 20.0 * math.log10(rms)
                db_val = max(DB_FLOOR_DBFS, min(DB_CEIL_DBFS, db_val))
                db_pct = (db_val - DB_FLOOR_DBFS) / (DB_CEIL_DBFS - DB_FLOOR_DBFS) * 100.0
            else:
                db_pct = 0.0

            windowed = samples * np.hanning(len(samples))
            fft      = np.abs(np.fft.rfft(windowed))
            freqs    = np.fft.rfftfreq(len(samples), d=1.0 / cur_sr)

            if prev_fft is not None and len(fft) == len(prev_fft):
                flux = float(np.sum(np.maximum(0.0, fft - prev_fft)))
            else:
                flux = 0.0
            prev_fft = fft.copy()
            flux_hist.append(flux)
            if len(flux_hist) > BPM_FLUX_HISTORY:
                flux_hist.pop(0)

            if np.max(fft) > 0.001:
                centers  = (freq_edges[:-1] + freq_edges[1:]) / 2.0
                band_idx = np.clip(np.searchsorted(freq_edges, freqs) - 1, 0, nb - 1)
                sums     = np.bincount(band_idx, weights=fft, minlength=nb)
                counts   = np.bincount(band_idx, minlength=nb)
                averaged = np.divide(sums, counts, out=np.zeros(nb), where=counts > 0)
                interp   = np.interp(centers, freqs, fft)
                raw      = np.where(counts > 0, averaged, interp)

                active     = raw > (BAR_NOISE_FLOOR * 0.5)
                local_bmax = np.where(
                    active,
                    np.maximum(local_bmax * self.band_max_decay, raw),
                    np.maximum(local_bmax * 0.9999, 0.01),
                )
                new_bands = np.clip(raw / local_bmax, 0.0, 1.0)
                new_bands = np.where(new_bands < BAR_NOISE_FLOOR, 0.0, new_bands)
                new_bands = new_bands ** BAR_GAMMA
            else:
                new_bands = np.zeros(nb)

            if BAR_SMOOTH_SIGMA > 0:
                new_bands = _gaussian_smooth(new_bands, BAR_SMOOTH_SIGMA)

            rising      = new_bands > local_bands
            local_bands = np.where(
                rising,
                local_bands * (1 - BAR_ATTACK) + new_bands * BAR_ATTACK,
                local_bands * (1 - BAR_DECAY)  + new_bands * BAR_DECAY,
            )

            with self._lock:
                self._bands  = local_bands.copy()
                self._db_pct = db_pct
