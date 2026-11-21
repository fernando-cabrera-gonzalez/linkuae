"""bot.py as one object the panel can drive frame by frame.

panel.py runs the bot inside its own process (two LinkUAE clients on one emulator would interfere), so it needs
`Player.step(n, frame, link, active)`. bot.py is a console script with its own loop in main(); this module repeats
that loop's decisions (Brain.decide, the bombs and their circle, the deaths and the level transitions); every
constant and every rule is read from bot.py.

step() answers on the joystick and returns what the bot saw as plain data (JSON-ready) for the panel.
"""
import collections

import bot as b

SCORE = b.PLAYER + 0x6A                 # 8 ASCII digits
WEAPON_NAMES = {v: k for k, v in b.COLOURS.items()}
TRACE_FRAMES = 48                       # frames of ship positions kept, to tell a death from a level transition


class Player:
    def __init__(self, want=None):
        self.want = want
        self.brain = b.Brain(want)
        self.history = collections.deque(maxlen=7)         # states of the latest frames, newest last
        self.trace = collections.deque(maxlen=TRACE_FRAMES)  # the ship of the latest frames
        self.deaths = self.bombs_thrown = 0
        self.was_dead = False
        self.game_over, self.go_out = False, 0              # bot.py's game over: lives 0 and the ship out of play for GAME_OVER_FRAMES frames
        self.prev_fire = False                             # the fire sent the frame before
        self.back_at = self.last_bomb = -10 ** 6
        self.bomb_at = self.last_bombs = self.last_weapon = None
        self.bomb_moves = b.BOMB_MOVES
        self.same, self.last_snap = 0, None

    def say(self, n, text):
        print(f"[{n}] {text}", flush=True)

    def step(self, n, f, link, active=True):
        try:
            raw = f.read(b.CHIP, 0x80000)
        except KeyError:                                   # the RAM window was dropped: ask for it again, skip the frame
            link.set_windows([(b.CHIP, 0x80000)])
            self.say(n, "the RAM window was gone: asking for it again")
            return None
        m = b.Mem(raw)
        s = b.read_state(m)
        snap = (s.ship, sorted(s.enemies.items()), s.bullets)
        ship = s.ship
        frozen_now = snap == self.last_snap
        bombs, weapon = m.raw[b.BOMBS], m.raw[b.WEAPON]
        joy, fire = (False,) * 4, (n // 2) % 2 == 0
        in_play = b.in_play(ship)                          # while the game loads, the RAM is not its data yet
        if in_play:                                        # compared from one frame in play to the next: changes while out of play show later
            if self.last_bombs is not None and bombs != self.last_bombs:
                self.say(n, f"bombs {self.last_bombs} -> {bombs}")
            self.last_bombs = bombs
        history = {a: self.history[-a] for a in range(1, len(self.history) + 1)}
        info = {}
        if active:
            if self.bomb_at is not None:                   # throwing a bomb: follow the circle
                step = (n - self.bomb_at) // b.BOMB_STEP
                if step < len(self.bomb_moves):
                    d = self.bomb_moves[step]
                    joy, fire = ("L" in d, "R" in d, "U" in d, "D" in d), True
                else:
                    self.bomb_at = None
            if self.bomb_at is None and ship and not ship[2]:
                self.brain.want = self.want
                joy, info = self.brain.decide(s, history, n, weapon, m.raw[b.LEVEL])
                why = info["bomb"]
                if (why and bombs and not (why.startswith('launcher') and bombs < b.LAUNCH_MIN_BOMBS)
                        and n - self.last_bomb >= b.BOMB_COOLDOWN
                        and n - self.back_at >= b.RESPAWN_CALM and self.same < 5):
                    self.bomb_at, self.last_bomb, self.bombs_thrown = n, n, self.bombs_thrown + 1
                    self.brain.bomb_thrown(n, why)
                    self.bomb_moves = info.get("gesture_moves") or b.BOMB_MOVES
                    self.say(n, f"bomb! ({why}; {bombs} left; circle {''.join(self.bomb_moves)})")
                    d = self.bomb_moves[0]
                    joy, fire = ("L" in d, "R" in d, "U" in d, "D" in d), True
        dead = not ship or ship[2]                         # exploding, or out of play
        if self.was_dead and not dead:
            self.back_at = n
        dying = dead and not self.was_dead and ship        # a ship that is just not there is a transition
        self.was_dead = dead
        if in_play:
            if weapon != self.last_weapon:
                self.say(n, f"weapon {WEAPON_NAMES.get(weapon, weapon)}")
            self.last_weapon = weapon
        self.go_out = self.go_out + 1 if m.w(b.PLAYER) == 999 else 0
        if m.raw[b.LIVES] == 0 and self.go_out == b.GAME_OVER_FRAMES and not self.game_over:
            self.game_over = True
            self.say(n, f"GAME OVER: no reserve lives (0 left) and the ship out of play for {b.GAME_OVER_FRAMES} frames; letting go of the controls")
        if self.game_over:
            joy, fire = (False,) * 4, False
        if active:
            link.joy(1, left=joy[0], right=joy[1], up=joy[2], down=joy[3], fire=fire)
        view = self.view(n, m, s, joy, fire, info, bombs, weapon, history)
        if n % 100 == 0:                                   # log only: the same status line as bot.py
            print(f"[{n}] ship={ship and ship[:2]} enemies={len(s.enemies)} bullets={len(s.bullets)} deaths={self.deaths}", flush=True)
        view["fire_prev"], self.prev_fire = self.prev_fire, fire
        self.trace.append(ship)
        self.same = self.same + 1 if frozen_now else 0
        self.last_snap = snap
        if dying and n - self.back_at < b.ALIVE_BEFORE_DEATH:
            dying = False                                  # the explosion image flickering: the same death, not a new one
        if dying:
            moved = len({tuple(t[:2]) for t in self.trace if t}) > 1
            where = f" at ({ship[0]},{ship[1]})"
            if not moved:                                  # a level transition shows a ship image as tall as the explosion
                self.say(n, f"explosion-like ship image{where} but the ship has not moved: a transition, not a death")
            else:
                self.deaths += 1
                self.say(n, f"died{where}")
            view["deaths"] = self.deaths
        self.history.append(s)
        return view

    def view(self, n, m, s, joy, fire, info, bombs, weapon, history):
        ship = s.ship
        objs = []
        for i, (x, y, hw, hh, flags, kind, colour, touch) in s.enemies.items():
            vx, vy = b.enemy_velocity(i, x, y, history)
            role = "item" if kind in b.ITEM_KINDS else "enemy" if (touch or b.solid(flags, kind)) and not flags & 0x8000 else "inert"
            objs.append({"x": round(x), "y": round(y), "hw": hw, "hh": hh, "role": role, "colour": colour,
                         "vx": round(vx, 1), "vy": round(vy, 1)})
        big = {(round(q[0]), round(q[1])): (q[3], q[4]) for q in s.structs + s.tough + [r[:5] for r in s.a280]}     # sized ground objects
        ground = []
        for x, y, hp, _slot in s.ground:
            hw, hh = big.get((round(x), round(y)), (8, 8))
            ground.append([round(x), round(y), hp, hw, hh])
        path = []
        if ship and not ship[2]:                           # the first step of the plan, held over the horizon
            dx, dy = self.brain.last
            for t in range(1, b.HORIZON + 1):
                path.append((round(min(max(ship[0] + dx * b.STEP * t, b.X_MIN), b.X_MAX) + b.SHIP_W / 2),
                             round(min(max(ship[1] + dy * b.STEP * t, b.Y_MIN), b.Y_MAX) + b.SHIP_H / 2)))
        goal = info.get("goal")
        return {
            "n": n, "ship": ship and [ship[0], ship[1]], "out": ship is None, "exploding": bool(ship and ship[2]),
            "objs": objs,
            "bullets": [[round(x), round(y), *b.bullet_velocity(x, y, history), hw, hh] for x, y, hw, hh in s.bullets],
            "ground": ground,
            "goal": goal and [round(goal[0]), round(goal[1])],
            "target": info.get("target"),
            "path": path,
            "joy": "".join(c for c, on in zip("LRUD", joy) if on) + ("F" if fire else ""),
            "score": bytes(m.raw[SCORE:SCORE + 8]).decode("latin1"),
            "bombs": bombs, "lives": m.raw[b.LIVES], "game_over": m.raw[b.LIVES] == 0 and self.go_out >= b.GAME_OVER_FRAMES,
            "weapon": WEAPON_NAMES.get(weapon, weapon), "deaths": self.deaths,
            "bombs_thrown": self.bombs_thrown, "bombing": self.bomb_at is not None,
        }
