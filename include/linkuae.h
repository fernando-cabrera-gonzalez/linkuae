/*
 * linkuae: low-latency external control through a named shared memory block.
 * One protocol, lockstep: on every frame the emulator publishes its state and stops.
 * The client owns the block from the new `frame` value until it stores reply_frame;
 * the emulator owns it the rest of the time, so no other locking is needed.
 * Layout shared with the Python client (linkuae/python/linkuae/protocol.py):
 * keep both in sync and bump LINKUAE_VERSION on any change.
 * All fields are host (little-endian) uint32 except custom[] and data[],
 * which hold raw big-endian Amiga bytes.
 */
#ifndef UAE_LINKUAE_H
#define UAE_LINKUAE_H

#include <stdint.h>

#define LINKUAE_MAP_NAME     "Local\\LinkUAE"
#define LINKUAE_FRAME_EVENT  "Local\\LinkUAE_Frame"
#define LINKUAE_REPLY_EVENT  "Local\\LinkUAE_Reply"

#define LINKUAE_MAGIC        0x4541554c /* 'LUAE' */
#define LINKUAE_VERSION      4
#define LINKUAE_MAX_WINDOWS  64
#define LINKUAE_MAX_INPUTS   64
#define LINKUAE_MAX_WRITES   256
#define LINKUAE_DATA_SIZE    (1024 * 1024)

/* joy[] bits */
#define LINKUAE_JOY_LEFT  0x01
#define LINKUAE_JOY_RIGHT 0x02
#define LINKUAE_JOY_UP    0x04
#define LINKUAE_JOY_DOWN  0x08
#define LINKUAE_JOY_FIRE  0x10
#define LINKUAE_JOY_FIRE2 0x20
#define LINKUAE_JOY_FIRE3 0x40

/* linkuae_input.kind */
#define LINKUAE_IN_KEY   0 /* code = raw Amiga keycode 0x00-0x67, value 1 down / 0 up */
#define LINKUAE_IN_MOUSE 1 /* port 0/1, code 0 = x / 1 = y, value = signed relative count */

/* linkuae_write.kind */
#define LINKUAE_WR_MEM8   1
#define LINKUAE_WR_MEM16  2
#define LINKUAE_WR_MEM32  3
#define LINKUAE_WR_REG    4 /* addr 0-7 = D0-D7, 8-15 = A0-A7 */
#define LINKUAE_WR_CUSTOM 5 /* addr = even $DFF000-$DFF1FE, 16-bit value */

/* linkuae_write.status / linkuae_window.status */
#define LINKUAE_ST_PENDING 0
#define LINKUAE_ST_OK      1
#define LINKUAE_ST_ERROR   2

struct linkuae_window {
	uint32_t addr, len;
	uint32_t offset; /* filled by emulator: offset into data[] */
	uint32_t status;
};

struct linkuae_input {
	uint32_t kind, port, code;
	int32_t value;
};

struct linkuae_write {
	uint32_t kind, addr, value;
	uint32_t status; /* filled by emulator */
};

struct linkuae_shared {
	/* header, emulator-owned */
	uint32_t magic, version, total_size, data_size;
	uint32_t emu_pid, emu_running, timeout_ms, qpc_freq_lo;
	uint32_t qpc_freq_hi;
	uint32_t session_id; /* new value every time the emulator (re)opens the block */

	/* client attach */
	volatile uint32_t client_attached, client_pid;

	/* state, written by the emulator; `frame` last: a new value hands the block to the client */
	volatile uint32_t frame;
	uint32_t vsync_counter, vpos, hpos, cpu_model;
	uint32_t d[8], a[8];
	uint32_t usp, isp, msp, vbr, sr, pc;
	uint32_t publish_qpc_lo, publish_qpc_hi;
	uint8_t custom[512]; /* $DFF000-$DFF1FF snapshot, big-endian */

	/* client stores the frame it has answered, after filling the commands below */
	volatile uint32_t reply_frame;

	/* commands of that reply; the emulator fills in the statuses before the next frame */
	uint32_t windows_changed;
	uint32_t joy[2]; /* port 0 = mouse port, port 1 = joystick port */
	uint32_t n_inputs, n_writes, n_windows;
	struct linkuae_input inputs[LINKUAE_MAX_INPUTS];
	struct linkuae_write writes[LINKUAE_MAX_WRITES];
	struct linkuae_window windows[LINKUAE_MAX_WINDOWS];

	/* stats, emulator-owned */
	uint32_t stat_waits, stat_timeouts, stat_last_wait_us, stat_max_wait_us;
	uint32_t stat_cmds, stat_publish_us;

	/* memory window data */
	uint8_t data[LINKUAE_DATA_SIZE];
};

#ifdef WITH_LINKUAE
void linkuae_vsync(void);
void linkuae_instruction_boundary(void);
void linkuae_close(void);
#else
static inline void linkuae_vsync(void) {}
static inline void linkuae_instruction_boundary(void) {}
static inline void linkuae_close(void) {}
#endif

#endif
