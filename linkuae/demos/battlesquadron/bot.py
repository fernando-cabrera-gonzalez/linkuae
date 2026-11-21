"""Battle Squadron (Innerprise, 1989) bot over LinkUAE, player 1.

Every frame it reads the ship, the enemies and the enemy bullets, and plans the ship's path over the next
HORIZON game ticks with dynamic programming over a grid of ship positions (one cell = the 4 px the ship moves
per tick, any number of turns). The first step of the cheapest path is the
joystick. A bomb is thrown when no path avoids a hit, not when a cost is high.

  python bot.py --want orange [--frames N] [--press-p] [--pause-at-end]

RAM map (A500, 512K chip + 512K slow):
  player 1     $4DA2: +0 X, +2 Y on screen (999 = not in play), +4 X on the playfield; +$3B weapon, +$43 bombs
               the ship is sprites 4+5 (32x30): hstart = X - 113, vstart = Y - 218
  objects      $2DC80: 12 slots of $50 bytes; +$10..+$16 hit box x1 x2 y1 y2 on the playfield
               (screen X = playfield X - (player +4 - player +0)); +$18 flags (high byte = hp), +$20 kind pointer
               the game's ship-vs-object test ($3556) skips a slot with +$1D != 0, bit 3 of +$1E (the garage cars),
               or +$1F = 6 with +$3F >= 5; +$1F = 5 is a pick-up. Ship box there: playfield x+7..x+25, y+7..y+23
  ground       $2E040: 16 slots of $40 bytes; +$30..+$36 box, +$1C hp, +$0E graphic ($EB00 green structure, $5B00 door)
  pick-ups     object kind $12930; +$1C / 2 = colour: 0 red, 1 blue, 2 green, 3 orange, 5 = the bomb "M"
  bullets      the images of sprites 0-3: bullets 7 lines tall, homing "arrows" 12 lines tall
  bombs        thrown by holding fire and turning the stick a full circle anticlockwise (10 frames)
The game logic runs at 25 Hz: the ship moves 4 px every other frame.
"""
import argparse, collections, json, os, sys, threading, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "python"))
import numpy as np
try:
    import plan_nb                       # the planner's heavy part compiled with Numba (pip install numba): about 5x faster
except ImportError:
    plan_nb = None                       # plain numpy: the same algorithm, only slower
from linkuae import Link
from linkuae.link import EmulatorGone

# ---------------------------------------------------------------- measured facts (RAM, geometry, timings)
CHIP = 0x000000
PLAYER = 0x4DA2
OBJ_TABLE, OBJ_SIZE, OBJ_COUNT = 0x2DC80, 0x50, 12
GROUND_TABLE, GROUND_SIZE, GROUND_COUNT = 0x2E040, 0x40, 16
SPRITES = 0x5E000            # sprite n's list at SPRITES + n * $400
KEY_P = 0x19
PAUSE_LOOP = (0xDB6, 0xDE2)  # the game's own pause loop
DOOR_GFX, STRUCT_GFX = 0x5B00, 0xEB00
TOUGH_GFX = {0xA000,         # ground: a hole that turns into a head and fires a lot (hp 47)
             0x2800,         # ground: the purple armoured "crab claws", 48x64 or 64x64, hp 23
             }
A280_GFX = 0xA280            # ground (green): 32x64, hp 21-35, fires point-blank at a ship flying over it. Apart from TOUGH_GFX: it is the only one
                             # we go after (priority over the flying enemies with a strong gun), the others are still only kept away from
IGNORE_A280 = False          # --ignore-a280: the A280 do not exist for the planner (no fear, no hunt, no ground target); only the statistics see them
A280_LEVEL, AIM_A280, A280_FOCUS_R = 4, 0.8, 64.0    # weapon level (0..5) from which the A280 are hunted; cost per px off its column (AIM 0.6 .. AIM_STRUCT 1.0);
                             # the A280 being hunted is feared from A280_FOCUS_R (the others, and by default this one too, from PROX_R)
DOOR_SIZE, DOOR_BELOW = 32, 8
SPR_DX, SPR_DY = 113, 218    # sprite hstart/vstart -> player screen coordinates
BOMBS, WEAPON, LEVEL = PLAYER + 0x43, PLAYER + 0x3B, PLAYER + 0x3D   # LEVEL: low byte of the word +$3C, 0..5
LIVES = PLAYER + 0x38       # reserve lives = the HUD icons (3 in the intro, 2 once the ship is in play, 1, 0); a death at 0 is the game over
BOMB_TIMER = PLAYER + 0x5A   # 0, or a countdown from $FD while our own bomb is going off (measured)
BOMB_SPRITE_H = 16           # ... and meanwhile its explosion is drawn with 16-line sprites: not bullets
GAME_OVER_FRAMES = 15        # game over = reserve lives 0 and the ship out of play (px = 999) this many frames in a row
ALIVE_BEFORE_DEATH = 25      # a death counts only after the ship has been alive this long (the explosion image flickers)
COLOURS = {"red": 0, "blue": 1, "green": 2, "orange": 3}
BOMB_ITEM = 5
ITEM_KINDS = {0x00012930}
FLAGS0_SOLID = {0x00017780}  # a 32x32 enemy that sits at flags $0000 and still kills
EXPLOSION_KINDS = range(0x00011310, 0x00012810 + 1)
EXPLOSION_LINES = (56, 64)   # height of the ship's explosion image in sprite 4 (60 on every real death)

SHIP_W, SHIP_H = 32, 30
X_MIN, X_MAX = 256, 512      # ship's top-left limits
Y_MIN, Y_MAX = 250, 422
SCREEN_TOP = 256             # first visible line, in the player's Y
SHIP_HIT_X, SHIP_HIT, SHIP_HIT_DY = 17, 12, 4     # hit box against bullets (measured): half width, half height, centre this far below the sprite's centre
OBJ_HIT_X, OBJ_HIT_Y, OBJ_HIT_DY = 9, 8, 0       # hit box against objects (the game's code: x+7..x+25, y+7..y+23)
BULLET_HIT = 4
ITEM_HIT = 8 + 11            # a pick-up is taken a bit before the ship's core touches it
VEL_AGES = (6, 4, 2)         # frames back to measure a velocity over, longest first (the game moves things at an irregular 25 Hz)
# The playfield scrolls sideways with the ship: the camera heads for 0.375*X - 96.3 (0..96) at 0.5 px per frame.
SCROLL_SLOPE, SCROLL_BASE, SCROLL_MAX, SCROLL_RATE = 0.375, -96.3, 96.0, 0.5
BOMB_MOVES = ["U", "L", "D", "R", "U"]    # the gesture, BOMB_STEP frames each: the bomb goes off 9-10 frames in
CIRCLE = "ULDR"                           # the same circle can start at any point: L D R U L, D R U L D, R U L D R
BOMB_STEP = 2

# ---------------------------------------------------------------- the planner's grid
STEP = 4                     # px the ship moves per game tick, on each axis
TICK = 2                     # frames per game tick
HORIZON = 14                 # ticks planned ahead (28 frames)
GX = np.arange(X_MIN, X_MAX + 1, STEP, dtype=np.float32)     # ship's left edge, per column
GY = np.arange(Y_MIN, Y_MAX + 1, STEP, dtype=np.float32)     # ship's top edge, per row
NX, NY = len(GX), len(GY)
CX, CY = GX + SHIP_W / 2, GY + SHIP_H / 2                   # ship's centre
NEIGH = [(0, 0)] + [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0)]
F4 = np.float32

# ---------------------------------------------------------------- tuning
HIT_BASE, HIT_SOON, HIT_DECAY = 2500.0, 3000.0, 0.75    # cost of a hit at tick t: BASE + SOON * DECAY^t. Always above everything else added up
GROW = 0.3                   # px per tick the boxes are widened by: far predictions are less reliable
NEAR_PX, NEAR_COST, NEAR_DECAY = 6.0, 25.0, 0.85        # a ring this wide around every box costs this per tick: no grazing
PROX_R_SMALL, SMALL_HALF = 24.0, 17   # a plain small enemy (both half sizes below SMALL_HALF, e.g. the 32x32 $1A780: half sizes 16) only repels from this close: a wider radius keeps
                                       # the ship from lining up under it
