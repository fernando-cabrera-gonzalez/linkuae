// The panel's behaviour: live data from panel.py, the game drawn from it, and the buttons.

const WIDTH = 320;                                                     // game screen, in Amiga pixels
const HEIGHT = 256;                                                    // same
const PLAYER_X = 64;                                                   // same constants as pulse.s
const PLAYER_W = 16;                                                   // sprite width
const PLAYER_H = 12;                                                   // square height
const GATE_W = 16;                                                     // barrier width
const GATE_GAP = 72;                                                   // gap height
const MODES = ["waiting", "playing", "game over"];                     // game modes, in order
// run states
const PHASES = { stopped: "Stopped", booting: "Booting", running: "Running" };
const STATE_ADDR = 0x60000;                                            // where the game keeps its state
const STATE_SIZE = 34;                                                 // bytes the bot reads each frame
const MAP_BYTES = 32;                                                  // four lines of 8; Restart wait is left out

const $ = (id) => document.getElementById(id);                         // shorthand for elements
const canvas = $("game");                                              // the entity view
const ctx = canvas.getContext("2d");                                   // its drawing context
const css = getComputedStyle(document.documentElement);                // colours live in the stylesheet
const color = (name) => css.getPropertyValue(name).trim();             // read one
// Amiga style
const hex = (value, digits) => value.toString(16).toUpperCase().padStart(digits, "0");
const addr = (value) => "$" + hex(value, 8);                           // 32-bit values

// ---- writing to the page: only what changed, 25 times a second ----

const calm = matchMedia("(prefers-reduced-motion: reduce)");           // the viewer asked for no motion

function flash(el, strength) {                                         // a short glow on a changed value
  if (calm.matches || !el.animate) return;                             // none if motion is reduced
  el._flash?.cancel();                                                 // restart rather than stack
  el._flash = el.animate(                                              // fades out on its own
    [{ boxShadow: `inset 0 0 0 100px rgba(255, 255, 255, ${strength})` }, { boxShadow: "inset 0 0 0 100px rgba(255, 255, 255, 0)" }],
    { duration: 450, easing: "ease-out" });
}

// Sets an element's text if it differs. nil marks a placeholder; glow > 0 flashes a real change.
function put(el, text, nil = false, glow = 0) {
  text = String(text);                                                 // numbers too
  if (el.textContent === text) return;                                 // nothing to do
  const was = el.dataset.nil === undefined;                            // it held a real value
  el.textContent = text;                                               // the new value
  el.toggleAttribute("data-nil", nil);                                 // placeholders are dimmed
  if (glow && was && !nil) flash(el, glow);                            // only changes between values
}

function lamp(el, on) {                                                // an on/off indicator
  const value = on ? "true" : "false";                                 // as the stylesheet reads it
  if (el.dataset.on !== value) el.dataset.on = value;                  // only when it changes
}

// ---- the state block at $60000: big-endian, words signed ----

const word = (b, o) => { const v = (b[o] << 8) | b[o + 1]; return v >= 0x8000 ? v - 0x10000 : v; };
const uword = (b, o) => (b[o] << 8) | b[o + 1];                        // same, unsigned
const words = (b, o) => [0, 1, 2].map((i) => word(b, o + 2 * i));      // three barriers

