/*
 * linkuae: low-latency external control (Windows shared memory + events).
 * inputdevice_vsync() arms the frame work; it runs at the next CPU instruction
 * boundary (SPCFLAG_CALLBACK -> device_call_main_thread_callbacks): publish
 * CPU/custom/memory-window state, wait for the client's reply and apply its
 * commands before emulation continues.
 */
#include "sysconfig.h"
#include "sysdeps.h"

#ifdef WITH_LINKUAE

#include <windows.h>
#include "options.h"
#include "memory.h"
#include "newcpu.h"
#include "custom.h"
#include "inputdevice.h"
#include "inputrecord.h"
#include "debug.h"
#include "savestate.h"
#include "linkuae.h"

// Must match linkuae/python/linkuae/protocol.py (ctypes.sizeof(Shared) / Shared.data.offset).
static_assert(offsetof(struct linkuae_shared, data) == 6872, "linkuae layout changed: update protocol.py");
static_assert(sizeof(struct linkuae_shared) == 6872 + LINKUAE_DATA_SIZE, "linkuae layout changed: update protocol.py");

static HANDLE map_handle, frame_event, reply_event, client_process;
static struct linkuae_shared *sh;
static uint32_t client_pid_seen;
static bool attached, open_failed, frame_pending, cpu_thread_warned;
static uint32_t pending_vsync;
static uint32_t held_joy[2];
static bool held_keys[0x68];
static int64_t qpc_freq;

struct local_window {
	uint32_t addr, len, offset;
};
static struct local_window windows[LINKUAE_MAX_WINDOWS];
static int num_windows;

static const int joy_events[2][7] = {
	{ INPUTEVENT_JOY1_LEFT, INPUTEVENT_JOY1_RIGHT, INPUTEVENT_JOY1_UP, INPUTEVENT_JOY1_DOWN,
	  INPUTEVENT_JOY1_FIRE_BUTTON, INPUTEVENT_JOY1_2ND_BUTTON, INPUTEVENT_JOY1_3RD_BUTTON },
	{ INPUTEVENT_JOY2_LEFT, INPUTEVENT_JOY2_RIGHT, INPUTEVENT_JOY2_UP, INPUTEVENT_JOY2_DOWN,
	  INPUTEVENT_JOY2_FIRE_BUTTON, INPUTEVENT_JOY2_2ND_BUTTON, INPUTEVENT_JOY2_3RD_BUTTON }
};

static int64_t qpc_now(void)
{
	LARGE_INTEGER t;
	QueryPerformanceCounter(&t);
	return t.QuadPart;
}

static uint32_t qpc_us(int64_t ticks)
{
	return (uint32_t)(ticks * 1000000 / qpc_freq);
}

// Pressed bits are re-sent every time: an input reset (GUI, config change)
// clears the emulated state without us knowing. Released bits only on change,
// so a real joystick on the same port keeps working.
static void set_joy(int port, uint32_t state)
{
	state &= 0x7f;
	uint32_t send = state | (state ^ held_joy[port]);
	for (int b = 0; b < 7; b++) {
		if (send & (1 << b))
			send_input_event(joy_events[port][b], (state >> b) & 1, 0, 0);
	}
	held_joy[port] = state;
}

static void release_all(void)
{
	for (int p = 0; p < 2; p++) {
		if (held_joy[p])
			set_joy(p, 0);
	}
	for (int k = 0; k < 0x68; k++) {
		if (held_keys[k]) {
			inputdevice_do_keyboard(k, 0);
			held_keys[k] = false;
		}
	}
}

static void detach(void)
{
	release_all();
	num_windows = 0;
	attached = false;
	client_pid_seen = 0;
	if (client_process) {
		CloseHandle(client_process);
		client_process = NULL;
	}
	if (sh)
		sh->client_attached = 0;
}

static void setup_windows(void)
{
	uint32_t n = sh->n_windows;
	uint32_t offset = 0;
	if (n > LINKUAE_MAX_WINDOWS)
		n = LINKUAE_MAX_WINDOWS;
	num_windows = 0;
	for (uint32_t i = 0; i < n; i++) {
		struct linkuae_window *w = &sh->windows[i];
		uint32_t len = w->len;
		if (!len || len > LINKUAE_DATA_SIZE - offset) {
			w->status = LINKUAE_ST_ERROR;
			continue;
		}
		windows[num_windows].addr = w->addr;
		windows[num_windows].len = len;
		windows[num_windows].offset = offset;
		w->offset = offset;
		w->status = LINKUAE_ST_OK;
		num_windows++;
		offset += (len + 3) & ~3;
	}
}

