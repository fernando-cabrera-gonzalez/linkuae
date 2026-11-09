"""A local web panel for the Pulse demo: start and stop it, watch the game's entities and debug the emulator.
This process is the only LinkUAE client: two clients on one emulator would interfere with each other.

  python panel.py path\\to\\winuae64.exe [--port 8765]
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

import bot                                                              # the bot's logic and the game's constants
from linkuae import EmulatorGone, Link, emulator_present                # bot.py has already set the path

HERE = os.path.dirname(os.path.abspath(__file__))                       # this folder
CONFIG = os.path.join(HERE, "pulse.uae")                                # emulator configuration
DISK = os.path.join(HERE, "pulse.adf")                                  # game disk
FILES = {"/": ("panel.html", "text/html"),                              # what the browser may ask for
         "/panel.js": ("panel.js", "text/javascript")}
SNAPSHOT_FRAMES = 2                                                     # frames per update of the panel: 25 per second
STREAM_SECONDS = 0.02                                                   # how often each browser checks for an update
FRAME_WAIT = 1.0                                                        # seconds to wait for a frame before checking the emulator
CONNECT_SECONDS = 20.0                                                  # time the emulator gets to create the shared block
STOP_SECONDS = 5.0                                                      # time the worker and the emulator get to finish
# registers to show
CUSTOM_REGS = [(0x002, "DMACONR"), (0x004, "VPOSR"), (0x006, "VHPOSR"),
               (0x00A, "JOY0DAT"), (0x00C, "JOY1DAT"), (0x00E, "CLXDAT"),
               (0x01C, "INTENAR"), (0x01E, "INTREQR"), (0x08E, "DIWSTRT"),
               (0x090, "DIWSTOP"), (0x092, "DDFSTRT"), (0x094, "DDFSTOP"),
               (0x100, "BPLCON0"), (0x102, "BPLCON1"), (0x104, "BPLCON2"),
               (0x180, "COLOR00")]


def sr_flags(sr):
    """Status register as text: trace, supervisor, interrupt level and condition codes."""
    trace = "T" if sr & 0x8000 else "-"                                 # trace bit
    mode = "S" if sr & 0x2000 else "U"                                  # supervisor or user
    pairs = zip("XNZVC", (16, 8, 4, 2, 1))                              # condition codes with their bits
    ccr = "".join(c if sr & b else "-" for c, b in pairs)               # one letter per set flag
    return f"{trace}{mode} IPL{(sr >> 8) & 7} {ccr}"                    # all together


def raw_state(frame):
    """The game's state block as hex, or nothing until its window is active."""
    try:
        return frame.read(bot.STATE, bot.STATE_BYTES).hex()             # also fails after a reattach
    except KeyError:                                                    # the window arrives with the next frame
        return ""                                                       # the page says so


def debug_snapshot(frame):
    """The emulator's state on this frame."""
    return {
        "frame": frame.number,                                          # frames published
        "vsync": frame.vsync,                                           # emulated vertical blanks
        "beam": [frame.vpos, frame.hpos],                               # where the beam was
        "cpu": frame.cpu_model,                                         # CPU model
        "pc": frame.pc,                                                 # program counter
        "sr": frame.sr,                                                 # status register
        "flags": sr_flags(frame.sr),                                    # and as text
        "d": frame.d,                                                   # data registers
        "a": frame.a,                                                   # address registers
        "usp": frame.usp,                                               # user stack
        "isp": frame.isp,                                               # interrupt stack
        "vbr": frame.vbr,                                               # vector base
        "custom": [[n, frame.custom_word(r)] for r, n in CUSTOM_REGS],  # chip registers
        "state": raw_state(frame),                                      # the game's raw state block
    }