// offset, size, name and how to read it; minor fields have no table row, only the caption on hover
const FIELDS = [
  { key: "sig", off: 0, len: 4, name: "Signature", minor: true,
    value: (b) => { const s = String.fromCharCode(...b.slice(0, 4)).replace(/[^\x20-\x7e]/g, ".");
      return `"${s}"` + (s === "PLSE" ? " · game ready" : ""); } },
  { key: "mode", off: 4, len: 2, name: "Mode", value: (b) => `${word(b, 4)} · ${MODES[word(b, 4)] ?? "?"}` },
  { key: "score", off: 6, len: 2, name: "Score", value: (b) => word(b, 6) },
  { key: "y", off: 8, len: 2, name: "Square y", value: (b) => `${word(b, 8)} px` },
  { key: "speed", off: 10, len: 2, name: "Speed", value: (b) => `${(word(b, 10) / 256).toFixed(2)} px/frame` },
  { key: "gatex", off: 12, len: 6, name: "Barrier x", value: (b) => words(b, 12).join(", ") },
  { key: "gap", off: 18, len: 6, name: "Gap top", value: (b) => words(b, 18).join(", ") },
  { key: "best", off: 24, len: 2, name: "Best", value: (b) => word(b, 24) },
  // 8.8 fixed point, so it is read unsigned
  { key: "fy", off: 26, len: 2, name: "Fixed y", minor: true, note: "y with 8 fraction bits",
    value: (b) => `${(uword(b, 26) / 256).toFixed(2)} px` },
  { key: "seed", off: 28, len: 2, name: "Course seed", minor: true, note: "state of the course generator",
    value: (b) => "$" + hex(uword(b, 28), 4) },
  // pulse.s keeps the button in one byte (clr.b/move.b); the next one is never written
  { key: "btn", off: 30, len: 1, name: "Button", minor: true, note: "as the game read it on the last frame",
    value: (b) => (b[30] ? "down" : "up") },
  { key: "pad", off: 31, len: 1, name: "Unused", minor: true, value: () => "not written by the game" },
];
const SHOWN = FIELDS.filter((f) => !f.minor);                          // the ones with a table row

let lit = null;                                                        // field under the pointer or focus
let last = { debug: null, b: [] };                                     // latest block, for the caption

function caption() {                                                   // the hovered field, or a hint
  const { debug, b } = last;
  const f = FIELDS.find((x) => x.key === lit);
  put($("state-msg"), !debug ? "No data: the demo is not running."
    : b.length < STATE_SIZE ? "Not readable yet."
    : f ? `+${f.off} ${f.name} · ${f.value(b)}` + (f.note ? ` · ${f.note}` : "")
    : "Hover a field to see its bytes and its value.");
}

const bytes = [];                                                      // one element per byte
const fieldOf = [];                                                    // which field each byte belongs to
const rows = {};                                                       // table cells, by field

function build() {                                                     // once: the dump and the table
  const dump = $("dump");
  for (let i = 0; i < MAP_BYTES; i++) {                                // 8 bytes per line
    if (i % 8 === 0) {
      const line = document.createElement("div");
      line.className = "row";
      line.innerHTML = `<span class="addr">$${hex(STATE_ADDR + i, 5)}</span>`;
      dump.append(line);
    }
    const field = FIELDS.find((f) => i >= f.off && i < f.off + f.len);
    const b = document.createElement("span");
    b.className = `b f-${field.key}` + (i === field.off && i % 8 ? " fs" : "");
    b.dataset.f = field.key;
    b.dataset.nil = "";
    b.textContent = "··";
    dump.lastChild.append(b);
    bytes.push(b);
    fieldOf.push(field);
  }
  for (const f of SHOWN) {
    const tr = document.createElement("tr");
    tr.className = `f-${f.key}`;
    tr.dataset.f = f.key;
    tr.tabIndex = 0;
    tr.innerHTML = `<td class="off">+${f.off}</td><td class="name">${f.name}</td><td class="hex"></td><td class="val" data-nil>–</td>`;
    $("fields-main").append(tr);
    rows[f.key] = { tr, hex: tr.querySelector(".hex"), val: tr.querySelector(".val") };
  }
  const light = (key) => {                                             // its bytes and its row, together
    if (key === lit) return;
    for (const el of document.querySelectorAll("#state .hl")) el.classList.remove("hl");
    lit = key;
    caption();                                                         // its name and value
    if (!key) return;
    for (const el of document.querySelectorAll(`#state [data-f="${key}"]`)) el.classList.add("hl");
  };
  const state = $("state");
  state.addEventListener("pointerover", (e) => light(e.target.closest("[data-f]")?.dataset.f ?? null));
  state.addEventListener("pointerleave", () => light(null));
  state.addEventListener("focusin", (e) => light(e.target.closest("[data-f]")?.dataset.f ?? null));
  state.addEventListener("focusout", () => light(null));
}
build();