PROX_R_RISER, RISER_VY = 38.0, -0.5   # a small enemy climbing from below (vy < RISER_VY px/frame) fires point-blank as it passes the ship
                                       # (measured on a death): bullet ~2.1 px/frame, ~12 frames to see it and get 20 px
                                       # aside -> ~25 px of flight + the ship's half box = ~37-42 px from its centre
NEW_BULLET_R, NEW_BULLET_SPEED = 64.0, 2.0   # a bullet born this close to the ship has no measured speed for 2 frames: assume it is aimed at
                                             # the ship at the usual speed (measured ~2.1 px/frame) until it is known
GROUND_MARGIN, GROUND_W = 12.0, 600.0   # the ship's box over any live ground object (+ this margin) costs this per tick: they fire point-blank
                                        # at a ship flying over them ($A280, $E840, $E6A0...).
                                        # Below a hit (HIT_BASE) so a dodge can still cross one; the ship can still line up below them to shoot
PROX_R, PROX_W = 64.0, 40.0  # being closer than R to something that shoots costs up to W per tick (point-blank shots cannot be dodged)
WALL_R, WALL_W = 64.0, 10.0  # being closer than R to a side limit costs up to W per tick, scaled by the threat (PROX + NEAR) where the
                             # ship is now: free when calm (go to the sides for targets), a cost when fleeing (a corner leaves half the ways out)
UNWANTED = 30.0              # per tick on top of a pick-up of a colour we do not want: avoided, never deadly
ITEM_REWARD, ITEM_DECAY = 120.0, 0.9
RUN, TERM = 0.05, 1.0        # weight of the aim field at every tick, and at the end of the path
MOVE_COST = 0.3              # per step: when nothing matters, stay put
KEEP = 0.2                   # ... and prefer the direction already taken
Y_PREF, Y_HIGH, ITEM_LOW = 410, 340, 300
AIM, AIM_IDLE, AIM_STRUCT, AIM_BOSS, AIM_BOSS_BOMB = 0.6, 0.2, 1.0, 1.2, 3.0   # cost per px off the target's column
ENTER_MARGIN = 30            # a target counts from when its lower edge is this close above the screen
BIG = 30                     # an air enemy at least this half-wide is a boss piece: line up under the biggest
BOSS_KIND, BOSS_WING_W = 0x000511E0, 76      # the final boss: both 76 px wings first (any order), then the head, a second head, then the whole body
DOOR_PULL_X, DOOR_PULL_Y, DOOR_RUSH, DOOR_RUSH_PULL = 1.5, 1.0, 150, 2.0
DOOR_NEAR, DOOR_WAIT = 220, 12   # close to the door: clear way = rush in, busy way = bomb after DOOR_WAIT ticks, then rush
DOOR_BOMB = 110              # a blocked way only earns the bomb once the door is this close (it scrolls ~0.5 px/frame)
DOOR_CORRIDOR = 40            # half-width added around the ship-to-door corridor when looking for what blocks it
DOOR_ENGAGE = 160             # the door only pulls the ship once it is this close above it (screen px)
BOSS_LINE, BOSS_BULLETS, BOSS_WAIT, BOSS_WINDOW = 40, 4, 500, 200   # BOSS_WAIT: frames under fire before a boss bomb
LAUNCHER_KIND = 0x000147E8   # air table: the weapon-pod launcher (the turret next to it is ground $EA80)
LOW_LEVEL, LAUNCH_LINE, LAUNCH_NEAR, LAUNCH_WAIT, LAUNCH_COST = 2, 40, 130, 120, 400.0   # weak gun: bomb the launcher only if it is close, the way to it is cut AND the plan is bad, all for this long
LAUNCH_MIN_BOMBS = 3         # ... and never with fewer bombs than this left (kept for real emergencies)
TRAP_TICKS = 9               # no hit-free path this many ticks ahead = trapped
TRAP_MIN = 3                 # ... but a hit sooner than this cannot be bombed away (the gesture takes 5 ticks): keep dodging
TRAP_CONFIRM = 2             # frames in a row it must hold
PLAN_HIT, PLAN_CONFIRM = 10000.0, 2  # the best plan costing this much (hits on every way, margins included) this many frames
                                     # in a row is a trap too: the exact-box test often still sees a gap that closes later.
RESPAWN_CALM, BOMB_COOLDOWN = 250, 200
GHOST_FRAMES, GHOST_MASS = 6, 3      # a bullet that drops out of the sprites is kept this long; this many at once is a screen clear
TRACE_FRAMES, MAX_TRACES = 100, 100  # 100 frames = 2 s of RAM kept; saved on a death, a bomb, or when a file named mark.flag is created next to bot.py
MARK_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mark.flag")


# ---------------------------------------------------------------- reading the game
class Mem:
    def __init__(self, raw):
        self.raw = raw

    def w(self, a):
        return (self.raw[a] << 8) | self.raw[a + 1]


def sprite_images(m, n):
    """[(hstart, vstart, height, address)] of every image in sprite n's list."""
    out, a = [], SPRITES + n * 0x400
    for _ in range(24):
        pos, ctl = m.w(a), m.w(a + 2)
        vs = (pos >> 8) | ((ctl & 4) << 6)
        ve = (ctl >> 8) | ((ctl & 2) << 7)
        if ve <= vs:
            break
        out.append((((pos & 0xFF) << 1) | (ctl & 1), vs, ve - vs, a))
        a += 4 + 4 * (ve - vs)
    return out


def sprite_box(m, a, h):
    """(first column, last column, first row, last row) of the lit pixels of the image whose header is at a."""
    cols, rows = 0, []
    for r in range(h):
        row = m.w(a + 4 + 4 * r) | m.w(a + 6 + 4 * r)
        cols |= row
        if row:
            rows.append(r)
    lit = [c for c in range(16) if cols >> (15 - c) & 1]
    return (lit[0], lit[-1], rows[0], rows[-1]) if lit else None


