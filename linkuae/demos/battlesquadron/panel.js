// The panel's behaviour: live data from panel.py, what the bot sees drawn from it, and the buttons.

// The game keeps its coordinates in the Amiga's sprite system: a ship sprite starts at hstart = X - 113,
// vstart = Y - 218. The visible window is set by DIWSTRT $2690 / DIWSTOP $F5B0: it starts at hstart 144, line 38
// and is 288 x 207 pixels, so a game position (X, Y) is the screen pixel (X - 258, Y - 256).
const X0 = 258, Y0 = 256;                                              // game coordinates of the screen's top-left pixel
const WIDTH = 288, HEIGHT = 207;                                       // the visible window, in pixels
const gamePos = (x, y) => `${Math.round(x)}, ${Math.round(y)}`;        // a position as the game stores it
const SHIP_W = 32, SHIP_H = 30, SHIP_HIT = 11;                         // a rough core for the view (bot.py's box: SHIP_HIT_X, SHIP_HIT)
const Y_HIGH = 340, Y_PREF = 410;                                      // the lane the ship keeps to
const PHASES = { stopped: "Stopped", booting: "Booting", running: "Running" };
const WEAPONS = { red: "#ff5a4d", blue: "#4aa3ff", green: "#3ddc84", orange: "#ffa63d" };
const WEAPON_NAMES = ["red", "blue", "green", "orange"];               // the pod's colour index, as in bot.py
const BOMB_ITEM = 5;                                                   // the "M" pick-up
const WANTS = ["any", "orange", "red", "blue", "green"];

const $ = (id) => document.getElementById(id);                         // shorthand for elements
const canvas = $("game");                                              // the entity view
const ctx = canvas.getContext("2d");                                   // its drawing context
const css = getComputedStyle(document.documentElement);                // colours live in the stylesheet
const color = (name) => css.getPropertyValue(name).trim();             // read one
const hex = (value, digits) => value.toString(16).toUpperCase().padStart(digits, "0");
const addr = (value) => "$" + hex(value, 8);                           // 32-bit values

// ---- writing to the page: only what changed, 25 times a second ----

// Sets an element's text if it differs; nil marks a placeholder. No glow or animation on a change: the page
// repaints 25 times a second, and effects cost the browser CPU that the emulator and the bot need on a slow PC.
function put(el, text, nil = false) {
  text = String(text);
  if (el.textContent === text) return;
  el.textContent = text;
  el.toggleAttribute("data-nil", nil);
}

function lamp(el, on) {                                                // an on/off indicator
  const value = on ? "true" : "false";
  if (el.dataset.on !== value) el.dataset.on = value;
}

// ---- the table of what the bot knows ----

const count = (g, role) => g.objs.filter((o) => o.role === role).length;
const weaponDot = (name) => `<span class="sw" style="background:${WEAPONS[name] ?? "#888"}"></span>${name}`;
// name, colour of its marker, and how to read it from the game
const FIELDS = [
  { name: "Frame", c: "#cfe3ff", value: (g) => g.n },
  { name: "Weapon", c: "#ffa63d", html: (g) => weaponDot(g.weapon) },
  { name: "Ship X, Y", c: "#cfe3ff", value: (g) => (g.ship ? gamePos(g.ship[0], g.ship[1]) : "out of play") },
  { name: "Enemies", c: "#ff6b5b", value: (g) => count(g, "enemy") + count(g, "inert") },
  { name: "Bullets", c: "#ffd24a", value: (g) => g.bullets.length },
  { name: "Deaths", c: "#ff7a66", value: (g) => g.deaths },
  { name: "Bombs thrown", c: "#ff7eb6", value: (g) => g.bombs_thrown },
];
const cells = [];

