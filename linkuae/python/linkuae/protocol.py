"""Mirror of include/linkuae.h. Keep in sync and bump VERSION on any change."""
import ctypes as C

MAP_NAME = "Local\\LinkUAE"
FRAME_EVENT = "Local\\LinkUAE_Frame"
REPLY_EVENT = "Local\\LinkUAE_Reply"

MAGIC = 0x4541554C  # 'LUAE'
VERSION = 4
MAX_WINDOWS = 64
MAX_INPUTS = 64
MAX_WRITES = 256
DATA_SIZE = 1024 * 1024

JOY_LEFT, JOY_RIGHT, JOY_UP, JOY_DOWN = 0x01, 0x02, 0x04, 0x08
JOY_FIRE, JOY_FIRE2, JOY_FIRE3 = 0x10, 0x20, 0x40

IN_KEY, IN_MOUSE = 0, 1

WR_MEM8, WR_MEM16, WR_MEM32, WR_REG, WR_CUSTOM = 1, 2, 3, 4, 5

ST_PENDING, ST_OK, ST_ERROR = 0, 1, 2

u32 = C.c_uint32


class Window(C.Structure):
    _fields_ = [("addr", u32), ("len", u32), ("offset", u32), ("status", u32)]


class Input(C.Structure):
    _fields_ = [("kind", u32), ("port", u32), ("code", u32), ("value", C.c_int32)]


class Write(C.Structure):
    _fields_ = [("kind", u32), ("addr", u32), ("value", u32), ("status", u32)]


class Shared(C.Structure):
    _fields_ = [
        ("magic", u32), ("version", u32), ("total_size", u32), ("data_size", u32),
        ("emu_pid", u32), ("emu_running", u32), ("timeout_ms", u32), ("qpc_freq_lo", u32),
        ("qpc_freq_hi", u32), ("session_id", u32),
        ("client_attached", u32), ("client_pid", u32),
        ("frame", u32),
        ("vsync_counter", u32), ("vpos", u32), ("hpos", u32), ("cpu_model", u32),
        ("d", u32 * 8), ("a", u32 * 8),
        ("usp", u32), ("isp", u32), ("msp", u32), ("vbr", u32), ("sr", u32), ("pc", u32),
        ("publish_qpc_lo", u32), ("publish_qpc_hi", u32),
        ("custom", C.c_uint8 * 512),
        ("reply_frame", u32),
        ("windows_changed", u32),
        ("joy", u32 * 2),
        ("n_inputs", u32), ("n_writes", u32), ("n_windows", u32),
        ("inputs", Input * MAX_INPUTS),
        ("writes", Write * MAX_WRITES),
        ("windows", Window * MAX_WINDOWS),
        ("stat_waits", u32), ("stat_timeouts", u32), ("stat_last_wait_us", u32), ("stat_max_wait_us", u32),
        ("stat_cmds", u32), ("stat_publish_us", u32),
        ("data", C.c_uint8 * DATA_SIZE),
    ]