def read_state(m):
    """Everything in the player's screen coordinates, centres:
    ship = (x, y, exploding) or None; enemies = {slot: (cx, cy, hw, hh, flags, kind, colour, touch)}; bullets = [(cx, cy, hw, hh)];
    touch = the game's own test says the ship crashes into it;
    ground = [(cx, cy, hp, slot)]; structs and tough (TOUGH_GFX) = [(cx, cy, hp, hw, hh)]; doors = [(x1, y1, w, h)]; scroll = playfield X - screen X."""
    px, py, pfx = m.w(PLAYER), m.w(PLAYER + 2), m.w(PLAYER + 4)
    exploding = any(EXPLOSION_LINES[0] <= h <= EXPLOSION_LINES[1] for _, _, h, _ in sprite_images(m, 4))
    s = types.SimpleNamespace(ship=None if px == 999 else (px, py, exploding), scroll=pfx - px,
                              enemies={}, shielded=set(), bullets=[], ground=[], ground_boxes=[], structs=[], tough=[], a280=[], doors=[])
    for i in range(OBJ_COUNT):
        a = OBJ_TABLE + i * OBJ_SIZE
        x1, x2, y1, y2 = m.w(a + 0x10), m.w(a + 0x12), m.w(a + 0x14), m.w(a + 0x16)
        if x2 <= x1 or y2 <= y1 or not m.w(a):
            continue
        kind = (m.w(a + 0x20) << 16) | m.w(a + 0x22)
        r = m.raw
        touch = r[a + 0x1D] == 0 and not r[a + 0x1E] & 8 and r[a + 0x1F] != 5 and not (r[a + 0x1F] == 6 and r[a + 0x3F] >= 5)
        if r[a + 0x1E] & 0x80 and kind not in EXPLOSION_KINDS:      # bit 7 of +$1E: the game's shot test skips it (a vehicle inside a closed tunnel stretch, $36280: $88)
            s.shielded.add(i)
        s.enemies[i] = ((x1 + x2) / 2 - s.scroll, (y1 + y2) / 2, (x2 - x1) / 2, (y2 - y1) / 2, m.w(a + 0x18), kind, r[a + 0x1C] // 2, touch)
    for i in range(GROUND_COUNT):
        a = GROUND_TABLE + i * GROUND_SIZE
        x1, x2, y1, y2 = m.w(a + 0x30), m.w(a + 0x32), m.w(a + 0x34), m.w(a + 0x36)
        gfx, hp = m.w(a + 0x0E), m.raw[a + 0x1C]
        if m.w(a) and gfx == DOOR_GFX and y2 > y1 < 0xF000:
            s.doors.append((x1 - s.scroll, y1, DOOR_SIZE, y2 - y1))
        if not m.w(a) or x2 <= x1 or y2 <= y1 or hp & 0x80:    # free slot, or destroyed (hp < 0)
            continue
        if not (IGNORE_A280 and gfx == A280_GFX):
            s.ground.append(((x1 + x2) / 2 - s.scroll, (y1 + y2) / 2, hp, i))
        if gfx != DOOR_GFX:                # every live ground object: never stay right over one (they fire point-blank)
            s.ground_boxes.append(((x1 + x2) / 2 - s.scroll, (y1 + y2) / 2, 0.0, 0.0, (x2 - x1) / 2, (y2 - y1) / 2,
                                   OBJ_HIT_X, OBJ_HIT_Y, OBJ_HIT_DY))
        if gfx == A280_GFX:
            s.a280.append(((x1 + x2) / 2 - s.scroll, (y1 + y2) / 2, hp, (x2 - x1) / 2, (y2 - y1) / 2, i))
        elif gfx == STRUCT_GFX or gfx in TOUGH_GFX:
            (s.structs if gfx == STRUCT_GFX else s.tough).append(((x1 + x2) / 2 - s.scroll, (y1 + y2) / 2, hp, (x2 - x1) / 2, (y2 - y1) / 2))
    bombing = m.raw[BOMB_TIMER] != 0
    for ch in range(4):
        for hs, vs, h, a in sprite_images(m, ch):
            if bombing and h == BOMB_SPRITE_H:
                continue       # our own bomb going off, not bullets
            if h <= 10:        # the usual bullets: 7 lines
                s.bullets.append((hs + SPR_DX + 4, vs + SPR_DY + h / 2, BULLET_HIT, BULLET_HIT))
            elif h <= 16:      # the homing "arrows": 12 lines, drawn diagonally
                box = sprite_box(m, a, h)
                if box:
                    c0, c1, r0, r1 = box
                    s.bullets.append((hs + SPR_DX + (c0 + c1 + 1) / 2, vs + SPR_DY + (r0 + r1 + 1) / 2, (c1 - c0 + 1) / 2 + 1, (r1 - r0 + 1) / 2 + 1))
    return s


def solid(flags, kind):
    """An air object the ship can crash into. Flags $FFxx is NOT a dead state (a green $373E0 in it still kills): only the kind
    tells an explosion apart."""
    return (bool(flags) or kind in FLAGS0_SOLID) and kind not in EXPLOSION_KINDS and kind not in ITEM_KINDS


def in_play(ship):
    """The ship is in the area the bot steers it in (the same check as Brain.decide); before the game is in memory the
    RAM holds anything, so log lines about bombs and weapons only count from here."""
    return bool(ship) and X_MIN - STEP <= ship[0] <= X_MAX + STEP and Y_MIN - STEP <= ship[1] <= Y_MAX + 3 * STEP


def on_screen(x, y):
    return X_MIN <= x <= X_MAX + SHIP_W and SCREEN_TOP <= y <= Y_MAX + SHIP_H + 20


def align_x(pf):
    """Ship centre X (screen) that lines the ship up with playfield X pf once the camera has followed it; None if out of reach."""
    left = (pf - SHIP_W / 2 - SCROLL_BASE) / (1 + SCROLL_SLOPE)
    sc = SCROLL_SLOPE * left + SCROLL_BASE
    if sc < 0:
        left = pf - SHIP_W / 2
    elif sc > SCROLL_MAX:
        left = pf - SCROLL_MAX - SHIP_W / 2
    return left + SHIP_W / 2 if X_MIN <= left <= X_MAX else None


def ground_aim(x, y, scroll):
    """Screen X to line up with a ground object at screen (x, y); when it is cut off at a side, where the scroll will bring it."""
    if not SCREEN_TOP <= y <= Y_MAX + SHIP_H + 20:
        return None
    return x if X_MIN <= x <= X_MAX + SHIP_W else align_x(x + scroll)


def bullet_velocity(x, y, history):
    """history: {age in frames: state of that frame}. First one game tick back (2 frames: a bullet moves 2-4 px, its
    neighbours in a stream are 10-15 px away), then refined over 4 and 6 frames by looking where the bullet SHOULD have
    been: the nearest bullet 6 frames ago is often the neighbour in a stream (the crab claws), which read as standing still."""
    past = history.get(2)
    if past is None:
        return 0.0, 0.0
    near = min(past.bullets, key=lambda b: abs(b[0] - x) + abs(b[1] - y), default=None)
    if not near or abs(near[0] - x) + abs(near[1] - y) >= 10:
        return 0.0, 0.0
    vx, vy = (x - near[0]) / 2, (y - near[1]) / 2
    for age in (4, 6):
        past = history.get(age)
        if past is None:
            break
        px, py = x - vx * age, y - vy * age
        near = min(past.bullets, key=lambda b: abs(b[0] - px) + abs(b[1] - py), default=None)
        if not near or abs(near[0] - px) + abs(near[1] - py) > 6:
            break
        vx, vy = (x - near[0]) / age, (y - near[1]) / age
    return vx, vy


def bullet_is_new(x, y, history):
    """True if no bullet was near this one 2 frames ago: its speed cannot be measured yet (bullet_velocity gives 0, 0)."""
    past = history.get(2)
    if past is None:
        return False
    return not any(abs(b[0] - x) + abs(b[1] - y) < 10 for b in past.bullets)


def enemy_velocity(i, x, y, history):
    for age in VEL_AGES:
        past = history.get(age)
        if past and i in past.enemies:
            px, py = past.enemies[i][:2]
            if abs(x - px) + abs(y - py) < 8 * age:           # a slot reused by another object is not a movement
                return (x - px) / age, (y - py) / age
    return 0.0, 0.0


# ---------------------------------------------------------------- the brain
def _arr(rows, cols=6):
    return np.array(rows, dtype=F4).reshape(-1, cols)


_PAD_F = np.full((NY + 2, NX + 2), np.inf, F4)     # reused padded buffers (avoids np.pad on every call); the border never changes
_PAD_B = np.zeros((NY + 2, NX + 2), bool)
_ROW_F = np.empty((NY + 2, NX), F4)
_ROW_B = np.empty((NY + 2, NX), bool)


def _spread_min(q):
    """For every cell, the cheapest of itself and its 8 neighbours (moving costs MOVE_COST).
    The 3x3 minimum is done as rows then columns; min(q, min3x3 + MOVE_COST) is exactly the plain 9-neighbour minimum, because the
    rounding of x + MOVE_COST never reverses an order and q + MOVE_COST >= q."""
    p = _PAD_F
    p[1:-1, 1:-1] = q
    r = _ROW_F
    np.minimum(p[:, :-2], p[:, 1:-1], out=r)
    np.minimum(r, p[:, 2:], out=r)
    m = np.minimum(r[:-2], r[1:-1])
    np.minimum(m, r[2:], out=m)
    m += MOVE_COST
    return np.minimum(q, m)


def _spread_any(r):
    p = _PAD_B
    p[1:-1, 1:-1] = r
    w = _ROW_B
    np.logical_or(p[:, :-2], p[:, 1:-1], out=w)
    w |= p[:, 2:]
    out = w[:-2] | w[1:-1]
    out |= w[2:]
    return out


class Brain:
    def __init__(self, want=None):
        self.want = want                   # set of colours worth taking, or None = any
        self.a280_slot, self.a280_sticky = None, False   # the A280 being hunted; sticky: keep it until it dies or leaves instead of choosing again every frame
        self.a280_focus, self.level = True, None     # hunt the A280 (False: --no-a280-focus); weapon level, set by decide
        self.tracked = []                  # bullets seen lately: dicts x y vx vy hw hh seen
        self.scrolls = collections.deque(maxlen=7)
        self.last = (0, 0)
        self.trap_streak = self.plan_streak = 0
        self.door_until, self.door_done, self.door_seen, self.door_mis = -1, False, -10**6, 0
        self.boss_until, self.boss_mis = -1, 0
        self.door_clear = self.door_busy = False
        self.launch_done, self.launch_seen, self.launch_mis = False, -10**6, 0
        self.info = {}

    # ---- what is out there
    def _movers(self, s, history, n, weapon):
        """hard = what kills (rows x y vx vy hw hh + the ship's half width, half height and centre offset to test them against),
        shooters = what can fire from close (rows x y vx vy hw hh), wanted / unwanted pick-ups."""
        hard, shooters, wanted, unwanted = [], [], [], []
        for i, (x, y, hw, hh, flags, kind, colour, touch) in s.enemies.items():
            if kind in ITEM_KINDS:
                vx, vy = enemy_velocity(i, x, y, history)
                ok = self.want is None or colour in self.want or colour == BOMB_ITEM or colour == weapon   # the M and our own colour always
                (wanted if ok else unwanted).append((x, y, vx, vy, 0, 0))
                continue
            vx, vy = enemy_velocity(i, x, y, history)
            if touch:                      # the game's own collision test, with its own (smaller) ship box
                hard.append((x, y, vx, vy, hw, hh, OBJ_HIT_X, OBJ_HIT_Y, OBJ_HIT_DY))
            if flags & 0x8000 and not touch:      # a destroyed hulk the game no longer collides with ($50F90, flags $FF33): not a shooter
                continue
            if solid(flags, kind) and X_MIN - 40 <= x <= X_MAX + SHIP_W + 40 and SCREEN_TOP - 40 <= y <= Y_MAX + SHIP_H + 60:
                small = hw < SMALL_HALF and hh < SMALL_HALF
                shooters.append((x, y, vx, vy, hw, hh, (PROX_R_RISER if vy < RISER_VY else PROX_R_SMALL) if small else PROX_R))
        cur = []
        scx, scy = s.ship[0] + SHIP_W / 2, s.ship[1] + SHIP_H / 2 + SHIP_HIT_DY     # the ship's hit centre
        for x, y, bhw, bhh in s.bullets:
            vx, vy = bullet_velocity(x, y, history)
            dx, dy = scx - x, scy - y
            dist = (dx * dx + dy * dy) ** 0.5
            if vx == 0.0 and vy == 0.0 and 0 < dist < NEW_BULLET_R and bullet_is_new(x, y, history):
                vx, vy = dx / dist * NEW_BULLET_SPEED, dy / dist * NEW_BULLET_SPEED   # just fired, speed unknown: assume aimed at us
            hard.append((x, y, vx, vy, bhw, bhh, SHIP_HIT_X, SHIP_HIT, SHIP_HIT_DY))
            cur.append({"x": x, "y": y, "vx": vx, "vy": vy, "hw": bhw, "hh": bhh, "seen": n})
        lost = []                          # a bullet can drop out of the sprites for a couple of frames: keep it where it should be
        for t in self.tracked:
            dt = n - t["seen"]
            if not 1 <= dt <= GHOST_FRAMES:
                continue
            px, py = t["x"] + t["vx"] * dt, t["y"] + t["vy"] * dt
            if any(abs(b["x"] - px) + abs(b["y"] - py) < 8 + 3 * dt for b in cur):
                continue
            if X_MIN - 40 <= px <= X_MAX + SHIP_W + 40 and SCREEN_TOP - 20 <= py <= Y_MAX + SHIP_H + 40:
                lost.append((t, px, py))
        if len(lost) < GHOST_MASS:         # many vanishing at once is a bomb or a screen clear, not a flicker
            for t, px, py in lost:
                hard.append((px, py, t["vx"], t["vy"], t["hw"], t["hh"], SHIP_HIT_X, SHIP_HIT, SHIP_HIT_DY))
                cur.append(t)
        self.tracked = cur
        for x, y, hp, hw, hh in s.structs + s.tough:     # the green structures and the tough ground ones fire a lot: keep off them too
            if on_screen(x, y):
                shooters.append((x, y, 0.0, 0.0, hw, hh, PROX_R))
        for x, y, hp, hw, hh, slot in ([] if IGNORE_A280 else s.a280):
            if on_screen(x, y):
                shooters.append((x, y, 0.0, 0.0, hw, hh, A280_FOCUS_R if slot == self.a280_slot else PROX_R))
        return _arr(hard, 9), _arr(shooters, 7), _arr(wanted), _arr(unwanted)

    # ---- what the ship wants
    def _goals(self, s, sx, sy, wanted, n):
        """(target x, weight per px, door, goal pick-up) and the fields kept for the bombs and the traces."""
        cx, cy = sx + SHIP_W / 2, sy + SHIP_H / 2
        live = [e for i, e in s.enemies.items() if i not in s.shielded and solid(e[4], e[5]) and not e[4] & 0x8000]     # hp < 0: dying, shooting it is wasted
        cand = [(x, y, hh) for x, y, hw, hh, *_ in live
                if X_MIN <= x <= X_MAX + SHIP_W and y + hh >= SCREEN_TOP - ENTER_MARGIN and y <= Y_MAX + SHIP_H + 20 and y < cy - 20]
        seen = [(cy - y, x) for x, y, hh in cand if y + hh >= SCREEN_TOP]     # already visible: these come first; one still above the screen only
        above = seen or [(cy - y, x) for x, y, hh in cand]                    # counts when nothing visible is left
        if not above:                      # ground targets only when nothing is flying
            above = [(cy - y, a) for x, y, _, _ in s.ground for a in [ground_aim(x, y, s.scroll)] if a is not None and y < cy - 20]
        target, weight = (min(above)[1], AIM) if above else (160.0 + X_MIN, AIM_IDLE)
        focus = None                       # a green structure on screen must die: it keeps dropping enemies and shooting
        for st in sorted(s.structs, key=lambda q: abs(q[0] - cx) + 6 * q[2]):
            a = ground_aim(st[0], st[1], s.scroll)
            if a is not None:
                target, weight, focus = a, AIM_STRUCT, st
                break
        hunted = None
        if focus is None and self.a280_focus and self.level is not None and self.level >= A280_LEVEL:    # the A280 beat the flying enemies
            cands = [q for q in s.a280 if ground_aim(q[0], q[1], s.scroll) is not None and q[1] < cy - 20]
            if self.a280_sticky and self.a280_slot is not None:
                hunted = next((q for q in cands if q[5] == self.a280_slot), None)
            if hunted is None and cands:
                hunted = min(cands, key=lambda q: abs(q[0] - cx) + 6 * q[2])
            if hunted is not None:
                target, weight, focus = ground_aim(hunted[0], hunted[1], s.scroll), AIM_A280, hunted[:5]
        self.a280_slot = None if hunted is None else hunted[5]
        boss = [e for e in live if e[2] >= BIG and e[1] + e[3] >= SCREEN_TOP - ENTER_MARGIN]     # boss pieces: under the biggest
        boss_x = None
        if boss:                           # a live wing first: the head cannot be hurt while one is left
            wings = [e for e in boss if e[5] == BOSS_KIND and abs(2 * e[2] - BOSS_WING_W) <= 4]
            boss_x = (min(wings, key=lambda e: abs(e[0] - cx)) if wings else max(boss, key=lambda e: e[2] * e[3]))[0]
            target, weight = boss_x, (AIM_BOSS_BOMB if n < self.boss_until else AIM_BOSS)
        door = None                        # the nearest door on screen beats pick-ups and targets
        for x1, y1, w, h in s.doors:
            if on_screen(x1 + w / 2, y1 + h / 2) or y1 > SCREEN_TOP - 120:
                d = (x1 + w / 2, y1 + h / 2 + DOOR_BELOW)
                if d[1] < cy - DOOR_ENGAGE:    # still far above: keep killing, do not park under it yet
                    continue
                if door is None or abs(d[0] - cx) + abs(d[1] - cy) < abs(door[0] - cx) + abs(door[1] - cy):
                    door = d
        self.door_clear = False            # close to the door with nothing in the way: rush straight in, no bomb
        self.door_busy = False
        if door and abs(door[1] - cy) < DOOR_NEAR and not self.door_done:
            lo_x, hi_x = min(cx, door[0]) - DOOR_CORRIDOR, max(cx, door[0]) + DOOR_CORRIDOR     # only what lies between the ship and the door
            lo_y, hi_y = door[1] - 30, cy + 10
            self.door_busy = (any(lo_x <= b[0] <= hi_x and lo_y <= b[1] <= hi_y for b in s.bullets)
                              or any(solid(e[4], e[5]) and lo_x <= e[0] <= hi_x and lo_y <= e[1] <= hi_y for e in s.enemies.values()))
            self.door_clear = not self.door_busy
        if door:
            self.door_seen = n
        elif n - self.door_seen > 60:
            self.door_done, self.door_mis = False, 0
        low = [w for w in wanted if on_screen(w[0], w[1]) and w[1] > ITEM_LOW]      # a pick-up only counts once it has come down
        goal = min(low, key=lambda w: abs(w[0] - cx) + abs(w[1] - cy)) if low else None
        self.info.update(target=target if (above or focus or boss) else None, boss_x=boss_x, door=door,
                         goal=None if goal is None else (float(goal[0]), float(goal[1])))
        return target, weight, door, goal

    def _aim_field(self, sx, sy, target, weight, door, goal, n):
        a = (0.0 if self.door_clear else 2.0) * np.maximum(0, Y_HIGH - GY)[:, None] + np.zeros((NY, NX), F4)     # enemies come in from the top: stay low (not when going straight to a clear door)
        if door and (self.door_clear or self.door_done or n < self.door_until):   # only a clear way (straight in) or right after the bomb (rush) pulls: never park under it
            rush = DOOR_RUSH_PULL if (n < self.door_until or self.door_clear or self.door_done) else 1.0
            a += np.abs(CX - door[0])[None, :] * DOOR_PULL_X * rush
            if door[1] >= sy + SHIP_H / 2 - 6 or self.door_clear:
                a += np.abs(CY - door[1])[:, None] * DOOR_PULL_Y * rush
            else:
                a += np.abs(GY - Y_PREF)[:, None] * AIM
        elif goal is not None:
            a += AIM * (np.abs(CX - (goal[0] + goal[2] * 8))[None, :] + np.abs(CY - (goal[1] + goal[3] * 8))[:, None])
        else:
            a += np.abs(GY - Y_PREF)[:, None] * AIM + np.abs(CX - target)[None, :] * weight
        return a

    # ---- the plan
    def decide(self, s, history, n, weapon=None, level=None):
        """Returns ((left, right, up, down), info). info['bomb'] is the reason to throw one now, or None."""
        sx, sy = s.ship[0], s.ship[1]
        self.level = level
        self.info = info = {"bomb": None, "trap": None}
        hard, shooters, wanted, unwanted = self._movers(s, history, n, weapon)
        target, weight, door, goal = self._goals(s, sx, sy, wanted, n)
        if not (X_MIN - STEP <= sx <= X_MAX + STEP and Y_MIN - STEP <= sy <= Y_MAX + 3 * STEP):     # some starts are at y = 432
            return (False,) * 4, info      # flying in, or a transition: not ours to steer
        ix = int(np.clip(round((sx - X_MIN) / STEP), 0, NX - 1))
        iy = int(np.clip(round((sy - Y_MIN) / STEP), 0, NY - 1))
        self.scrolls.append(float(s.scroll))
        scroll0 = self.scrolls[-1]
        srate = (self.scrolls[-1] - self.scrolls[0]) / max(1, len(self.scrolls) - 1)      # px per frame the camera is moving already
        cam_goal = np.clip(SCROLL_SLOPE * GX + SCROLL_BASE, 0.0, SCROLL_MAX)              # where the camera heads for, per ship column
        aim = self._aim_field(sx, sy, target, weight, door, goal, n)
        wall = (WALL_W * np.clip(1 - np.minimum(GX - X_MIN, X_MAX - GX) / WALL_R, 0, None) ** 2)[None, :].astype(F4)   # per column

        if plan_nb is not None:
            # NUMBA: the per-tick costs, the danger, the wall and the backward pass in plan_nb.plan, which repeats the
            # numpy operations one value at a time with the same types and order.
            T = HORIZON
            ts = range(1, T + 1)
            kf = np.array([TICK * t for t in ts], F4)
            shiftT = (np.minimum(np.maximum((cam_goal - scroll0)[None, :], np.array([-SCROLL_RATE * TICK * t for t in ts], F4)[:, None]),
                                 np.array([SCROLL_RATE * TICK * t for t in ts], F4)[:, None])
                      - np.array([srate * (TICK * t) for t in ts], F4)[:, None])
            grounds = np.array(s.ground_boxes, F4).reshape(-1, 9)
            TE = min(T, max(TRAP_TICKS, len(BOMB_MOVES)))
            if len(wanted):
                lowT = wanted[:, 1][None, :] + wanted[:, 3][None, :] * kf[:, None] > ITEM_LOW - 20
            else:
                lowT = np.zeros((T, 0), bool)
            c3, ex3, q, danger = plan_nb.plan(
                np.ascontiguousarray(RUN * aim, F4), np.ascontiguousarray(TERM * aim, F4), np.ascontiguousarray(wall[0], F4), kf, shiftT, CX, CY,
                np.ascontiguousarray(hard, F4), np.abs(hard[:, 2]), np.abs(hard[:, 3]), np.array([1 + GROW * t for t in ts], F4), F4(NEAR_PX),
                np.array([HIT_BASE + HIT_SOON * HIT_DECAY ** t for t in ts]), np.array([NEAR_COST * NEAR_DECAY ** t for t in ts]),
                np.ascontiguousarray(shooters, F4), F4(PROX_W),
                grounds, F4(GROUND_MARGIN), float(GROUND_W),
                np.ascontiguousarray(unwanted, F4), F4(ITEM_HIT + 6 - SHIP_HIT_X), F4(ITEM_HIT + 6 - SHIP_HIT), float(UNWANTED),
                np.ascontiguousarray(wanted, F4), np.ascontiguousarray(lowT), F4(ITEM_HIT - SHIP_HIT_X), F4(ITEM_HIT - SHIP_HIT),
                np.array([ITEM_REWARD * ITEM_DECAY ** t for t in ts]), bool(lowT.any()),
                F4(SHIP_HIT_X), F4(SHIP_HIT), F4(SHIP_HIT_DY), F4(MOVE_COST), TE, iy, ix)
            exact = [None] + list(ex3)         # per tick, 1-based
        else:
            def box(o, k, shift, ix_, iy_):    # [NY, NX]: cells where the ship's hit box overlaps any row of o, k frames ahead
                if o.shape[1] > 6:             # rows that carry the ship's box to use against them
                    sx_, sy_, sdy = o[:, 6], o[:, 7], o[:, 8]
                else:
                    sx_, sy_, sdy = SHIP_HIT_X, SHIP_HIT, np.full(len(o), SHIP_HIT_DY, F4)
                dx = np.abs((o[:, 0] + o[:, 2] * k)[:, None] - shift[None, :] - CX[None, :])
                dy = np.abs((o[:, 1] + o[:, 3] * k - sdy)[:, None] - CY[None, :])
                return (dy < (o[:, 5] + sy_ + iy_)[:, None]).astype(F4).T @ (dx < (o[:, 4] + sx_ + ix_)[:, None]).astype(F4) > 0

            grounds = np.array(s.ground_boxes, F4).reshape(-1, 9)
            cost, exact = [None], [None]       # per tick, 1-based
            danger = 0.0                       # the threat (PROX + NEAR) where the ship is now, along the horizon
            vx, vy = np.abs(hard[:, 2]), np.abs(hard[:, 3])
            for t in range(1, HORIZON + 1):
                k = TICK * t
                shift = np.clip(cam_goal - scroll0, -SCROLL_RATE * k, SCROLL_RATE * k) - srate * k   # how far everything slides, per ship column
                c = RUN * aim
                threat = np.zeros((NY, NX), F4)    # how hot each cell is: the wall only matters when fleeing from something
                if len(hard):
                    exact.append(box(hard, k, shift, 0.5 * vx, 0.5 * vy))                      # the boxes as measured: decides the bombs
                    g = 1 + GROW * t                                                            # widened by a frame of travel and by time
                    hit = box(hard, k, shift, vx + g, vy + g)
                    ring = box(hard, k, shift, vx + g + NEAR_PX, vy + g + NEAR_PX)
                    near = (ring & ~hit) * (NEAR_COST * NEAR_DECAY ** t)
                    c = c + hit * (HIT_BASE + HIT_SOON * HIT_DECAY ** t) + near
                    threat = threat + near
                else:
                    exact.append(np.zeros((NY, NX), bool))
                if len(shooters):              # distance from the ship's centre to each shooter's box
                    ex = np.maximum(np.abs((shooters[:, 0] + shooters[:, 2] * k)[:, None] - shift[None, :] - CX[None, :]) - shooters[:, 4, None], 0)
                    ey = np.maximum(np.abs((shooters[:, 1] + shooters[:, 3] * k)[:, None] - CY[None, :]) - shooters[:, 5, None], 0)
                    d = np.sqrt(ex[:, None, :] ** 2 + ey[:, :, None] ** 2)
                    prox = (PROX_W * np.clip(1 - d / shooters[:, 6, None, None], 0, None) ** 2).sum(0)
                    c = c + prox
                    threat = threat + prox
                danger = max(danger, float(threat[iy, ix]))
                if len(grounds):               # right over a live ground object: it fires point-blank
                    c = c + box(grounds, k, shift, GROUND_MARGIN, GROUND_MARGIN) * GROUND_W
                if len(unwanted):
                    c = c + box(unwanted, k, shift, ITEM_HIT + 6 - SHIP_HIT_X, ITEM_HIT + 6 - SHIP_HIT) * UNWANTED
                if len(wanted):
                    low = wanted[wanted[:, 1] + wanted[:, 3] * k > ITEM_LOW - 20]
                    if len(low):
                        c = c - box(low, k, shift, ITEM_HIT - SHIP_HIT_X, ITEM_HIT - SHIP_HIT) * (ITEM_REWARD * ITEM_DECAY ** t)
                cost.append(c.astype(F4))
            danger = min(1.0, danger / PROX_W)     # the wall only matters when the ship has something to flee from
            for t in range(1, HORIZON + 1):
                cost[t] = cost[t] + wall * danger

            v = TERM * aim                     # backwards: the cheapest way on from every cell
            for t in range(HORIZON, 1, -1):
                v = _spread_min(cost[t] + v)
            q = cost[1] + v                    # being at each cell on the next tick, and on from there
        best, move = np.inf, (0, 0)
        for dx, dy in NEIGH:
            if 0 <= ix + dx < NX and 0 <= iy + dy < NY:
                val = q[iy + dy, ix + dx] + (MOVE_COST if (dx, dy) != (0, 0) else 0) - (KEEP if (dx, dy) == self.last and self.last != (0, 0) else 0)
                if val < best:
                    best, move = val, (dx, dy)
        self.last = move
        info["cost"] = float(best)

        reach = np.zeros((NY, NX), bool)   # forwards: is there any hit-free path at all?
        reach[iy, ix] = True
        for t in range(1, TRAP_TICKS + 1):
            reach = _spread_any(reach) & ~exact[t]
            if not reach.any():
                info["trap"] = t
                break
        self.trap_streak = self.trap_streak + 1 if info["trap"] else 0
        best = None                        # would the bomb's own circle get hit before it goes off? The circle (anticlockwise,
        for k in range(len(CIRCLE)):       # full) can start at any of its 4 points: take the one hit last, U first on a tie
            moves = [CIRCLE[(k + j) % len(CIRCLE)] for j in range(len(BOMB_MOVES))]
            gx, gy, hit = ix, iy, None
            for t, mv in enumerate(moves, 1):
                gx = min(max(gx + (mv == "R") - (mv == "L"), 0), NX - 1)
                gy = min(max(gy + (mv == "D") - (mv == "U"), 0), NY - 1)
                if exact[t][gy, gx]:
                    hit = t
                    break
            score = len(moves) + 1 if hit is None else hit
            if best is None or score > best[0]:
                best = (score, hit, moves)
        gesture = best[1]
        info["gesture_moves"] = best[2]

        cx, cy = sx + SHIP_W / 2, sy + SHIP_H / 2
        if info["trap"] and info["trap"] >= TRAP_MIN and self.trap_streak >= TRAP_CONFIRM and (gesture is None or gesture >= info["trap"]):
            info["bomb"] = f"trapped: no way past tick {info['trap']}"
        self.plan_streak = self.plan_streak + 1 if info["cost"] >= PLAN_HIT else 0
        if not info["bomb"] and self.plan_streak >= PLAN_CONFIRM and (gesture is None or gesture >= len(BOMB_MOVES)):
            info["bomb"] = f"plan: hit on every way (cost {info['cost']:.0f})"
        boss_x = info["boss_x"]            # the boss: bullets keeping the ship from under it for too long
        self.boss_mis = self.boss_mis + 1 if (boss_x is not None and abs(boss_x - cx) > BOSS_LINE and len(s.bullets) >= BOSS_BULLETS) else 0
        if self.boss_mis >= BOSS_WAIT and not info["bomb"]:
            info["bomb"] = "boss: clearing the way under it"
        if door and abs(door[1] - cy) < DOOR_NEAR and not self.door_done:     # the door: one bomb, if the way to it stays busy
            self.door_mis = self.door_mis + 1 if (self.door_busy and abs(door[1] - cy) < DOOR_BOMB) else 0
            if self.door_mis >= DOOR_WAIT and not info["bomb"]:
                info["bomb"] = "door: clearing the way to it"
        launchers = [e for e in s.enemies.values() if e[5] == LAUNCHER_KIND and not e[4] & 0x8000 and on_screen(e[0], e[1])]
        if launchers:
            self.launch_seen = n
        elif n - self.launch_seen > 60:
            self.launch_done, self.launch_mis = False, 0
        if launchers and level is not None and level <= LOW_LEVEL and not self.launch_done:   # weak gun: the pod is worth a bomb
            lx, ly = min(launchers, key=lambda e: abs(e[0] - cx) + abs(e[1] - cy))[:2]
            lo_x, hi_x = min(cx, lx) - DOOR_CORRIDOR, max(cx, lx) + DOOR_CORRIDOR       # only what lies between the ship and the launcher
            lo_y, hi_y = ly - 30, cy + 10
            cut = (any(lo_x <= b[0] <= hi_x and lo_y <= b[1] <= hi_y for b in s.bullets)
                   or any(solid(e[4], e[5]) and e[5] != LAUNCHER_KIND and lo_x <= e[0] <= hi_x and lo_y <= e[1] <= hi_y for e in s.enemies.values()))
            self.launch_mis = self.launch_mis + 1 if (cut and info["cost"] >= LAUNCH_COST and abs(ly - cy) < LAUNCH_NEAR and abs(lx - cx) > LAUNCH_LINE) else 0
            if self.launch_mis >= LAUNCH_WAIT and not info["bomb"]:
                info["bomb"] = "launcher: weak gun, bombing it for the pod"
        return (move[0] < 0, move[0] > 0, move[1] < 0, move[1] > 0), info

    def bomb_thrown(self, n, reason):
        self.tracked = []                  # it clears the bullets: no ghosts
        self.trap_streak = self.plan_streak = 0
        if reason.startswith("door") or self.info.get("door"):
            self.door_until, self.door_done, self.door_mis = n + DOOR_RUSH, True, 0
        if reason.startswith("launcher"):
            self.launch_done, self.launch_mis = True, 0
        if reason.startswith("boss"):
            self.boss_until, self.boss_mis = n + BOSS_WINDOW, 0


# ---------------------------------------------------------------- running it
def save_trace(trace, number, frame, prefix="death"):
    """Saves the latest frames: deaths/<prefix>_NNN_frameF_HHMMSS.json (frame, joystick, ship) and ..._ram.npz (chip RAM).
    The compression takes ~3 s and the emulator waits for the bot every frame: it is done in a thread so the game does not freeze."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deaths")
    os.makedirs(d, exist_ok=True)
    name = f"{prefix}_{number:03d}_frame{frame}_{time.strftime('%H%M%S')}"
    rows = list(trace)                 # the deque keeps changing: work on a snapshot (the RAM images themselves are never modified)

    def write():
        np.savez_compressed(os.path.join(d, name + "_ram.npz"), ram=np.stack([np.frombuffer(r, np.uint8) for _, r, _, _ in rows]),
                            frames=np.array([t[0] for t in rows]))
        with open(os.path.join(d, name + ".json"), "w") as f:
            json.dump({"frame": frame, "frames": [{"n": t[0], "joy": t[2], "ship": t[3] and list(t[3][:2])} for t in rows]}, f)
    threading.Thread(target=write, name="save_trace").start()
    return name


def main():
    ap = argparse.ArgumentParser(description="Battle Squadron bot: console loop for a WinUAE already running with LinkUAE (see restart.ps1).")
    ap.add_argument("--frames", type=int, default=3000, help="frames to play before stopping (default 3000, about a minute)")
    ap.add_argument("--press-p", action="store_true", help="press P once at the start: for a savestate saved paused")
    ap.add_argument("--pause-at-end", action="store_true", help="pause the game with P when the run ends (after a game over it is always paused)")
    ap.add_argument("--quiet", action="store_true", help="fewer console lines (no status and new-kind lines)")
    ap.add_argument("--no-bombs", action="store_true", help="never throw bombs")
    ap.add_argument("--idle", type=int, default=0, help="play nothing for this many frames at the start (a different start, for tests)")
    ap.add_argument("--a280-aim", type=float, help="aim weight of the hunt (default 0.8; 1.0 = green structures, 1.2 = boss)")
    ap.add_argument("--a280-focus-r", type=float, help="fear radius of the A280 being hunted (default 64 = like the others)")
    ap.add_argument("--a280-sticky", action="store_true", help="keep hunting the same A280 until it dies or leaves")
    ap.add_argument("--no-ram-traces", action="store_true", help="do not save the RAM traces of bombs and deaths (the log lines stay; mark.flag still works)")
    ap.add_argument("--ignore-a280", action="store_true", help="the A280 do not exist for the planner: no fear, no hunt, not even as a ground target")
    ap.add_argument("--no-a280-focus", action="store_true", help="do not hunt the A280 ground enemies")
    ap.add_argument("--lives", type=int, default=0, help="stop and let go of the controls after this many deaths (no continues); 0 = never")
    ap.add_argument("--no-items", action="store_true", help="ignore the pick-ups")
    ap.add_argument("--want", help="weapon colours worth taking, e.g. orange or red,green (default: any)")
    ap.add_argument("--mem-log", nargs=3, metavar=("FILE", "FROM", "TO"), help="save chip RAM every frame from FROM to TO into FILE (.npy)")
    ap.add_argument("--record", action="store_true", help="screen recording: no output, no traces, no bookkeeping that only serves the logs (the bot plays the same)")
    ap.add_argument("--trace", type=int, default=0, help="print the ship and its plan every N frames")
    ap.add_argument("--objs", type=int, default=0, help="print the object table every N frames")
    args = ap.parse_args()
    if args.mem_log:
        args.mem_log = [args.mem_log[0], int(args.mem_log[1]), int(args.mem_log[2])]

    rec = args.record
    say = (lambda *a, **k: None) if rec else print
    brain = Brain({COLOURS[c] for c in args.want.split(",")} if args.want else None)
    brain.a280_focus = not (args.no_a280_focus or args.ignore_a280)
    globals()["IGNORE_A280"] = args.ignore_a280
    if args.a280_aim is not None:
        globals()["AIM_A280"] = args.a280_aim
    if args.a280_focus_r is not None:
        globals()["A280_FOCUS_R"] = args.a280_focus_r
    brain.a280_sticky = args.a280_sticky
    hist, deaths, was_dead, kinds, back_at = [], 0, False, {}, -10**6
    trace = collections.deque(maxlen=TRACE_FRAMES)      # (frame, RAM, joystick, ship) of the latest frames
    trace_joy, traces_saved, marks = {}, (MAX_TRACES if rec or args.no_ram_traces else 0), 0     # no_ram_traces: no RAM files for bombs and deaths (the log lines stay)
    last_weapon, last_bombs, last_bomb, bomb_at, bombs_thrown = None, None, -10**6, None, 0
    last_lives, out_frames = None, 0
    a280_alive, a280_killed, a280_min = set(), 0, None
    same, last_snap, paused_at, mem_rows, prev, think_ms = 0, None, None, [], None, []
    history = collections.deque(maxlen=7)               # the states of the latest frames, newest last
    with Link() as link:
        link.set_windows([(CHIP, 0x80000)])
        n, gone, game_over, bomb_moves = 0, False, False, BOMB_MOVES
        while n < args.frames:
            try:
                f = link.wait_frame(2.0)
            except EmulatorGone:      # closed by the user: keep the logs
                say(f"[{n}] the emulator closed", flush=True)
                gone = True
                break
            if f is None:
                continue
            if args.press_p and n in (1, 5):            # --press-p: P once, at the very start; a pause made by the user is never undone
                link.key(KEY_P, n == 1)
            if n >= 2:
                try:
                    raw = f.read(CHIP, 0x80000)
                except KeyError:          # the memory window was dropped: ask for it again, skip the frame
                    say(f"[{n}] the RAM window was gone: asking for it again", flush=True)
                    link.set_windows([(CHIP, 0x80000)])
                    link.step()
                    n += 1
                    continue
                m = Mem(raw)
                s = read_state(m)
                if args.no_items:
                    s.enemies = {i: e for i, e in s.enemies.items() if e[5] not in ITEM_KINDS}
                ship, enemies, bullets = s.ship, s.enemies, s.bullets
                trace.append((n, None if rec else m.raw, trace_joy.get(n - 1), ship))
                if not rec and n % 5 == 0 and os.path.exists(MARK_FLAG):      # the user saw something odd: keep the last 2 s as they are
                    try:
                        os.remove(MARK_FLAG)
                    except OSError:
                        pass
                    marks += 1
                    say(f"[{n}] MARK: trace saved:", save_trace(trace, marks, n, "mark"), flush=True)
                if ship and prev and not rec:         # a pick-up that was there last frame and is gone with the ship on top of it: taken
                    for i, pe in prev.enemies.items():
                        if (pe[5] in ITEM_KINDS and (i not in enemies or enemies[i][5] not in ITEM_KINDS)
                                and abs(pe[0] - ship[0] - SHIP_W / 2) < 40 and abs(pe[1] - ship[1] - SHIP_H / 2) < 40):
                            say(f"[{n}] item taken (colour {pe[6]})", flush=True)
                for i, e in (() if rec else enemies.items()):
                    if e[5] not in kinds:
                        kinds[e[5]] = e
                        if not args.quiet:
                            say(f"[{n}] new kind {e[5]:08X} flags={e[4]:04X} size={2 * e[2]:.0f}x{2 * e[3]:.0f} at ({e[0]:.0f},{e[1]:.0f})")
                for i in range(GROUND_COUNT):        # the A280 killed: the slot turns to hp >= $80 while it was alive (leaving the screen only frees it)
                    a = GROUND_TABLE + i * GROUND_SIZE
                    if m.w(a) and m.w(a + 0x0E) == A280_GFX:
                        if m.raw[a + 0x1C] & 0x80:
                            if i in a280_alive:
                                a280_alive.discard(i)
                                a280_killed += 1
                                say(f"[{n}] A280 killed (slot {i}); {a280_killed} so far", flush=True)
                        else:
                            a280_alive.add(i)
                    else:
                        a280_alive.discard(i)
                if ship and not ship[2]:             # how close the ship gets to the box of an A280 (centre to edge)
                    for q in s.a280:
                        ddx = max(abs(q[0] - ship[0] - SHIP_W / 2) - q[3], 0)
                        ddy = max(abs(q[1] - ship[1] - SHIP_H / 2) - q[4], 0)
                        dd = (ddx * ddx + ddy * ddy) ** 0.5
                        if a280_min is None or dd < a280_min:
                            a280_min = dd
                joy, fire = (False,) * 4, (n // 2) % 2 == 0
                bombs = m.raw[BOMBS]
                if in_play(ship):                                   # only real changes, not the RAM while the game loads
                    if last_bombs is not None and bombs != last_bombs:
                        say(f"[{n}] bombs {last_bombs} -> {bombs}", flush=True)
                    last_bombs = bombs
                info = {}
                if bomb_at is not None:   # throwing a bomb: follow the circle
                    step = (n - bomb_at) // BOMB_STEP
                    if step < len(bomb_moves):
                        d = bomb_moves[step]
                        joy, fire = ("L" in d, "R" in d, "U" in d, "D" in d), True
                    else:
                        bomb_at = None
                if bomb_at is None and ship and not ship[2] and n >= args.idle:
                    t0 = time.perf_counter()
                    joy, info = brain.decide(s, {a: history[-a] for a in range(1, len(history) + 1)}, n, m.raw[WEAPON], m.raw[LEVEL])
                    think_ms.append((time.perf_counter() - t0) * 1000)
                    why = info["bomb"]
                    if why and bombs and not (why.startswith('launcher') and bombs < LAUNCH_MIN_BOMBS) and not args.no_bombs and n - last_bomb >= BOMB_COOLDOWN and n - back_at >= RESPAWN_CALM and same < 5:
                        bomb_at, last_bomb, bombs_thrown = n, n, bombs_thrown + 1
                        brain.bomb_thrown(n, why)
                        bomb_moves = info.get("gesture_moves") or BOMB_MOVES
                        say(f"[{n}] bomb! ({why}; {bombs} left; circle {''.join(bomb_moves)})", flush=True)
                        say("     why:", hist[-1] if hist else None, flush=True)
                        if traces_saved < MAX_TRACES:
                            traces_saved += 1
                            say("     trace saved:", save_trace(trace, bombs_thrown, n, "bomb"), flush=True)
                        d = bomb_moves[0]
                        joy, fire = ("L" in d, "R" in d, "U" in d, "D" in d), True
                dead = not ship or ship[2]          # exploding, or out of play
                lives = m.raw[LIVES]
                if lives != last_lives:
                    say(f"[{n}] reserve lives {last_lives} -> {lives}", flush=True)
                    last_lives = lives
                out_frames = out_frames + 1 if m.w(PLAYER) == 999 else 0    # ship not in play (px = 999), straight from RAM
                if lives == 0 and out_frames == GAME_OVER_FRAMES and not game_over:
                    game_over = True
                    say(f"[{n}] GAME OVER: no reserve lives (0 left) and the ship out of play for {GAME_OVER_FRAMES} frames; letting go of the controls and pausing", flush=True)
                if was_dead and not dead:
                    back_at = n
                moved = len({tuple(t[3][:2]) for t in trace if t[3]}) > 1
                if dead and not was_dead and ship and n - back_at < ALIVE_BEFORE_DEATH:
                    pass                            # the explosion image flickering: the same death, not a new one
                elif dead and not was_dead and ship and not moved:   # a level transition shows a ship image as tall as the explosion
                    say(f"[{n}] explosion-like ship image but the ship has not moved for {len(trace)} frames: a transition, not a death", flush=True)
                elif dead and not was_dead and ship:  # a ship that just is not there (px = 999) is a transition, not a death
                    deaths += 1
                    say(f"[{n}] died at ({ship[0]},{ship[1]})", flush=True)
                    if args.lives and deaths >= args.lives:
                        game_over = True
                        say(f"[{n}] GAME OVER: {deaths} lives lost (--lives); letting go of the controls and pausing", flush=True)
                    for line in hist[-24::3]:
                        say("    ", line)
                    if same < 20 and traces_saved < MAX_TRACES:
                        traces_saved += 1
                        say("     trace saved:", save_trace(trace, deaths, n), flush=True)
                was_dead = dead
                weapon = m.raw[WEAPON]
                if in_play(ship):
                    if weapon != last_weapon:
                        say(f"[{n}] weapon {({v: k for k, v in COLOURS.items()}).get(weapon, weapon)}", flush=True)
                    last_weapon = weapon
                if ship and not rec:
                    near = sorted([(round(x - ship[0] - 16), round(y - ship[1] - 15), "b") for x, y, _, _ in bullets] +
                                  [(round(e[0] - ship[0] - 16), round(e[1] - ship[1] - 15), f"e{i}") for i, e in enemies.items()],
                                  key=lambda t: abs(t[0]) + abs(t[1]))[:4]
                    hist = (hist + [f"[{n}] ship=({ship[0]},{ship[1]}) joy={''.join(c for c, v in zip('LRUD', joy) if v)} "
                                    f"trap={info.get('trap')} near={near}"])[-60:]
                if game_over:
                    joy, fire = (False,) * 4, False
                if n < args.idle:
                    joy, fire = (False,) * 4, False            # --idle: nothing at all
                if not rec:
                    trace_joy[n] = "".join(c for c, v in zip("LRUD", joy) if v) + ("+fire" if fire else "")
                link.joy(1, left=joy[0], right=joy[1], up=joy[2], down=joy[3], fire=fire)
                prev = s
                history.append(s)
                snap = (ship, sorted(enemies.items()), bullets)
                same = same + 1 if snap == last_snap else 0
                last_snap = snap
                in_pause = PAUSE_LOOP[0] <= f.pc < PAUSE_LOOP[1]
                if n > 20 and in_pause and paused_at is None:
                    paused_at = n
                    say(f"[{n}] the game is paused (pc=${f.pc:06X}): leaving it alone", flush=True)
                elif not in_pause:
                    paused_at = None
                if args.mem_log and args.mem_log[1] <= n < args.mem_log[2]:
                    mem_rows.append(np.frombuffer(m.raw, np.uint8).copy())
                if same == 100:
                    say(f"[{n}] nothing has changed for 100 frames: game stopped? pc=${f.pc:06X} sr=${f.sr:04X}", flush=True)
                if args.objs and n % args.objs == 0:
                    say(f"[{n}] ground " + " | ".join(f"{i}: hp={hp} ({x:.0f},{y:.0f})" for x, y, hp, i in s.ground))
                    say(f"[{n}] objs " + " | ".join(f"{i}:{e[5]:05X} hp={e[4] >> 8} c={e[6]} {2 * e[2]:.0f}x{2 * e[3]:.0f} ({e[0]:.0f},{e[1]:.0f})"
                                                      for i, e in enemies.items()))
                if args.trace and n % args.trace == 0 and ship and info:
                    say(f"[{n}] ship=({ship[0]},{ship[1]}) joy={trace_joy[n]} target={info.get('target')} goal={info.get('goal')} door={info.get('door')}"
                          f" boss={info.get('boss_x')} trap={info.get('trap')} cost={info.get('cost', 0):.0f} enemies={len(enemies)} bullets={len(bullets)}")
                if n % 100 == 0 and not args.quiet:
                    say(f"[{n}] ship={ship and ship[:2]} enemies={len(enemies)} bullets={len(bullets)} deaths={deaths}")
            link.step()
            n += 1
            if game_over:
                break
        if not gone:
            link.joy(1)
        # after a game over the controls are already released (above) and P pauses the game while the ship explodes,
        # before the name entry / continue: P is the only key ever sent
        for k in range(0 if gone else (30 if game_over else (10 if args.pause_at_end else 1))):   # the game reads keys at 25 Hz: hold P 4 frames
            f = link.wait_frame(2.0)
            if f:
                if (game_over or args.pause_at_end) and k in (0, 4):
                    link.key(KEY_P, k == 0)
                if game_over and k == 29:
                    say(f"[{n}] game paused (pc=${f.pc:06X})" if PAUSE_LOOP[0] <= f.pc < PAUSE_LOOP[1]
                        else f"[{n}] WARNING: P sent but the game does not look paused (pc=${f.pc:06X}): check the screen", flush=True)
                link.step()
    if args.mem_log and mem_rows:
        np.save(args.mem_log[0], np.stack(mem_rows))
    ms = f", think {np.mean(think_ms):.1f} ms avg / {np.max(think_ms):.1f} max" if think_ms else ""
    print(f"done: {n} frames, deaths {deaths}, bombs thrown {bombs_thrown}, bombs left {last_bombs}, A280 killed {a280_killed}, "
          f"closest A280 {'-' if a280_min is None else f'{a280_min:.0f} px'}{ms}")


def warmup():
    """Compiles the planner now (about 8 s), at the panel's start, instead of on the first frame of the game.
    A throwaway brain on a made-up scene: the compiled code only depends on the types of the arrays, not on their contents."""
    if plan_nb is None:
        return 0
    s = types.SimpleNamespace(ship=(384, 410, False), scroll=0, enemies={}, shielded=set(), bullets=[(400.0, 300.0, BULLET_HIT, BULLET_HIT)],
                              ground=[], ground_boxes=[], structs=[], tough=[], a280=[], doors=[])
    Brain(None).decide(s, {}, 0, 0, 0)
    return len(plan_nb.plan.signatures)


if __name__ == "__main__":
    main()