static bool ram_range(uaecptr addr, uint32_t len)
{
	for (uint32_t i = 0; i < len; i++) {
		addrbank &bank = get_mem_bank(addr + i);
		if (!(bank.flags & ABFLAG_RAM) || !bank.check(addr + i, 1))
			return false;
	}
	return true;
}

static uint32_t apply_write(const struct linkuae_write *w, bool *mem_written)
{
	switch (w->kind) {
	case LINKUAE_WR_MEM8:
	case LINKUAE_WR_MEM16:
	case LINKUAE_WR_MEM32:
	{
		uint32_t len = w->kind == LINKUAE_WR_MEM8 ? 1 : w->kind == LINKUAE_WR_MEM16 ? 2 : 4;
		if (!ram_range(w->addr, len))
			return LINKUAE_ST_ERROR;
		// Byte writes: big-endian order, no alignment rules.
		for (uint32_t i = 0; i < len; i++)
			get_mem_bank(w->addr + i).bput(w->addr + i, (uae_u8)(w->value >> (8 * (len - 1 - i))));
		*mem_written = true;
		return LINKUAE_ST_OK;
	}
	case LINKUAE_WR_REG:
		// Safe at an instruction boundary. PC/SR stay GDB-only (prefetch/stack bank).
		if (w->addr > 15)
			return LINKUAE_ST_ERROR;
		regs.regs[w->addr] = w->value;
		return LINKUAE_ST_OK;
	case LINKUAE_WR_CUSTOM:
		if (w->addr < 0xdff000 || w->addr > 0xdff1fe || (w->addr & 1))
			return LINKUAE_ST_ERROR;
		debug_write_memory_16(w->addr, (uae_u16)w->value);
		return LINKUAE_ST_OK;
	}
	return LINKUAE_ST_ERROR;
}

static void process_commands(void)
{
	if (sh->windows_changed)
		setup_windows();
	// Input recorder owns the input stream while recording/playing.
	if (!input_record && !input_play) {
		set_joy(0, sh->joy[0]);
		set_joy(1, sh->joy[1]);
		uint32_t n = sh->n_inputs;
		if (n > LINKUAE_MAX_INPUTS)
			n = LINKUAE_MAX_INPUTS;
		for (uint32_t i = 0; i < n; i++) {
			const struct linkuae_input *in = &sh->inputs[i];
			if (in->kind == LINKUAE_IN_KEY && in->code < 0x68) {
				bool down = in->value != 0;
				// Key-down always goes through: a reset may have released it behind our back.
				if (down || held_keys[in->code]) {
					held_keys[in->code] = down;
					inputdevice_do_keyboard(in->code, down);
				}
			} else if (in->kind == LINKUAE_IN_MOUSE && in->port < 2 && in->code < 2) {
				static const int axes[2][2] = {
					{ INPUTEVENT_MOUSE1_HORIZ, INPUTEVENT_MOUSE1_VERT },
					{ INPUTEVENT_MOUSE2_HORIZ, INPUTEVENT_MOUSE2_VERT }
				};
				send_input_event(axes[in->port][in->code], in->value, 0, 0);
			}
		}
	}
	uint32_t n = sh->n_writes;
	if (n > LINKUAE_MAX_WRITES)
		n = LINKUAE_MAX_WRITES;
	bool mem_written = false;
	for (uint32_t i = 0; i < n; i++)
		sh->writes[i].status = apply_write(&sh->writes[i], &mem_written);
	if (mem_written) {
		// Same invalidation as GdbTarget::write_memory, minus fill_prefetch.
		flush_cpu_caches(true);
		flush_icache(3);
	}
	sh->stat_cmds++;
}

static void copy_window(const struct local_window *w)
{
	uint8_t *dst = sh->data + w->offset;
	addrbank &bank = get_mem_bank(w->addr);
	// Fast path only for directly mapped RAM/ROM: xlateaddr on other banks can halt the CPU.
	if (bank.baseaddr_direct_r && (bank.flags & (ABFLAG_RAM | ABFLAG_ROM | ABFLAG_ROMIN)) &&
		valid_address(w->addr, w->len)) {
		memcpy(dst, get_real_address(w->addr), w->len);
		return;
	}
	for (uint32_t i = 0; i < w->len; i++) {
		uaecptr a = w->addr + i;
		addrbank &b = get_mem_bank(a);
		dst[i] = ((b.flags & (ABFLAG_RAM | ABFLAG_ROM | ABFLAG_ROMIN)) && b.check(a, 1)) ? (uint8_t)b.bget(a) : 0;
	}
}

