"""Client side of LinkUAE: shared memory + named events (Windows only).

One protocol: wait_frame() -> read the Frame and queue commands -> step().
The client owns the shared block between those two calls; the emulator is stopped.
"""
import ctypes as C
import ctypes.wintypes as W
import os
import time

from . import protocol as P

_k32 = C.WinDLL("kernel32", use_last_error=True)
_k32.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
_k32.OpenFileMappingW.restype = W.HANDLE
_k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t]
_k32.MapViewOfFile.restype = C.c_void_p
_k32.UnmapViewOfFile.argtypes = [C.c_void_p]
_k32.OpenEventW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
_k32.OpenEventW.restype = W.HANDLE
_k32.SetEvent.argtypes = [W.HANDLE]
_k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
_k32.WaitForSingleObject.restype = W.DWORD
_k32.CloseHandle.argtypes = [W.HANDLE]
_k32.QueryPerformanceCounter.argtypes = [C.POINTER(C.c_int64)]

FILE_MAP_ALL_ACCESS = 0xF001F
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000

_REG_NAMES = {f"d{i}": i for i in range(8)} | {f"a{i}": 8 + i for i in range(8)} | {"sp": 15}


class EmulatorGone(Exception):
    pass


class StaleFrame(RuntimeError):
    """A Frame was used after step(): the emulator is running and the block is no longer ours."""


def qpc():
    v = C.c_int64()
    _k32.QueryPerformanceCounter(C.byref(v))
    return v.value


def emulator_present():
    """True if an emulator has the shared block open: only one can be controlled at a time."""
    handle = _k32.OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, P.MAP_NAME)
    if handle:
        _k32.CloseHandle(handle)
    return bool(handle)


class Frame:
    """View of the published frame, read straight from the shared block. Valid until step()."""

    def __init__(self, link):
        self._link = link
        self.number = link.sh.frame
        sh = link.sh
        publish = sh.publish_qpc_lo | (sh.publish_qpc_hi << 32)
        self.latency_us = (qpc() - publish) * 1_000_000 / link.qpc_freq

    @property
    def _sh(self):
        if self._link._frame_number != self.number:
            raise StaleFrame(f"frame {self.number} used after step()")
        return self._link.sh

    vsync = property(lambda s: s._sh.vsync_counter)
    vpos = property(lambda s: s._sh.vpos)
    hpos = property(lambda s: s._sh.hpos)
    cpu_model = property(lambda s: s._sh.cpu_model)
    d = property(lambda s: list(s._sh.d))
    a = property(lambda s: list(s._sh.a))
    usp = property(lambda s: s._sh.usp)
    isp = property(lambda s: s._sh.isp)
    msp = property(lambda s: s._sh.msp)
    vbr = property(lambda s: s._sh.vbr)
    sr = property(lambda s: s._sh.sr)
    pc = property(lambda s: s._sh.pc)

    @property
    def windows(self):
        """[(addr, len)] of the memory windows in this frame."""
        self._sh
        return [(a, n) for a, n, _ in self._link._active_windows]

    def custom_word(self, reg):
        """reg = offset ($DFF000 + reg) or full address."""
        custom = self._sh.custom
        off = reg & 0x1FE
        return (custom[off] << 8) | custom[off + 1]

    def read(self, addr, length):
        self._sh
        link = self._link
        for waddr, wlen, offset in link._active_windows:
            if waddr <= addr and addr + length <= waddr + wlen:
                return C.string_at(link._data + offset + addr - waddr, length)
        raise KeyError(f"${addr:06X}+{length} not inside an active window")

    def u8(self, addr):
        return self.read(addr, 1)[0]

    def u16(self, addr):
        return int.from_bytes(self.read(addr, 2), "big")

    def u32(self, addr):
        return int.from_bytes(self.read(addr, 4), "big")