class Demo:
    """The emulator process, and the thread that plays through LinkUAE while it runs."""

    def __init__(self, winuae):
        self.winuae = winuae                                            # path to the emulator
        self.lock = threading.Lock()                                    # guards the data the threads share
        self.control = threading.Lock()                                 # one Start or Stop at a time
        self.quit = threading.Event()                                   # asks the worker to finish
        self.thread = None                                              # the worker
        self.bot_on = True                                              # whether the bot presses the button
        # latest state
        self.snapshot = {"phase": "stopped", "bot": True, "error": None}
        self.sequence = 0                                               # bumped on every new snapshot

    def publish(self, **fields):
        """Merges fields into the state the browsers see."""
        with self.lock:                                                 # other threads read it
            # a new dict each time
            self.snapshot = {**self.snapshot, **fields, "bot": self.bot_on}
            self.sequence += 1                                          # tells the browsers there is news

    def current(self):
        """(sequence, snapshot) for a browser."""
        with self.lock:                                                 # consistent pair
            return self.sequence, self.snapshot                         # dicts are replaced, never modified

    def set_bot(self, on):
        """Turns the bot on or off. Off: the button is free for a person."""
        with self.lock:                                                 # the worker reads it
            self.bot_on = on                                            # takes effect on the next frame
        self.publish()                                                  # show it even with the demo stopped

    def start(self):
        """Starts the emulator and the worker. Returns an error text, or None."""
        with self.control:                                              # two quick clicks must not start two runs
            if self.thread and self.thread.is_alive():                  # one run at a time
                return "the demo is already running"                    # nothing to do
            if emulator_present():                                      # a client would attach to that other one
                # only one can exist
                error = "another WinUAE with LinkUAE is running: close it first"
                self.publish(error=error)                               # show it
                return error                                            # and do not start
            self.quit.clear()                                           # a new run
            # clean state
            self.publish(phase="booting", error=None, game=None, debug=None, games=0)
            # the worker
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()                                         # off it goes
            return None                                                 # no error

    def stop(self):
        """Asks the worker to finish and waits for it."""
        with self.control:                                              # one at a time
            self.quit.set()                                             # the worker checks this on every frame
            if self.thread:                                             # if it ever started
                self.thread.join(STOP_SECONDS)                          # it also closes the emulator

    def run(self):
        """Worker: owns the emulator and the LinkUAE client for one run."""
        command = [self.winuae, "-f", CONFIG, "-s", "floppy0=" + DISK]  # the demo's setup
        folder = os.path.dirname(os.path.abspath(self.winuae))          # it looks for its files there
        error = None                                                    # what went wrong, if anything
        try:
            emulator = subprocess.Popen(command, cwd=folder)            # start it
        except OSError as e:                                            # wrong path, for instance
            # show why
            self.publish(phase="stopped", error=f"cannot start {self.winuae}: {e}")
            return                                                      # nothing to clean up
        try:
            if self.wait_for_block(emulator):                           # it may be closed or stopped before that
                with Link(connect_timeout=1.0) as link:                 # attach; detaches at the end
                    self.play(link, emulator)                           # until told to stop or the emulator closes
        except EmulatorGone as e:                                       # closed by the user
            if not self.quit.is_set():                                  # not our own doing
                error = str(e)                                          # worth showing
        except Exception as e:                                          # whatever it is, the panel must not stay stuck in Running
            error = f"{type(e).__name__}: {e}"                          # show it
        finally:
            emulator.terminate()                                        # no emulator left behind
            try:
                emulator.wait(STOP_SECONDS)                             # and it has really gone
            except subprocess.TimeoutExpired:                           # it refuses
                emulator.kill()                                         # force it
            # back to the start
            self.publish(phase="stopped", error=error, game=None, debug=None)

    def wait_for_block(self, emulator):
        """Waits for the emulator to create the shared block. False if it never will."""
        deadline = time.monotonic() + CONNECT_SECONDS                   # it gets this long
        while not emulator_present():                                   # not there yet
            if self.quit.is_set() or emulator.poll() is not None:       # stopped, or it closed
                return False                                            # nothing to attach to
            if time.monotonic() > deadline:                             # something is wrong with it
                # say so
                raise EmulatorGone("no shared block: is this a WinUAE built with LinkUAE?")
            time.sleep(0.1)                                             # look again soon
        return True                                                     # ready

    def play(self, link, emulator):
        """One pass per frame: read, decide, answer. Stays short because the Amiga waits for it."""
        link.set_windows([(bot.STATE, bot.STATE_BYTES)])                # the game's state block
        pressed, last_mode, games, count = False, None, 0, 0            # what it remembers
        fired = False                                                   # a press since the last update: they last one frame
        while not self.quit.is_set():                                   # until the Stop button
            frame = link.wait_frame(timeout=FRAME_WAIT)                 # the emulator is stopped until step()
            if frame is None:                                           # paused from its own window, or killed
                if emulator.poll() is not None:                         # it has gone
                    return                                              # end of the run
                continue                                                # paused: keep waiting
            press, game = False, None                                   # by default, no button and nothing to draw
            try:
                # read
                sig, mode, score, y, vy, gates, gaps, best = bot.read_state(frame)
            except KeyError:                                            # the window arrives with the next frame
                sig = None                                              # nothing to read yet
            if sig == bot.SIGNATURE:                                    # the game has booted
                with self.lock:                                         # the browser can change it
                    bot_on = self.bot_on                                # a local copy for this frame
                if bot_on:                                              # the bot plays
                    # decide
                    press = bot.decide(mode, y, vy, gates, gaps, pressed)
                if mode != last_mode and mode == bot.GAME_OVER:         # a game has just ended
                    games += 1                                          # count it
                last_mode = mode                                        # remember the mode
                # to draw
                game = {"mode": mode, "score": score, "best": best, "y": y, "vy": vy,
                        "gates": gates, "gaps": gaps, "aim": bot.aim_y(gates, gaps),
                        "press": press}
            link.joy(bot.PORT, fire=press)                              # queue the button
            pressed = press                                             # remember it
            fired = fired or press                                      # the panel only sees every other frame
            count += 1                                                  # one more frame
            if count % SNAPSHOT_FRAMES == 0:                            # time for a panel update
                # the emulator's state
                debug = debug_snapshot(frame)
                phase = "running" if game else "booting"                # the signature tells
                if game:                                                # report presses from skipped frames too
                    game["press"] = fired
                fired = False                                           # counted from here again
                # send it out
                self.publish(phase=phase, game=game, debug=debug, games=games)
            link.step()                                                 # answer: the emulator runs the next frame