static void publish_state(void)
{
	int64_t t0 = qpc_now();

	MakeSR();
	sh->vsync_counter = vsync_counter;
	sh->vpos = vpos;
	sh->hpos = agnus_hpos;
	sh->cpu_model = currprefs.cpu_model;
	for (int i = 0; i < 8; i++) {
		sh->d[i] = regs.regs[i];
		sh->a[i] = regs.regs[8 + i];
	}
	sh->usp = regs.s ? regs.usp : regs.regs[15];
	sh->isp = (regs.s && !regs.m) ? regs.regs[15] : regs.isp;
	sh->msp = (regs.s && regs.m) ? regs.regs[15] : regs.msp;
	sh->vbr = regs.vbr;
	sh->sr = regs.sr;
	sh->pc = m68k_getpc();

	// Same snapshot the GDB server uses for $DFF000 reads (no live I/O side effects).
	uae_u8 snapshot[4 + 512 + 4];
	size_t size = 0;
	save_custom(&size, snapshot, 1);
	if (size > 4)
		memcpy(sh->custom, snapshot + 4, size - 4 < 512 ? size - 4 : 512);

	for (int i = 0; i < num_windows; i++)
		copy_window(&windows[i]);

	int64_t t1 = qpc_now();
	sh->publish_qpc_lo = (uint32_t)t1;
	sh->publish_qpc_hi = (uint32_t)(t1 >> 32);
	sh->stat_publish_us = qpc_us(t1 - t0);
	MemoryBarrier();
	sh->frame = sh->frame + 1; // last: hands the block to the client
}

// True if the client replied: its commands are complete and can be applied.
static bool wait_reply(void)
{
	uint32_t target = sh->frame;
	uint32_t timeout = sh->timeout_ms;
	int64_t start = qpc_now();
	HANDLE handles[2] = { reply_event, client_process };
	DWORD nhandles = client_process ? 2 : 1;
	sh->stat_waits++;
	while (sh->reply_frame != target) {
		if (!sh->client_attached)
			break;
		uint32_t elapsed = qpc_us(qpc_now() - start) / 1000;
		if (elapsed >= timeout) {
			sh->stat_timeouts++;
			write_log(_T("linkuae: lockstep reply timeout, client detached\n"));
			detach();
			break;
		}
		DWORD r = WaitForMultipleObjects(nhandles, handles, FALSE, timeout - elapsed);
		if (r == WAIT_OBJECT_0 + 1 || r == WAIT_FAILED) {
			write_log(_T("linkuae: client process %u gone (wait=%u)\n"), client_pid_seen, r);
			detach();
			break;
		}
	}
	uint32_t us = qpc_us(qpc_now() - start);
	sh->stat_last_wait_us = us;
	if (us > sh->stat_max_wait_us)
		sh->stat_max_wait_us = us;
	bool replied = sh->reply_frame == target;
	MemoryBarrier();
	return replied && attached; // a detach already released the controls: don't take them again
}