function showBlock(debug) {                                      // the 34 bytes, decoded
  const raw = debug?.state ?? "";
  const b = (raw.match(/../g) ?? []).map((pair) => parseInt(pair, 16));
  const ok = b.length >= STATE_SIZE;                                   // a whole block arrived
  last = { debug, b };                                                 // also read on hover
  caption();
  bytes.forEach((el, i) => put(el, i < b.length ? hex(b[i], 2) : "··", i >= b.length));
  for (const f of SHOWN) {
    const row = rows[f.key];
    const mine = ok ? b.slice(f.off, f.off + f.len) : [];
    put(row.hex, mine.map((v) => hex(v, 2)).join("").replace(/(.{4})(?=.)/g, "$1 "));
    put(row.val, ok ? f.value(b) : "–", !ok);                          // no glow: the bytes above show changes
  }
}

function showCpu(debug) {                                              // the 68000's registers
  put($("pc"), debug ? addr(debug.pc) : "–", !debug, 0.12);
  for (let i = 0; i < 8; i++) {
    put($("d" + i), debug ? addr(debug.d[i]) : "–", !debug, 0.12);
    put($("a" + i), debug ? addr(debug.a[i]) : "–", !debug, 0.12);
  }
  lamp($("a4-tag"), debug?.a[4] === STATE_ADDR);                       // the game keeps its state here
  const sr = debug?.sr ?? 0;
  put($("sr"), debug ? "$" + hex(sr, 4) : "–", !debug, 0.12);
  put($("sr-ipl"), debug ? (sr >> 8) & 7 : "–", !debug);               // interrupt mask
  lamp($("sr-s"), debug && sr & 0x2000);                               // supervisor
  lamp($("sr-x"), debug && sr & 0x10);                                 // condition codes
  lamp($("sr-n"), debug && sr & 0x8);
  lamp($("sr-z"), debug && sr & 0x4);
  lamp($("sr-v"), debug && sr & 0x2);
  lamp($("sr-c"), debug && sr & 0x1);
}