class PanelServer(ThreadingHTTPServer):
    """The HTTP server, with the port to itself. On Windows, SO_REUSEADDR (on by default in
    HTTPServer) lets a second panel bind the same port silently, and the browser then talks to
    whichever of the two Windows picks."""

    allow_reuse_address = os.name != "nt"                               # elsewhere it only skips TIME_WAIT

    def server_bind(self):
        if os.name == "nt":                                             # Windows: nobody else on this port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()                                           # fails if it is taken


class Handler(BaseHTTPRequestHandler):
    """The panel's page, its live data and its buttons."""

    demo = None                                                         # set in main()

    def log_message(self, *args):                                       # no line per request in the console
        pass                                                            # errors reach the browser anyway

    def reply(self, status, body, kind="application/json"):
        """Sends a complete response."""
        self.send_response(status)                                      # status line
        self.send_header("Content-Type", kind)                          # what it is
        self.send_header("Content-Length", str(len(body)))              # how long
        self.end_headers()                                              # ready for the body
        self.wfile.write(body)                                          # the body

    def host_allowed(self):
        """A page from another site can reach us by renaming itself in DNS: Host gives it away."""
        port = self.server.server_address[1]                            # our port
        # only these
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.host_allowed():                                     # not addressed to this panel
            self.reply(403, b"{}")                                      # refused
        elif self.path == "/events":                                    # live data
            self.stream()                                               # until the browser leaves
        elif self.path in FILES:                                        # the page itself
            name, kind = FILES[self.path]                               # which file
            with open(os.path.join(HERE, name), "rb") as f:             # read it from this folder
                self.reply(200, f.read(), kind + "; charset=utf-8")     # send it
        else:                                                           # nothing else is served
            self.reply(404, b"{}")                                      # not found

    def do_POST(self):
        origin = self.headers.get("Origin")                             # browsers send it with a POST
        host_ok = self.host_allowed()                                   # same check as GET
        # another website
        if not host_ok or (origin and origin != f"http://{self.headers.get('Host')}"):
            self.reply(403, b"{}")                                      # may not press our buttons
            return                                                      # done
        error = None                                                    # what to report
        if self.path == "/start":                                       # Start button
            error = self.demo.start()                                   # may refuse
        elif self.path == "/stop":                                      # Stop button
            self.demo.stop()                                            # waits for the emulator to close
        elif self.path in ("/bot/on", "/bot/off"):                      # the bot switch
            self.demo.set_bot(self.path.endswith("on"))                 # set it
        else:                                                           # nothing else exists
            self.reply(404, b"{}")                                      # not found
            return                                                      # done
        self.reply(200, json.dumps({"error": error}).encode())          # the page shows the error

    def stream(self):
        """Server-sent events: one JSON message per update, until the browser goes away."""
        self.send_response(200)                                         # status line
        self.send_header("Content-Type", "text/event-stream")           # the format EventSource wants
        self.send_header("Cache-Control", "no-store")                   # always live
        self.end_headers()                                              # no length: it never ends
        seen = -1                                                       # forces a first message
        try:
            while True:                                                 # until the connection breaks
                sequence, snapshot = self.demo.current()                # latest state
                if sequence != seen:                                    # something new
                    seen = sequence                                     # remember it
                    message = "data: " + json.dumps(snapshot) + "\n\n"  # one event
                    self.wfile.write(message.encode())                  # send it
                    self.wfile.flush()                                  # now
                time.sleep(STREAM_SECONDS)                              # the Amiga never waits for this thread
        except (ConnectionError, OSError):                              # browser closed or reloaded
            pass                                                        # normal way out