static bool open_shared(void)
{
	map_handle = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, sizeof(struct linkuae_shared), LINKUAE_MAP_NAME);
	if (!map_handle) {
		write_log(_T("linkuae: CreateFileMapping failed, err=%d\n"), GetLastError());
		return false;
	}
	bool existed = GetLastError() == ERROR_ALREADY_EXISTS;
	sh = (struct linkuae_shared *)MapViewOfFile(map_handle, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(struct linkuae_shared));
	if (!sh) {
		write_log(_T("linkuae: MapViewOfFile failed, err=%d\n"), GetLastError());
		CloseHandle(map_handle);
		map_handle = NULL;
		return false;
	}
	if (existed && sh->magic == LINKUAE_MAGIC && sh->emu_running && sh->emu_pid != GetCurrentProcessId()) {
		HANDLE other = OpenProcess(SYNCHRONIZE, FALSE, sh->emu_pid);
		bool alive = other && WaitForSingleObject(other, 0) == WAIT_TIMEOUT;
		if (other)
			CloseHandle(other);
		if (alive) {
			write_log(_T("linkuae: already owned by another WinUAE (pid %u)\n"), sh->emu_pid);
			UnmapViewOfFile(sh);
			CloseHandle(map_handle);
			sh = NULL;
			map_handle = NULL;
			return false;
		}
	}
	frame_event = CreateEventA(NULL, FALSE, FALSE, LINKUAE_FRAME_EVENT);
	reply_event = CreateEventA(NULL, FALSE, FALSE, LINKUAE_REPLY_EVENT);
	if (!frame_event || !reply_event) {
		write_log(_T("linkuae: CreateEvent failed, err=%d\n"), GetLastError());
		if (frame_event)
			CloseHandle(frame_event);
		if (reply_event)
			CloseHandle(reply_event);
		frame_event = reply_event = NULL;
		UnmapViewOfFile(sh);
		CloseHandle(map_handle);
		sh = NULL;
		map_handle = NULL;
		return false;
	}

	LARGE_INTEGER f;
	QueryPerformanceFrequency(&f);
	qpc_freq = f.QuadPart;

	uint32_t session = (uint32_t)qpc_now() | 1;
	if (existed && session == sh->session_id)
		session += 2;
	memset(sh, 0, offsetof(struct linkuae_shared, data));
	sh->version = LINKUAE_VERSION;
	sh->total_size = sizeof(struct linkuae_shared);
	sh->data_size = LINKUAE_DATA_SIZE;
	sh->emu_pid = GetCurrentProcessId();
	sh->timeout_ms = currprefs.linkuae_timeout;
	sh->qpc_freq_lo = (uint32_t)qpc_freq;
	sh->qpc_freq_hi = (uint32_t)(qpc_freq >> 32);
	sh->session_id = session;
	sh->emu_running = 1;
	MemoryBarrier();
	sh->magic = LINKUAE_MAGIC; // last: client checks it to know the block is ready
	write_log(_T("linkuae: shared memory ready (%u bytes)\n"), (uint32_t)sizeof(struct linkuae_shared));
	return true;
}

void linkuae_close(void)
{
	frame_pending = false;
	if (!sh)
		return;
	detach();
	sh->emu_running = 0;
	MemoryBarrier();
	SetEvent(frame_event); // wake a waiting client so it sees emu_running == 0
	UnmapViewOfFile(sh);
	sh = NULL;
	CloseHandle(map_handle);
	CloseHandle(frame_event);
	CloseHandle(reply_event);
	map_handle = frame_event = reply_event = NULL;
}

void linkuae_vsync(void)
{
	if (!currprefs.linkuae) {
		linkuae_close();
		open_failed = false;
		return;
	}
	if (!sh) {
		if (open_failed || !open_shared()) {
			open_failed = true;
			return;
		}
	}
	if (!sh->client_attached && !attached)
		return;
	// The threaded CPU never services SPCFLAG_CALLBACK.
	if (currprefs.cpu_thread) {
		if (!cpu_thread_warned)
			write_log(_T("linkuae: not available with cpu_threaded=true\n"));
		cpu_thread_warned = true;
		return;
	}
	// Events can run mid-instruction: defer the frame work to the next boundary.
	frame_pending = true;
	pending_vsync = vsync_counter;
	set_special(SPCFLAG_CALLBACK);
}

void linkuae_instruction_boundary(void)
{
	if (!frame_pending || !sh)
		return;
	frame_pending = false;
	// vsync_counter++ runs right after inputdevice_vsync(), hence +1. More means a flag
	// lost to a reset firing later from another callback, mid-frame: drop it.
	if (vsync_counter - pending_vsync > 1)
		return;
	sh->timeout_ms = currprefs.linkuae_timeout;

	if (!sh->client_attached) {
		if (attached)
			detach();
		return;
	}
	if (!attached || sh->client_pid != client_pid_seen) {
		release_all(); // a new client must not inherit the old one's held controls
		if (client_process)
			CloseHandle(client_process);
		client_pid_seen = sh->client_pid;
		client_process = NULL;
		attached = true;
	}
	if (!client_process)
		client_process = OpenProcess(SYNCHRONIZE, FALSE, client_pid_seen);
	// A client that dies without detaching must not leave controls held.
	if (client_process && WaitForSingleObject(client_process, 0) == WAIT_OBJECT_0) {
		write_log(_T("linkuae: client process %u exited\n"), client_pid_seen);
		detach();
		return;
	}

	publish_state();
	SetEvent(frame_event);
	if (wait_reply())
		process_commands(); // they act on the frame about to run
}

#endif