function draw(game) {
  const scale = canvas.width / WIDTH;                                  // Amiga pixels to canvas pixels
  ctx.setTransform(scale, 0, 0, scale, 0, 0);                          // draw in Amiga coordinates
  ctx.fillStyle = color("--sky");                                      // background
  ctx.fillRect(0, 0, WIDTH, HEIGHT);                                   // clears the last frame
  if (!game) return;                                                   // nothing to draw before the game boots

  // barrier the bot aims at
  const nextX = Math.min(...game.gates.filter((x) => x + GATE_W > PLAYER_X));
  game.gates.forEach((x, i) => {                                       // each barrier
    if (x >= WIDTH) return;                                            // not on screen yet
    const gap = game.gaps[i];                                          // top of its gap
    ctx.fillStyle = color("--gate");                                   // same colour as in the game
    ctx.fillRect(x, 0, GATE_W, gap);                                   // top piece
    ctx.fillRect(x, gap + GATE_GAP, GATE_W, HEIGHT - gap - GATE_GAP);  // bottom piece
    ctx.strokeStyle = color(x === nextX ? "--aim" : "--line");         // highlights the bot's target
    ctx.lineWidth = 1;                                                 // thin outline
    ctx.strokeRect(x + 0.5, gap + 0.5, GATE_W - 1, GATE_GAP - 1);      // outline of the gap
    ctx.fillStyle = color("--label");                                  // caption colour
    ctx.font = "7px ui-monospace, monospace";                          // small, in Amiga pixels
    ctx.fillText(`#${i} x=${x} gap=${gap}`, x + GATE_W + 3, gap + 9);  // its numbers, beside the gap
  });

  ctx.strokeStyle = color("--aim");                                    // the height the bot wants to hold
  ctx.setLineDash([3, 3]);                                             // dashed, so it reads as a guide
  ctx.beginPath();                                                     // one horizontal line
  ctx.moveTo(0, game.aim + 0.5);                                       // from the left edge
  ctx.lineTo(WIDTH, game.aim + 0.5);                                   // to the right edge
  ctx.stroke();                                                        // draw it
  ctx.setLineDash([]);                                                 // back to solid

  ctx.fillStyle = color(game.mode === 2 ? "--dead" : "--square");      // red once it has crashed
  ctx.fillRect(PLAYER_X, game.y, PLAYER_W, PLAYER_H);                  // the square
  ctx.strokeStyle = color("--label");                                  // the speed, as an arrow
  ctx.beginPath();                                                     // one line
  const middle = game.y + PLAYER_H / 2;                                // from the square's centre
  ctx.moveTo(PLAYER_X + PLAYER_W + 3, middle);                         // just to its right
  ctx.lineTo(PLAYER_X + PLAYER_W + 3, middle + game.vy / 32);          // length: its speed
  ctx.stroke();                                                        // draw it
  if (game.press) {                                                    // the bot pressed on this frame
    ctx.fillStyle = color("--aim");                                    // same colour as its guide
    ctx.fillRect(PLAYER_X, game.y - 4, PLAYER_W, 2);                   // a small mark above the square
  }
}

const FIRE_HOLD_MS = 120;                                              // a press lasts one frame: keep it visible
let fireUntil = 0;                                                     // when the lamp goes off

function showGame(game, games) {
  put($("score"), game ? game.score : "–", !game);                     // big figures
  put($("best"), game ? game.best : "–", !game);                       // same
  const now = performance.now();
  if (game?.press && $("bot").checked) fireUntil = now + FIRE_HOLD_MS; // what the bot sends, nothing else
  if (!game || !$("bot").checked) fireUntil = 0;                       // off at once when nothing plays
  lamp($("fire"), now < fireUntil);
}

function showDebug(debug) {
  showBlock(debug);                                              // what the bot reads
  showCpu(debug);                                                      // where the CPU is
}

function showState(state) {
  const phase = state.phase ?? "stopped";                              // defaults to stopped
  $("phase").textContent = PHASES[phase];                              // its name
  $("phase").dataset.phase = phase;                                    // its colour, from the stylesheet
  $("start").disabled = phase !== "stopped";                           // one run at a time
  $("stop").disabled = phase === "stopped";                            // nothing to stop
  $("bot").checked = state.bot;                                        // the switch follows the server
  $("error").textContent = state.error ?? "";                          // empty when all is well
  draw(state.game);                                                    // the entity view
  showGame(state.game, state.games);                                   // its numbers
  showDebug(state.debug);                                              // the emulator's state
}

async function post(path) {
  const button = document.activeElement;                               // the button just pressed
  try {
    const response = await fetch(path, { method: "POST" });            // tell the server
    const { error } = await response.json();                           // it may refuse
    if (error) $("error").textContent = error;                         // say why
  } catch {                                                            // the server has gone
    $("error").textContent = "No connection to panel.py";              // say so
  }
  button?.blur?.();                                                    // keyboard focus off the button
}

$("start").onclick = () => post("/start");                             // Start button
$("stop").onclick = () => post("/stop");                               // Stop button
// bot switch
$("bot").onchange = (e) => post(e.target.checked ? "/bot/on" : "/bot/off");

const events = new EventSource("/events");                             // live data; it reconnects by itself
events.onmessage = (e) => showState(JSON.parse(e.data));               // one snapshot
// server stopped
events.onerror = () => { $("error").textContent = "No connection to panel.py"; };