def main():
    ap = argparse.ArgumentParser()                                      # command line
    # the emulator
    ap.add_argument("winuae", help="path to a WinUAE built with LinkUAE")
    # where
    ap.add_argument("--port", type=int, default=8765, help="local port of the panel")
    # for tests
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    # as if Start were pressed
    ap.add_argument("--start", action="store_true", help="start the emulator right away")
    args = ap.parse_args()                                              # read it

    Handler.demo = Demo(args.winuae)                                    # shared by all requests
    try:
        server = PanelServer(("127.0.0.1", args.port), Handler)         # this machine only
    except OSError:                                                     # usually another panel already on it
        print(f"port {args.port} is busy: is another panel running? Close it or use --port", flush=True)
        return 1                                                        # the batch file pauses on this
    server.daemon_threads = True                                        # open streams do not hold the exit
    url = f"http://127.0.0.1:{args.port}/"                              # the panel's address
    print(f"Pulse panel at {url} (Ctrl+C to quit)", flush=True)         # say where
    if args.start:                                                      # no need to press Start
        error = Handler.demo.start()                                     # the page shows it too
        if error:
            print(f"cannot start: {error}", flush=True)
    if not args.no_browser:                                             # unless told otherwise
        webbrowser.open(url)                                            # open it
    try:
        server.serve_forever()                                          # until Ctrl+C
    except KeyboardInterrupt:                                           # Ctrl+C
        pass                                                            # normal way out
    finally:
        Handler.demo.stop()                                             # no emulator left behind
    return 0                                                            # exit code


if __name__ == "__main__":
    sys.exit(main())
