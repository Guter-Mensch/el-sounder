"""
core/audio_app_loopback.py  –  el sounder
==========================================
Spotify-only audio capture via Windows WASAPI ProcessLoopback API.

Uses IAudioClient + AUDIOCLIENT_ACTIVATION_PARAMS with
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE (Win10 Build 20348+)
via ctypes — no external dependency beyond what's already in requirements.txt.

Falls back gracefully: sets self.using_app_loopback = False so CoreEngine
can silence the bands instead.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import sys
import threading
import time

import numpy as np

_LOUD = os.environ.get("EL_SOUNDER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _murmur(msg: str) -> None:
    if _LOUD:
        print(msg)


def _holler(msg: str) -> None:
    print(msg)


# ── Shared audio buffer (same as core/audio.py) ─────────────────
import core.audio as _ca


# ── Win32 / COM constants ────────────────────────────────────────
COINIT_MULTITHREADED        = 0x0
CLSCTX_ALL                  = 0x17
AUDCLNT_STREAMFLAGS_LOOPBACK            = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK       = 0x00040000
AUDCLNT_SHAREMODE_SHARED    = 0
AUDCLNT_BUFFERFLAGS_SILENT  = 0x2
WAVE_FORMAT_IEEE_FLOAT      = 0x0003
WAVE_FORMAT_EXTENSIBLE      = 0xFFFE

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

S_OK    = 0
S_FALSE = 1
AUDCLNT_S_BUFFER_EMPTY = 0x08890001

# GUIDs
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator  = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioClient         = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioClient2        = "{726778CD-F60A-4EDA-82DE-E47610CD78AA}"
IID_IAudioCaptureClient  = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
IID_IActivateAudioInterfaceCompletionHandler = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"

DEVINTERFACE_AUDIO_RENDER = "{E6327CAD-DCEC-4949-AE8A-991E976A79D2}"

# Required for per-PROCESS loopback (AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_
# LOOPBACK). Don't swap this for DEVINTERFACE_AUDIO_RENDER above — that one
# activates fine (hr=S_OK) but silently ignores TargetProcessId, so you'd
# get the whole PC's audio instead of just Spotify's with no error at all.
# (Microsoft's "ApplicationLoopback" sample, audioclient.h docs.)
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"


def _guid(s: str) -> ctypes.Array:
    """Convert a GUID string to GUID bytes for RPC_IF_ID / REFIID."""
    import uuid
    b = uuid.UUID(s).bytes_le
    return (ctypes.c_byte * 16)(*b)


# ── WAVEFORMATEX ─────────────────────────────────────────────────
class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag",      ctypes.c_ushort),
        ("nChannels",       ctypes.c_ushort),
        ("nSamplesPerSec",  ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign",     ctypes.c_ushort),
        ("wBitsPerSample",  ctypes.c_ushort),
        ("cbSize",          ctypes.c_ushort),
    ]


# ── AUDIOCLIENT_ACTIVATION_PARAMS ────────────────────────────────
class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId",    ctypes.c_uint32),
        ("ProcessLoopbackMode", ctypes.c_uint32),
    ]


class _PARAMS_UNION(ctypes.Union):
    _fields_ = [
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_uint32),
        ("_u",             _PARAMS_UNION),
    ]


class PROPVARIANT_BLOB(ctypes.Structure):
    """Minimal PROPVARIANT with blob data pointer."""
    _fields_ = [
        ("vt",       ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("cbSize",   ctypes.c_ulong),
        ("pBlobData", ctypes.c_void_p),
    ]


# ────────────────────────────────────────────────────────────────
def _get_spotify_pids() -> list[int]:
    """Return all Spotify.exe PIDs (may be multiple windows/helpers)."""
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # tasklist emits console-OEM-codepage bytes, not the system ANSI
        # codepage — decoding as text=True (cp1252 on most Windows setups)
        # blows up on non-ASCII PID separators/usernames. Bytes + a
        # tolerant decode sidesteps that entirely; we only need the PID
        # column anyway, which is always plain ASCII digits.
        stdout = result.stdout.decode("cp850", errors="replace")
        pids = []
        for line in stdout.splitlines():
            parts = line.strip().split('","')
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1].strip('"')))
                except ValueError:
                    pass
        return pids
    except Exception as ex:
        _murmur(f"[AppLoopback] PID lookup failed: {ex}")
        return []


# ────────────────────────────────────────────────────────────────
class _ActivateHandler(ctypes.Structure):
    """
    Minimal IActivateAudioInterfaceCompletionHandler COM object.
    Implemented as a pure-ctypes vtable shim.
    """
    pass


# ── ProcessLoopbackStream ────────────────────────────────────────

class ProcessLoopbackStream:
    """
    Opens a WASAPI ProcessLoopback capture stream for one PID.
    Writes PCM float32 mono data into core.audio.on_air.

    Usage:
        s = ProcessLoopbackStream(pid)
        ok = s.open()          # returns False if API not supported
        s.start()              # starts background read thread
        s.stop()
    """

    SAMPLE_RATE = 44100
    CHANNELS    = 2
    BLOCK_FRAMES = 2048

    def __init__(self, pid: int) -> None:
        self._pid       = pid
        self._running   = False
        self._thread: threading.Thread | None = None
        self._client    = None   # IAudioClient pointer (ctypes)
        self._client_vt = None
        self._capture_ptr = None
        self._capture_vt  = None
        self.available  = False

    # ── open ────────────────────────────────────────────────────

    def open(self) -> bool:
        """
        Try to activate IAudioClient via ActivateAudioInterfaceAsync
        with AUDIOCLIENT_ACTIVATION_PARAMS for process loopback.
        Returns True on success.

        Requires Windows 10 Build 20348+.
        """
        try:
            return self._open_impl()
        except Exception as ex:
            _murmur(f"[AppLoopback] open() failed: {ex}")
            return False

    def _open_impl(self) -> bool:
        ole32   = ctypes.windll.ole32
        avrt    = ctypes.windll.avrt

        # ActivateAudioInterfaceAsync is in audioclient.h → mmdevapi.dll
        try:
            mmdevapi = ctypes.windll.mmdevapi
        except Exception:
            _murmur("[AppLoopback] mmdevapi not found")
            return False

        # Build activation params
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params._u.ProcessLoopbackParams.TargetProcessId     = self._pid
        params._u.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

        # Wrap in PROPVARIANT (vt = VT_BLOB = 0x41)
        pv = PROPVARIANT_BLOB()
        pv.vt      = 0x41  # VT_BLOB
        pv.cbSize  = ctypes.sizeof(params)
        pv.pBlobData = ctypes.cast(ctypes.byref(params), ctypes.c_void_p)

        # We need an event for the completion handler
        event = ctypes.windll.kernel32.CreateEventW(None, True, False, None)
        if not event:
            _murmur("[AppLoopback] CreateEventW failed")
            return False

        # Use a simple callback shim via ctypes CFUNCTYPE
        HANDLER_PROTO = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

        result_holder = [S_OK, None]

        def _completed(this, activate_result):
            hr    = ctypes.c_long()
            iface = ctypes.c_void_p()
            # IActivateAudioInterfaceAsyncOperation::GetActivateResult
            op = ctypes.cast(activate_result, ctypes.POINTER(ctypes.c_void_p))
            # vtable[3] = GetActivateResult(hr, ppvObj)
            vt = ctypes.cast(op[0], ctypes.POINTER(ctypes.c_void_p))
            get_result = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_void_p),
            )(vt[3])
            get_result(activate_result, ctypes.byref(hr), ctypes.byref(iface))
            result_holder[0] = hr.value
            result_holder[1] = iface.value
            ctypes.windll.kernel32.SetEvent(event)
            return 0  # S_OK

        handler_func = HANDLER_PROTO(_completed)

        # Build minimal COM vtable for IActivateAudioInterfaceCompletionHandler
        # Methods: QueryInterface, AddRef, Release, ActivateCompleted
        QI_PROTO      = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        ULONG_PROTO   = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        COMPL_PROTO   = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)

        def _qi(this, riid, ppv):
            ppv[0] = this
            return 0
        def _addref(this):  return 1
        def _release(this): return 1

        qi_func       = QI_PROTO(_qi)
        addref_func   = ULONG_PROTO(_addref)
        release_func  = ULONG_PROTO(_release)
        compl_func    = COMPL_PROTO(_completed)

        VTableType  = ctypes.c_void_p * 4
        vtable      = VTableType(
            ctypes.cast(qi_func,      ctypes.c_void_p),
            ctypes.cast(addref_func,  ctypes.c_void_p),
            ctypes.cast(release_func, ctypes.c_void_p),
            ctypes.cast(compl_func,   ctypes.c_void_p),
        )
        vtable_ptr  = ctypes.cast(vtable, ctypes.c_void_p)

        ObjType = ctypes.c_void_p * 1
        obj     = ObjType(ctypes.cast(ctypes.byref(vtable), ctypes.c_void_p).value)
        handler_ptr = ctypes.cast(obj, ctypes.c_void_p)

        # For AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, ActivateAudioInterfaceAsync
        # expects the special "VAD\Process_Loopback" virtual-device string, NOT the
        # generic audio-render device-interface category GUID. Using the render GUID
        # here used to silently activate whole-system loopback instead of a
        # per-process stream (see comment on VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
        # above) — that was the cause of "Only Spotify" mode still picking up
        # every app's audio.
        device_id_w = ctypes.c_wchar_p(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK)

        # The virtual "VAD\Process_Loopback" endpoint only implements the
        # base IAudioClient interface, not IAudioClient2 (unlike a normal
        # physical device activated through DEVINTERFACE_AUDIO_RENDER,
        # which does support IAudioClient2). Requesting IAudioClient2 here
        # made activation fail outright (E_NOINTERFACE) once the device id
        # above was corrected to the virtual endpoint — which is why the
        # bars went completely silent in "Only Spotify" mode instead of
        # just being unfiltered. None of the methods used below
        # (Initialize/Start/Stop/GetService) need IAudioClient2 anyway —
        # they're all base IAudioClient vtable slots.
        iid_bytes = _guid(IID_IAudioClient)
        iid_ptr   = ctypes.cast(iid_bytes, ctypes.c_void_p)

        # ActivateAudioInterfaceAsync(deviceId, riid, activationParams, handler, &asyncOp)
        try:
            ActivateAudioInterfaceAsync = mmdevapi.ActivateAudioInterfaceAsync
        except AttributeError:
            _murmur("[AppLoopback] ActivateAudioInterfaceAsync not found (Win < 20348)")
            ctypes.windll.kernel32.CloseHandle(event)
            return False

        async_op = ctypes.c_void_p()
        hr = ActivateAudioInterfaceAsync(
            device_id_w,
            iid_ptr,
            ctypes.byref(pv),
            handler_ptr,
            ctypes.byref(async_op),
        )
        if hr != S_OK:
            _murmur(f"[AppLoopback] ActivateAudioInterfaceAsync hr=0x{hr & 0xFFFFFFFF:08X}")
            ctypes.windll.kernel32.CloseHandle(event)
            return False

        # Wait for completion (up to 5 s)
        wait_result = ctypes.windll.kernel32.WaitForSingleObject(event, 5000)
        ctypes.windll.kernel32.CloseHandle(event)

        if wait_result != 0:  # WAIT_OBJECT_0 = 0
            _murmur("[AppLoopback] Activation timed out")
            return False

        if result_holder[0] != S_OK or not result_holder[1]:
            _murmur(f"[AppLoopback] Activation result hr=0x{result_holder[0] & 0xFFFFFFFF:08X}")
            return False

        # We now have an IAudioClient2 pointer
        client_ptr = result_holder[1]

        # Initialize: shared mode loopback, 10ms buffer
        wfx = WAVEFORMATEX()
        wfx.wFormatTag      = WAVE_FORMAT_IEEE_FLOAT
        wfx.nChannels       = self.CHANNELS
        wfx.nSamplesPerSec  = self.SAMPLE_RATE
        wfx.wBitsPerSample  = 32
        wfx.nBlockAlign     = wfx.nChannels * (wfx.wBitsPerSample // 8)
        wfx.nAvgBytesPerSec = wfx.nSamplesPerSec * wfx.nBlockAlign
        wfx.cbSize          = 0

        HNSTIME_100NS = 10_000_000  # 1 second in 100-ns units
        BUFFER_HNSTIME = HNSTIME_100NS // 100  # 10 ms

        client_vt = ctypes.cast(
            ctypes.cast(client_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p)
        )

        # IAudioClient::Initialize (vtable index 3)
        Initialize = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,   # this
            ctypes.c_int,      # ShareMode
            ctypes.c_uint32,   # StreamFlags
            ctypes.c_longlong, # hnsBufferDuration
            ctypes.c_longlong, # hnsPeriodicity
            ctypes.c_void_p,   # *pFormat
            ctypes.c_void_p,   # AudioSessionGuid
        )(client_vt[3])

        # NOTE: We deliberately do NOT set AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        # here. The read loop below is purely poll-based (GetNextPacketSize +
        # sleep) and never calls IAudioClient::SetEventHandle. Requesting
        # event-driven mode without ever registering an event handle is an
        # invalid/undefined combination per the WASAPI docs — it can appear
        # to work on a stream's *first* activation and then silently stop
        # delivering packets on a subsequent re-activation of the same
        # session (e.g. after toggling "Only Spotify" -> "All PC Sound" ->
        # "Only Spotify" again), which freezes the bars while everything
        # else (SMTC/Discord) keeps working fine.
        hr = Initialize(
            client_ptr,
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            BUFFER_HNSTIME,
            0,
            ctypes.byref(wfx),
            None,
        )
        if hr != S_OK:
            _murmur(f"[AppLoopback] IAudioClient::Initialize hr=0x{hr & 0xFFFFFFFF:08X}")
            return False

        # Get IAudioCaptureClient (vtable index 14: GetService)
        iid_capture = _guid(IID_IAudioCaptureClient)
        capture_ptr = ctypes.c_void_p()
        GetService = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(client_vt[14])

        hr = GetService(client_ptr, ctypes.cast(iid_capture, ctypes.c_void_p), ctypes.byref(capture_ptr))
        if hr != S_OK:
            _murmur(f"[AppLoopback] GetService(CaptureClient) hr=0x{hr & 0xFFFFFFFF:08X}")
            return False

        self._client         = client_ptr
        self._client_vt      = client_vt
        self._capture_ptr    = capture_ptr
        self._capture_vt     = ctypes.cast(
            ctypes.cast(capture_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p)
        )
        self._wfx            = wfx
        self.available       = True
        _murmur(f"[AppLoopback] IAudioClient activated for PID {self._pid}")
        return True

    # ── start / stop ────────────────────────────────────────────

    def start(self) -> None:
        if not self.available:
            return
        # IAudioClient::Start (vtable index 10)
        Start = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(self._client_vt[10])
        Start(self._client)
        self._running = True
        self._thread  = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._client and self._client_vt:
            try:
                Stop = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(self._client_vt[11])
                Stop(self._client)
            except Exception:
                pass

        # Let the read thread notice _running=False and exit before we
        # release the COM objects it's using.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

        # Release the COM objects we were handed. Without this, every
        # stop()/reopen cycle leaks a WASAPI session against the target
        # process — and that leftover session can silently prevent a
        # later capture attempt for the same PID from ever producing data.
        try:
            if self._capture_ptr and self._capture_vt:
                Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(self._capture_vt[2])
                Release(self._capture_ptr)
        except Exception:
            pass
        try:
            if self._client and self._client_vt:
                Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(self._client_vt[2])
                Release(self._client)
        except Exception:
            pass

        self._client       = None
        self._capture_ptr  = None
        self.available     = False

    # ── read loop ───────────────────────────────────────────────

    def _read_loop(self) -> None:
        """Read PCM from IAudioCaptureClient, write to core.audio.on_air."""
        # IAudioCaptureClient vtable:
        # 0=QI, 1=AddRef, 2=Release
        # 3=GetBuffer, 4=ReleaseBuffer, 5=GetNextPacketSize
        GetBuffer = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,                       # this
            ctypes.POINTER(ctypes.c_void_p),        # ppData
            ctypes.POINTER(ctypes.c_uint32),        # pNumFramesAvailable
            ctypes.POINTER(ctypes.c_uint32),        # pdwFlags
            ctypes.POINTER(ctypes.c_uint64),        # pu64DevicePosition
            ctypes.POINTER(ctypes.c_uint64),        # pu64QPCPosition
        )(self._capture_vt[3])

        ReleaseBuffer = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,   # this
            ctypes.c_uint32,   # NumFramesRead
        )(self._capture_vt[4])

        GetNextPacketSize = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        )(self._capture_vt[5])

        channels = self._wfx.nChannels
        accum    = np.zeros(0, dtype=np.float32)

        last_packet_time = time.time()
        stall_warned     = False

        while self._running:
            packet_size = ctypes.c_uint32(0)
            hr = GetNextPacketSize(self._capture_ptr, ctypes.byref(packet_size))
            if hr != S_OK or packet_size.value == 0:
                if not stall_warned and (time.time() - last_packet_time) > 2.0:
                    _murmur(f"[AppLoopback] no packets from PID {self._pid} for "
                         f"2s+ (last hr=0x{hr & 0xFFFFFFFF:08X}) — stream may be stalled")
                    stall_warned = True
                time.sleep(0.005)
                continue

            last_packet_time = time.time()
            stall_warned     = False

            data_ptr   = ctypes.c_void_p()
            num_frames = ctypes.c_uint32()
            flags      = ctypes.c_uint32()
            pos        = ctypes.c_uint64()
            qpc        = ctypes.c_uint64()

            hr = GetBuffer(
                self._capture_ptr,
                ctypes.byref(data_ptr),
                ctypes.byref(num_frames),
                ctypes.byref(flags),
                ctypes.byref(pos),
                ctypes.byref(qpc),
            )
            if hr != S_OK or not data_ptr.value:
                time.sleep(0.005)
                continue

            n = num_frames.value
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                samples = np.zeros(n, dtype=np.float32)
            else:
                raw = (ctypes.c_float * (n * channels)).from_address(data_ptr.value)
                arr = np.frombuffer(raw, dtype=np.float32).copy()
                if channels > 1:
                    arr = arr.reshape(-1, channels).mean(axis=1)
                samples = arr

            ReleaseBuffer(self._capture_ptr, n)

            # Accumulate real samples in a sliding window instead of
            # zero-padding each ~10ms WASAPI packet (~441 frames @ 44.1kHz)
            # up to BLOCK_SIZE (2048). Zero-padding was filling ~80% of
            # every analysis window with silence, which starved bass
            # frequencies and smeared the spectrum with spurious broadband
            # energy from the audio->silence edge — this was the main
            # cause of the Spotify visualization looking far less precise
            # than "All PC Sound".
            accum = np.concatenate([accum, samples])
            # Same real-world window duration as the system-loopback path
            # (see core.audio.ANALYSIS_WINDOW_S) — at the fixed 44100 Hz
            # this stream captures at, that comes out to BLOCK_SIZE (2048)
            # samples, same as before, just derived rather than hardcoded.
            target = int(round(_ca.ANALYSIS_WINDOW_S * self.SAMPLE_RATE))
            max_keep = target * 4  # bound memory if reads ever pile up
            if len(accum) > max_keep:
                accum = accum[-max_keep:]

            if len(accum) >= target:
                chunk = accum[-target:].astype(np.float32, copy=True)
                with _ca.on_air_lock:
                    _ca.on_air = chunk


# ── SpotifyAudioManager ─────────────────────────────────────────

class SpotifyAudioManager:
    """
    Watches for a running Spotify.exe process, opens a ProcessLoopback
    stream for it, and feeds audio into core.audio.on_air.

    If ProcessLoopback is not available (old Windows), sets
    using_app_loopback=False so CoreEngine can apply the silence-fallback.
    """

    def __init__(self, fallback_capture=None) -> None:
        self._fallback          = fallback_capture
        self._running           = False
        self._stream: ProcessLoopbackStream | None = None
        self._active_pid: int | None = None
        self._thread: threading.Thread | None = None
        self.using_app_loopback = False  # determined on first start()
        self._probed            = False  # only probe API availability once

    @property
    def samplerate(self) -> int:
        return ProcessLoopbackStream.SAMPLE_RATE

    def start(self) -> None:
        self._running = True

        if not self._probed:
            # Probe with PID 4 (System) — a harmless, always-present process.
            # Never probe with Spotify's real PID: activating and abandoning
            # a throwaway ProcessLoopback session against the PID we're
            # about to *really* capture from can leave that session in a
            # bad state and starve the real one right after.
            probe = ProcessLoopbackStream(4)
            self.using_app_loopback = probe.open()
            probe.stop()
            self._probed = True
            if self.using_app_loopback:
                _murmur("[SpotifyAudio] ProcessLoopback API available ✓")
            else:
                _holler("[SpotifyAudio] ProcessLoopback unavailable (Win < 20348).")

        if self.using_app_loopback:
            self._thread = threading.Thread(target=self._watch_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream = None
        # Reset so a subsequent start() re-opens the stream even if the
        # same Spotify PID is still running — otherwise _watch_loop thinks
        # it already has a live stream for that PID and never reopens it.
        self._active_pid = None
        # Wait for the watch thread to actually exit before returning, so a
        # quick stop()->start() (mode toggle) can never end up with two
        # _watch_loop threads racing over the same state.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.5)

    def _watch_loop(self) -> None:
        while self._running:
            pids = _get_spotify_pids()
            # Use main/first Spotify PID
            pid = pids[0] if pids else None

            if pid and pid != self._active_pid:
                _murmur(f"[SpotifyAudio] Spotify PID {pid} – opening loopback…")
                if self._stream:
                    self._stream.stop()
                s = ProcessLoopbackStream(pid)
                if s.open():
                    s.start()
                    self._stream    = s
                    self._active_pid = pid
                else:
                    _holler(f"[SpotifyAudio] Could not open loopback for PID {pid}")
                    self._active_pid = None

            elif not pid and self._active_pid:
                _murmur("[SpotifyAudio] Spotify closed – stopping loopback.")
                if self._stream:
                    self._stream.stop()
                    self._stream = None
                self._active_pid = None
                with _ca.on_air_lock:
                    _ca.on_air = np.zeros(_ca.BLOCK_SIZE, dtype=np.float32)

            time.sleep(2.0)