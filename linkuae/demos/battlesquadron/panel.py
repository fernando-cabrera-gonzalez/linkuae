"""A local web panel for the Battle Squadron bot: boot the game (or a savestate), watch what it sees
and plans, and debug the emulator. This process is the only LinkUAE client: two clients on one emulator
would interfere with each other, so the bot (bot.Player) runs inside it.

  python panel.py [path\\to\\winuae64.exe] [--start] [--port 8765] [--no-browser]
                  [--state my_game.uss] [--press-p] [--want orange] [--horizon N]
The emulator is the argument, else the WINUAE environment variable, else linkuae\\build\\out\\winuae64.exe.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import bot                                                            # the bot, and the game's constants
from player import Player                                             # bot.py as one object this panel can drive
from linkuae import EmulatorGone, Link, emulator_present                # bot.py has already set the path

HERE = os.path.dirname(os.path.abspath(__file__))                       # this folder
CONFIG = os.path.join(HERE, "bsquad.uae")                               # emulator configuration
DEFAULT_EXE = os.path.join(HERE, "..", "..", "build", "out", "winuae64.exe")   # where build.cmd leaves it
STATES_DIR = HERE                                                       # the savestates to start from: your own .uss files in this folder
FILES = {"/": ("panel.html", "text/html"),                              # what the browser may ask for
         "/panel.js": ("panel.js", "text/javascript"),
         "/stick.png": ("stick.png", "image/png"),                      # bot input sprites: 9 joystick frames, 2 button frames
         "/button.png": ("button.png", "image/png")}
WANTS = {"any": None, **{name: {code} for name, code in bot.COLOURS.items()}}   # weapon colours worth taking
SNAPSHOT_FRAMES = 2                                                     # frames per update of the panel: 25 per second
STREAM_SECONDS = 0.02                                                   # how often each browser checks for an update
FRAME_WAIT = 1.0                                                        # seconds to wait for a frame before checking the emulator
CONNECT_SECONDS = 20.0                                                  # time the emulator gets to create the shared block
STOP_SECONDS = 5.0                                                      # time the worker and the emulator get to finish


def debug_snapshot(frame):
    """The 68000 registers on this frame, as the CPU card shows them."""
    return {"pc": frame.pc, "sr": frame.sr, "d": frame.d, "a": frame.a}


def find_winuae(given):
    """The emulator to run and where its path came from: the argument, else the WINUAE environment variable, else
    linkuae\\build\\out. Quotes around the path are dropped, and a folder means the winuae64.exe in it."""
    for path, source in ((given, "the argument"), (os.environ.get("WINUAE"), "the WINUAE environment variable"),
                         (DEFAULT_EXE, "the build folder")):
        path = (path or "").strip().strip('"')
        if path:
            path = os.path.abspath(path)
            if os.path.isdir(path):
                path = os.path.join(path, "winuae64.exe")
            return path, source


def list_states():
    """The savestates one can start from."""
    try:
        return sorted(f for f in os.listdir(STATES_DIR) if f.lower().endswith(".uss"))
    except OSError:
        return []


class Demo:
    """The emulator process, and the thread that plays through LinkUAE while it runs."""

    def __init__(self, winuae, state, want="any"):
        self.press_p = False                                            # --press-p
        self.winuae = winuae                                            # path to the emulator
        self.state = state                                              # savestate for the next start (None: chosen in the page)
        self.lock = threading.Lock()                                    # guards the data the threads share
        self.control = threading.Lock()                                 # one Start or Stop at a time
        self.quit = threading.Event()                                   # asks the worker to finish
        self.thread = None                                              # the worker
        self.bot_on = True                                              # whether the bot plays
        self.want = want                                                # weapon colour it goes for (--want; the page can change it)
        self.snapshot = {"phase": "stopped", "bot": True, "error": None}
        self.sequence = 0                                               # bumped on every new snapshot
        self.publish()

    def publish(self, **fields):
        """Merges fields into the state the browsers see."""
        with self.lock:
            self.snapshot = {**self.snapshot, **fields, "bot": self.bot_on, "want": self.want,
                             "state": self.state, "states": list_states()}
            self.sequence += 1

    def current(self):
        """(sequence, snapshot) for a browser."""
        with self.lock:
            return self.sequence, self.snapshot                         # dicts are replaced, never modified

    def set_bot(self, on):
        """Turns the bot on or off. Off: the joystick is free for a person."""
        with self.lock:
            self.bot_on = on                                            # takes effect on the next frame
        self.publish()

    def set_want(self, name):
        with self.lock:
            self.want = name
        self.publish()

    def set_state(self, name):
        """The savestate the next start loads ("" = none: boot the disk). Returns an error text, or None."""
        if name and name not in list_states():
            return f"no such savestate: {name}"
        with self.lock:
            self.state = name or None
        self.publish()
        return None

    def start(self):
        """Starts the emulator and the worker. Returns an error text, or None."""
        with self.control:
            if self.thread and self.thread.is_alive():
                return "the game is already running"
            if self.state and self.state not in list_states():          # deleted since it was chosen
                error = f"the savestate {self.state} is no longer in {STATES_DIR}"
                self.publish(error=error)
                return error
            if emulator_present():                                      # a client would attach to that other one
                error = "another WinUAE with LinkUAE is running: close it first"
                self.publish(error=error)
                return error
            self.quit.clear()
            self.publish(phase="booting", error=None, game=None, debug=None)
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            return None

    def stop(self):
        """Asks the worker to finish and waits for it."""
        with self.control:
            self.quit.set()                                             # the worker checks this on every frame
            if self.thread:
                self.thread.join(STOP_SECONDS)                          # it also closes the emulator

    def run(self):
        """Worker: owns the emulator and the LinkUAE client for one run."""
        command = [self.winuae, "-f", CONFIG]                           # no savestate: it boots the disk in the configuration
        if self.state:
            command += ["-s", "statefile=" + os.path.join(STATES_DIR, self.state)]
        folder = os.path.dirname(os.path.abspath(self.winuae))          # it looks for its files there
        error = None
        try:
            emulator = subprocess.Popen(command, cwd=folder)
        except OSError as e:                                            # wrong path, for instance
            self.publish(phase="stopped", error=f"cannot start {self.winuae}: {e}")
            return
        try:
            if self.wait_for_block(emulator):
                with Link(connect_timeout=1.0) as link:                 # attach; detaches at the end
                    self.play(link, emulator)
        except EmulatorGone as e:                                       # closed by the user
            if not self.quit.is_set():
                error = str(e)
        except Exception as e:                                          # the panel must not stay stuck in Running
            error = f"{type(e).__name__}: {e}"
        finally:
            emulator.terminate()                                        # no emulator left behind
            try:
                emulator.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                emulator.kill()
            self.publish(phase="stopped", error=error, game=None, debug=None)

    def wait_for_block(self, emulator):
        """Waits for the emulator to create the shared block. False if it never will."""
        deadline = time.monotonic() + CONNECT_SECONDS
        while not emulator_present():
            if self.quit.is_set() or emulator.poll() is not None:       # stopped, or it closed
                return False
            if time.monotonic() > deadline:
                raise EmulatorGone("no shared block: is this a WinUAE built with LinkUAE?")
            time.sleep(0.1)
        return True

    def new_player(self):
        """A bot with no memory of earlier frames: at the start, and after a reset or a savestate loaded in WinUAE."""
        player = Player()
        player.brain.a280_sticky = True                                 # the A280 hunt of the perfect game (bot.py --a280-sticky)
        return player

    def play(self, link, emulator):
        """One pass per frame: read, decide, answer. Stays short because the Amiga waits for it."""
        link.set_windows([(bot.CHIP, 0x80000)])                         # all the chip RAM
        bot.AIM_A280, bot.A280_FOCUS_R = 1.2, 40.0                       # the A280 hunt of the perfect game (bot.py --a280-aim 1.2 --a280-focus-r 40)
        player = self.new_player()
        n, last_vsync, lost = 0, 0, 0
        while not self.quit.is_set():
            frame = link.wait_frame(timeout=FRAME_WAIT)
            if frame is None:                                           # paused from its own window, or killed
                if emulator.poll() is not None:
                    return
                continue
            if n == 0:                                                  # linkuae_wait holds the first frame for us: then it is vsync 0 or 1
                if frame.vsync > 1:
                    print(f"[0] note: the bot attached at frame {frame.vsync} of the emulator, not at the first one (is linkuae_wait set?)", flush=True)
            elif frame.vsync <= last_vsync:                             # reset, or a savestate or game loaded from WinUAE: the count starts again
                print(f"[{n}] the emulator was reset or loaded a savestate: the bot starts over", flush=True)
                player, lost = self.new_player(), 0
            elif frame.vsync != last_vsync + 1 and not lost:            # the emulator ran without us (reply over linkuae_timeout)
                lost = frame.vsync - last_vsync - 1
                print(f"[{n}] WARNING: the emulator ran {lost} frames without the bot (a reply took over linkuae_timeout)", flush=True)
                self.publish(error=f"frame {n}: the emulator ran {lost} frames without the bot (a reply took over linkuae_timeout)")
            last_vsync = frame.vsync
            if self.press_p and n in (1, 5):                            # --press-p: P once at the very start, for a savestate saved paused
                link.key(bot.KEY_P, n == 1)
            with self.lock:
                bot_on, want = self.bot_on, WANTS[self.want]
            game = None
            if n >= 2:
                player.want = want
                game = player.step(n, frame, link, active=bot_on)
            n += 1
            if game and n % SNAPSHOT_FRAMES == 0:                       # time for a panel update
                self.publish(phase="running", game=game, debug=debug_snapshot(frame))
            elif n % SNAPSHOT_FRAMES == 0:
                self.publish(phase="booting")
            link.step()                                                 # answer: the emulator runs the next frame


class PanelServer(ThreadingHTTPServer):
    """The HTTP server, with the port to itself. On Windows, SO_REUSEADDR (on by default in
    HTTPServer) lets a second panel bind the same port silently, and the browser then talks to
    whichever of the two Windows picks."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    """The panel's page, its live data and its buttons."""

    demo = None                                                         # set in main()

    def log_message(self, *args):
        pass

    def reply(self, status, body, kind="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def host_allowed(self):
        """A page from another site can reach us by renaming itself in DNS: Host gives it away."""
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.host_allowed():
            self.reply(403, b"{}")
        elif self.path == "/events":
            self.stream()
        elif self.path in FILES:
            name, kind = FILES[self.path]
            with open(os.path.join(HERE, name), "rb") as f:
                self.reply(200, f.read(), kind + ("; charset=utf-8" if kind.startswith("text") else ""))
        else:
            self.reply(404, b"{}")

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.host_allowed() or (origin and origin != f"http://{self.headers.get('Host')}"):
            self.reply(403, b"{}")                                      # another website may not press our buttons
            return
        error = None
        if self.path == "/start":
            error = self.demo.start()
        elif self.path == "/stop":
            self.demo.stop()
        elif self.path in ("/bot/on", "/bot/off"):
            self.demo.set_bot(self.path.endswith("on"))
        elif self.path.startswith("/want/") and self.path[6:] in WANTS:
            self.demo.set_want(self.path[6:])
        elif self.path.startswith("/state/"):
            error = self.demo.set_state(unquote(self.path[7:]))         # the name must be one of the listed files
        else:
            self.reply(404, b"{}")
            return
        self.reply(200, json.dumps({"error": error}).encode())

    def stream(self):
        """Server-sent events: one JSON message per update, until the browser goes away."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = -1
        try:
            while True:
                sequence, snapshot = self.demo.current()
                if sequence != seen:
                    seen = sequence
                    self.wfile.write(("data: " + json.dumps(snapshot) + "\n\n").encode())
                    self.wfile.flush()
                time.sleep(STREAM_SECONDS)                              # the Amiga never waits for this thread
        except (ConnectionError, OSError):                              # browser closed or reloaded
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("winuae", nargs="?", help="path to a WinUAE built with LinkUAE (default: WINUAE, then linkuae\\build\\out)")
    ap.add_argument("--state", help="savestate to start from, a .uss file in " + STATES_DIR + " (optional; without it the disk in bsquad.uae boots)")
    ap.add_argument("--horizon", type=int, default=bot.HORIZON, help="game ticks (2 frames each) the bot plans ahead (default %d)" % bot.HORIZON)
    ap.add_argument("--port", type=int, default=8765, help="local port of the panel")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    ap.add_argument("--start", action="store_true", help="start the emulator right away")
    ap.add_argument("--want", choices=list(WANTS), default="any", help="pick-up colour the bot goes for at start (any by default; the page can change it)")
    ap.add_argument("--press-p", action="store_true", help="press P once when the game starts: for a savestate saved paused")
    args = ap.parse_args()
    exe, source = find_winuae(args.winuae)
    if not os.path.isfile(exe):                                         # say how to fix it, before anything opens
        print(f"WinUAE with LinkUAE not found: {exe} (from {source})\n"
              "Give its path as the first argument, set the WINUAE environment variable to it, "
              "or build it with linkuae\\build\\build.cmd", flush=True)
        return 1
    try:
        with open(CONFIG, encoding="utf-8", errors="replace") as f:
            config = f.read()
    except OSError as e:
        print(f"cannot read {CONFIG}: {e}", flush=True)
        return 1
    if "<full path to your" in config:                                  # the ROM and disk placeholders are still there
        print("bsquad.uae still has the placeholders: write the full paths to your Kickstart ROM and Battle Squadron disk in it",
              flush=True)
        return 1
    if args.state and args.state not in list_states():
        print(f"no such savestate in {STATES_DIR}: {args.state} (found: {', '.join(list_states()) or 'none'})", flush=True)
        return 1
    bot.HORIZON = args.horizon
    if bot.plan_nb is not None:                                         # Numba: compile before the game, never during it
        print("compiling the planner (Numba)...", flush=True)
        t0 = time.monotonic()
        bot.warmup()
        print(f"planner compiled in {time.monotonic() - t0:.1f} s", flush=True)
    else:
        print("numba not installed: the planner runs on plain numpy (same algorithm, slower)", flush=True)

    Handler.demo = Demo(exe, args.state, args.want)
    Handler.demo.press_p = args.press_p
    try:
        server = PanelServer(("127.0.0.1", args.port), Handler)         # this machine only
    except OSError:
        print(f"port {args.port} is busy: is another panel running? Close it or use --port", flush=True)
        return 1
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Battle Squadron panel at {url} (Ctrl+C to quit)", flush=True)
    if args.start:
        error = Handler.demo.start()
        if error:
            print(f"cannot start: {error}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.demo.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