function build() {
  for (const f of FIELDS) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="name" style="--c:${f.c}">${f.name}</td><td class="val" data-nil>–</td>`;
    $("fields-main").append(tr);
    cells.push(tr.querySelector(".val"));
  }
}
build();

function showFields(game) {
  put($("state-msg"), game ? "" : "No data: the game is not running.");
  FIELDS.forEach((f, i) => {
    if (f.html) {                                                      // markup with a colour dot
      const html = game ? f.html(game) : "–";
      if (cells[i].innerHTML !== html) { cells[i].innerHTML = html; cells[i].toggleAttribute("data-nil", !game); }
    } else {
      put(cells[i], game ? f.value(game) : "–", !game);
    }
  });
}

function showCpu(debug) {                                              // the 68000's registers
  put($("pc"), debug ? addr(debug.pc) : "–", !debug);
  for (let i = 0; i < 8; i++) {
    put($("d" + i), debug ? addr(debug.d[i]) : "–", !debug);
    put($("a" + i), debug ? addr(debug.a[i]) : "–", !debug);
  }
  const sr = debug?.sr ?? 0;
  put($("sr"), debug ? "$" + hex(sr, 4) : "–", !debug);
  put($("sr-ipl"), debug ? (sr >> 8) & 7 : "–", !debug);               // interrupt mask
  lamp($("sr-s"), debug && sr & 0x2000);                               // supervisor
  lamp($("sr-x"), debug && sr & 0x10);                                 // condition codes
  lamp($("sr-n"), debug && sr & 0x8);
  lamp($("sr-z"), debug && sr & 0x4);
  lamp($("sr-v"), debug && sr & 0x2);
  lamp($("sr-c"), debug && sr & 0x1);
}

// ---- the entity view ----

function line(x1, y1, x2, y2) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
}

function draw(game) {
  const scale = canvas.width / WIDTH;                                  // game pixels to canvas pixels
  ctx.setTransform(scale, 0, 0, scale, -X0 * scale, -Y0 * scale);     // draw in game coordinates
  ctx.fillStyle = color("--sky");
  ctx.fillRect(X0, Y0, WIDTH, HEIGHT);
  ctx.lineWidth = 1;
  ctx.font = "7px ui-monospace, monospace";
  // the lane: the ship stays below the upper line, and aims for the lower one
  ctx.strokeStyle = color("--line");
  ctx.setLineDash([2, 4]);
  line(X0, Y_HIGH, X0 + WIDTH, Y_HIGH);
  line(X0, Y_PREF, X0 + WIDTH, Y_PREF);
  ctx.setLineDash([]);
  ctx.fillStyle = color("--label");
  ctx.fillText("stays below", X0 + 3, Y_HIGH - 3);
  if (!game) return;                                                   // nothing to draw before the game boots

  for (const [x, y, hp, hw, hh] of game.ground) {                      // standing structures and turrets
    ctx.fillStyle = "rgba(154, 134, 200, 0.28)";
    ctx.fillRect(x - hw, y - hh, hw * 2, hh * 2);
    ctx.strokeStyle = color("--ground");
    ctx.strokeRect(x - hw + 0.5, y - hh + 0.5, hw * 2 - 1, hh * 2 - 1);
    ctx.fillStyle = color("--label");
    ctx.fillText(`hp ${hp}`, x - hw + 2, y - hh + 8);
  }
  for (const o of game.objs) {
    if (o.role === "item") {                                           // pick-ups: pods and the bomb "M"
      const bomb = o.colour === BOMB_ITEM;
      ctx.fillStyle = bomb ? "#e6ff6b" : (WEAPONS[WEAPON_NAMES[o.colour]] ?? "#aaa");
      ctx.beginPath();
      ctx.arc(o.x, o.y, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#000";
      ctx.fillText(bomb ? "M" : "", o.x - 3, o.y + 3);
      continue;
    }
    ctx.strokeStyle = color(o.role === "enemy" ? "--enemy" : "--inert");
    ctx.setLineDash(o.role === "enemy" ? [] : [2, 2]);
    ctx.strokeRect(o.x - o.hw + 0.5, o.y - o.hh + 0.5, o.hw * 2 - 1, o.hh * 2 - 1);
    ctx.setLineDash([]);
    if (o.role === "enemy") line(o.x, o.y, o.x + o.vx * 8, o.y + o.vy * 8);   // where it is heading in 8 frames
  }
  for (const [x, y, vx, vy, hw, hh] of game.bullets) {                 // enemy projectiles
    const missile = hw > 5;                                            // the homing arrows are bigger than bullets
    ctx.fillStyle = color(missile ? "--enemy" : "--bullet");
    ctx.strokeStyle = ctx.fillStyle;
    if (missile) {
      ctx.globalAlpha = 0.4;
      ctx.fillRect(x - hw, y - hh, hw * 2, hh * 2);
      ctx.globalAlpha = 1;
      ctx.strokeRect(x - hw + 0.5, y - hh + 0.5, hw * 2 - 1, hh * 2 - 1);
    } else {
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    }
    line(x, y, x + vx * 8, y + vy * 8);
  }
  if (game.target != null) {                                           // the x it lines up with
    ctx.strokeStyle = color("--aim");
    ctx.globalAlpha = 0.35;
    ctx.setLineDash([2, 3]);
    line(game.target, Y0, game.target, Y0 + HEIGHT);
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }
  if (game.ship) {
    const [sx, sy] = game.ship;
    const cx = sx + SHIP_W / 2, cy = sy + SHIP_H / 2;
    if (game.path.length) {                                            // what the planner chose
      ctx.strokeStyle = color("--aim");
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      for (const [x, y] of game.path) ctx.lineTo(x, y);
      ctx.stroke();
      ctx.lineWidth = 1;
    }
    if (game.goal) {                                                   // the pick-up it is going for
      ctx.strokeStyle = color("--aim");
      ctx.beginPath();
      ctx.arc(game.goal[0], game.goal[1], 11, 0, Math.PI * 2);
      ctx.stroke();
      line(cx, cy, game.goal[0], game.goal[1]);
    }
    ctx.fillStyle = game.exploding ? color("--dead") : color("--ship");
    ctx.fillRect(sx, sy, SHIP_W, SHIP_H);
    ctx.strokeStyle = "#000";                                          // the core that counts for collisions
    ctx.strokeRect(cx - SHIP_HIT + 0.5, cy - SHIP_HIT + 0.5, SHIP_HIT * 2 - 1, SHIP_HIT * 2 - 1);
  }
  ctx.fillStyle = color("--label");
  ctx.font = "9px ui-monospace, monospace";
  if (game.out) ctx.fillText("ship out of play", X0 + 100, Y0 + 120);
  if (game.bombing) ctx.fillText("BOMB", X0 + 6, Y0 + 12);
}

// pointer over the view: the game coordinates under it
canvas.addEventListener("pointermove", (e) => {
  const r = canvas.getBoundingClientRect();
  const x = Math.floor(((e.clientX - r.left) / r.width) * WIDTH), y = Math.floor(((e.clientY - r.top) / r.height) * HEIGHT);
  put($("pointer"), `game ${x + X0}, ${y + Y0}`);
});
canvas.addEventListener("pointerleave", () => put($("pointer"), " "));
// bot input: the stick keeps its last direction for 8 frames and the button shows the real fire, frame by frame (the bot presses 2 frames, releases 2)
const STICK_HOLD_FRAMES = 8;
const STICK_FRAMES = { "": 0, "U": 1, "UR": 2, "R": 3, "DR": 4, "D": 5, "DL": 6, "L": 7, "UL": 8 };   // sprite order, 128 px per frame
let stickDir = "", stickN = -1e9, fireTimer = 0;

function showInput(game, live) {
  if (!live) { stickDir = ""; stickN = -1e9; }
  if (game && live) {
    const dirs = game.joy.replace("F", "");
    if (dirs) { stickDir = dirs; stickN = game.n; }
  }
  const held = game && live && game.n - stickN < STICK_HOLD_FRAMES ? stickDir : "";
  const x = (held.includes("R") ? 1 : 0) - (held.includes("L") ? 1 : 0);
  const y = (held.includes("D") ? 1 : 0) - (held.includes("U") ? 1 : 0);
  const key = (y < 0 ? "U" : y > 0 ? "D" : "") + (x > 0 ? "R" : x < 0 ? "L" : "");
  $("stick").style.backgroundPosition = `${-STICK_FRAMES[key] * 128}px 0`;
  $("stick").classList.toggle("off", !live);
  clearTimeout(fireTimer);                                             // the button replays the two frames of this update: the fire of the frame before, then the one sampled
  const press = (on) => { $("fire").dataset.on = String(on); };
  const now = !!game && live && game.joy.includes("F");
  if (game && live && game.fire_prev !== undefined) { press(game.fire_prev); fireTimer = setTimeout(() => press(now), 20); }
  else press(now);
  $("fire").disabled = !live;
}

function showGame(game) {
  put($("score"), game ? String(+game.score) : "–", !game);            // big figures
  put($("bombs"), game ? game.bombs : "–", !game);
  put($("lives"), game ? (game.game_over ? 0 : game.lives + 1) : "–", !game);
  showInput(game, game && $("bot").checked);
}

function fillSelect(select, values, current, placeholder) {            // rebuilt only when the list changes
  const key = values.join("|") + "|" + (placeholder ?? "");
  if (select.dataset.key !== key) {
    select.dataset.key = key;
    const options = values.map((v) => new Option(v, v));
    if (placeholder) options.unshift(new Option(placeholder, ""));    // the empty choice
    select.replaceChildren(...options);
  }
  if (document.activeElement !== select) select.value = current;       // do not fight the user's choice
}

function showState(state) {
  const phase = state.phase ?? "stopped";
  $("phase").textContent = PHASES[phase];
  $("phase").dataset.phase = phase;
  $("start").disabled = phase !== "stopped";                           // one run at a time
  $("stop").disabled = phase === "stopped";
  $("bot").checked = state.bot;
  const states = state.states ?? [];
  fillSelect($("state-file"), states, state.state ?? "", "none: boot the disk");
  fillSelect($("want"), WANTS, state.want ?? "any");
  $("error").textContent = state.error ?? "";
  draw(state.game);
  showGame(state.game);
  showFields(state.game);
  showCpu(state.debug);
}

async function post(path) {
  const button = document.activeElement;                               // the button just pressed
  try {
    const response = await fetch(path, { method: "POST" });
    const { error } = await response.json();                           // it may refuse
    if (error) $("error").textContent = error;
  } catch {
    $("error").textContent = "No connection to panel.py";
  }
  button?.blur?.();                                                    // keyboard focus off the button
}

$("start").onclick = () => post("/start");
$("stop").onclick = () => post("/stop");
$("bot").onchange = (e) => post(e.target.checked ? "/bot/on" : "/bot/off");
$("want").onchange = (e) => post("/want/" + e.target.value);
$("state-file").onchange = (e) => post("/state/" + encodeURIComponent(e.target.value));

const events = new EventSource("/events");                             // live data; it reconnects by itself
events.onmessage = (e) => showState(JSON.parse(e.data));
events.onerror = () => { $("error").textContent = "No connection to panel.py"; };