class Link:
    def __init__(self, connect_timeout=30.0):
        try:
            self._open(connect_timeout)
        except Exception:
            self._release()  # a handle left open would look like an emulator still running
            raise
        self._joy = [0, 0]
        self._inputs = []
        self._writes = []
        self._windows = []          # [(addr, len, offset)] requested by set_windows
        self._windows_dirty = False
        self._active_windows = []   # what the emulator copies into the frames to come
        self._frame_number = None   # the frame we own, None while the emulator runs
        self._n_last_writes = 0
        self._session = self.sh.session_id
        self._attach()

    # --- connection -------------------------------------------------------
    def _open(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self._map = _k32.OpenFileMappingW(FILE_MAP_ALL_ACCESS, False, P.MAP_NAME)
            if self._map:
                break
            if time.monotonic() > deadline:
                raise EmulatorGone("LinkUAE not found (is linkuae=true set?)")
            time.sleep(0.2)
        self._view = _k32.MapViewOfFile(self._map, FILE_MAP_ALL_ACCESS, 0, 0, C.sizeof(P.Shared))
        if not self._view:
            raise OSError(C.get_last_error(), "MapViewOfFile failed")
        self.sh = P.Shared.from_address(self._view)
        self._data = self._view + P.Shared.data.offset
        while self.sh.magic != P.MAGIC or not self.sh.emu_running:
            if time.monotonic() > deadline:
                raise EmulatorGone("linkuae block not ready")
            time.sleep(0.05)
        if self.sh.version != P.VERSION:
            raise RuntimeError(f"protocol version {self.sh.version}, client expects {P.VERSION}")
        access = EVENT_MODIFY_STATE | SYNCHRONIZE
        self._frame_ev = _k32.OpenEventW(access, False, P.FRAME_EVENT)
        self._reply_ev = _k32.OpenEventW(access, False, P.REPLY_EVENT)
        if not self._frame_ev or not self._reply_ev:
            raise OSError(C.get_last_error(), "OpenEvent failed")
        self.qpc_freq = self.sh.qpc_freq_lo | (self.sh.qpc_freq_hi << 32)

    def _attach(self):
        # Only a frame published from now on is ours: the one in the block may be old.
        self._last_frame = self.sh.frame
        self.sh.client_pid = os.getpid()
        self.sh.client_attached = 1

    def close(self):
        if getattr(self, "_view", None):
            if self.attached and self._frame_number is not None:
                self.step()  # sends what is queued and lets the emulator go
            self._frame_number = None  # any Frame still around is now stale
            self.sh.client_attached = 0
            _k32.SetEvent(self._reply_ev)  # release an emulator waiting on us
            self._release()

    def _release(self):
        if getattr(self, "_view", None):
            self.sh = None
            _k32.UnmapViewOfFile(self._view)
            self._view = None
        for name in ("_frame_ev", "_reply_ev", "_map"):
            handle = getattr(self, name, None)
            if handle:
                _k32.CloseHandle(handle)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def attached(self):
        return bool(self.sh.client_attached)

    @property
    def has_frame(self):
        """True between wait_frame() and step()."""
        return self._frame_number is not None

    def reattach(self):
        """After a reply timeout the emulator detaches us (and drops our windows); attach again."""
        self._frame_number = None
        self._active_windows = []
        self._windows_dirty = bool(self._windows)
        self._attach()

    # --- frames -----------------------------------------------------------
    def wait_frame(self, timeout=1.0):
        """Blocks until a new frame is published. Returns a Frame, or None on timeout."""
        if self._frame_number is not None:
            raise RuntimeError("call step() before waiting for another frame")
        sh = self.sh
        deadline = time.monotonic() + timeout
        while True:
            if not sh.emu_running:
                raise EmulatorGone("emulator closed linkuae")
            if sh.session_id != self._session or not sh.client_attached:
                # Emulator restarted, or it dropped us (reply timeout, crash detection).
                self._session = sh.session_id
                self.reattach()
            # frame 0 = nothing published since the block was (re)created
            elif sh.frame and sh.frame != self._last_frame:
                self._last_frame = self._frame_number = sh.frame
                return Frame(self)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            _k32.WaitForSingleObject(self._frame_ev, max(1, int(left * 1000)))

    # --- commands: queued, sent by step() ----------------------------------
    def set_windows(self, windows):
        """windows = [(addr, length)]; in every frame from the next one on."""
        if len(windows) > P.MAX_WINDOWS:
            raise ValueError("too many windows")
        out, off = [], 0
        for addr, length in windows:
            if length <= 0 or off + length > P.DATA_SIZE:
                raise ValueError(f"window ${addr:06X}+{length} does not fit")
            out.append((addr, length, off))
            off += (length + 3) & ~3
        self._windows = out
        self._windows_dirty = True

    def joy(self, port=1, left=False, right=False, up=False, down=False, fire=False, fire2=False, fire3=False):
        """Absolute joystick state; port 1 = joystick port, 0 = mouse port. Held until changed."""
        self._joy[port] = ((P.JOY_LEFT if left else 0) | (P.JOY_RIGHT if right else 0) |
                           (P.JOY_UP if up else 0) | (P.JOY_DOWN if down else 0) |
                           (P.JOY_FIRE if fire else 0) | (P.JOY_FIRE2 if fire2 else 0) |
                           (P.JOY_FIRE3 if fire3 else 0))

    def joy_bits(self, port, bits):
        self._joy[port] = bits & 0x7F

    def key(self, code, down):
        """Raw Amiga keycode (e.g. 0x45 ESC, 0x40 space, 0x28 L)."""
        self._inputs.append((P.IN_KEY, 0, code, 1 if down else 0))

    def mouse(self, port, dx=0, dy=0):
        if dx:
            self._inputs.append((P.IN_MOUSE, port, 0, dx))
        if dy:
            self._inputs.append((P.IN_MOUSE, port, 1, dy))

    def write_u8(self, addr, value):
        self._writes.append((P.WR_MEM8, addr, value & 0xFF))

    def write_u16(self, addr, value):
        self._writes.append((P.WR_MEM16, addr, value & 0xFFFF))

    def write_u32(self, addr, value):
        self._writes.append((P.WR_MEM32, addr, value & 0xFFFFFFFF))

    def set_reg(self, name, value):
        """D0-D7/A0-A7 (A7/SP = active stack). PC/SR are not writable through LinkUAE."""
        self._writes.append((P.WR_REG, _REG_NAMES[name.lower()], value & 0xFFFFFFFF))

    def write_custom(self, addr, value):
        self._writes.append((P.WR_CUSTOM, 0xDFF000 | (addr & 0x1FE), value & 0xFFFF))

    def step(self):
        """Sends the queued commands and lets the emulator run the next frame. Ends the Frame."""
        if self._frame_number is None:
            raise RuntimeError("step() needs a frame: call wait_frame() first")
        sh = self.sh
        if sh.session_id != self._session:  # emulator restarted: that frame no longer exists
            self._frame_number = None       # the next wait_frame() reattaches
            self._inputs.clear()
            self._writes.clear()
            return
        inputs, writes = self._inputs[:P.MAX_INPUTS], self._writes[:P.MAX_WRITES]
        del self._inputs[:len(inputs)]
        del self._writes[:len(writes)]
        for i, (k, p, c, v) in enumerate(inputs):
            e = sh.inputs[i]
            e.kind, e.port, e.code, e.value = k, p, c, v
        for i, (k, a, v) in enumerate(writes):
            w = sh.writes[i]
            w.kind, w.addr, w.value, w.status = k, a, v, P.ST_PENDING
        sh.windows_changed = 1 if self._windows_dirty else 0
        if self._windows_dirty:
            for i, (a, l, _) in enumerate(self._windows):
                w = sh.windows[i]
                w.addr, w.len, w.offset, w.status = a, l, 0, P.ST_PENDING
            sh.n_windows = len(self._windows)
            self._active_windows = list(self._windows)
            self._windows_dirty = False
        sh.n_inputs, sh.n_writes = len(inputs), len(writes)
        sh.joy[0], sh.joy[1] = self._joy
        self._n_last_writes = len(writes)
        reply = self._frame_number
        self._frame_number = None
        sh.reply_frame = reply  # last store: hands the block to the emulator (x86 keeps store order)
        _k32.SetEvent(self._reply_ev)

    def write_results(self):
        """In the frame after step(): status of each write it sent (ST_OK, ST_ERROR, ST_PENDING if dropped)."""
        if self._frame_number is None:
            raise RuntimeError("write results are read from the next frame: call wait_frame() first")
        return [self.sh.writes[i].status for i in range(self._n_last_writes)]

    def stats(self):
        sh = self.sh
        return {"waits": sh.stat_waits, "timeouts": sh.stat_timeouts,
                "last_wait_us": sh.stat_last_wait_us, "max_wait_us": sh.stat_max_wait_us,
                "cmds": sh.stat_cmds, "publish_us": sh.stat_publish_us}
