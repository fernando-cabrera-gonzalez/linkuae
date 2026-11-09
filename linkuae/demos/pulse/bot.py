"""The bot's logic, and a console loop that plays Pulse on an emulator that is already running.
panel.py uses the same logic; start from there unless you want the bare minimum.

  python bot.py [--games N]     (Ctrl+C to stop)
"""
import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))                      # this folder
sys.path.insert(0, os.path.join(HERE, "..", "..", "python"))           # where linkuae lives

from linkuae import EmulatorGone, Link                                 # the LinkUAE client

STATE = 0x60000                                                        # public state block, see pulse.s
STATE_BYTES = 34                                                       # its size
SIGNATURE = b"PLSE"                                                    # written by the game once it is ready
WAITING, PLAYING, GAME_OVER = 0, 1, 2                                  # game modes

PLAYER_X, PLAYER_H = 64, 12                                            # same constants as pulse.s
GATE_W, GATE_GAP = 16, 72                                              # same constants as pulse.s
AIM = (GATE_GAP - PLAYER_H) // 2 + 10                                  # below the centre: a flap rises about 33 pixels
CLIMB_FROM = 24                                                        # further than this below the aim: keep flapping
PORT = 1                                                               # joystick port


def read_state(frame):
    """(signature, mode, score, y, vy, [gate x], [gap top], best)."""
    block = frame.read(STATE, STATE_BYTES)                             # the block, big-endian as on the Amiga
    fields = struct.unpack_from(">4s10h", block)                       # signature and 10 words
    sig, mode, score, y, vy, x0, x1, x2, g0, g1, g2 = fields           # name them
    best = struct.unpack_from(">h", block, 24)[0]                      # best score, at offset 24
    return sig, mode, score, y, vy, [x0, x1, x2], [g0, g1, g2], best   # one value per field


def aim_y(gates, gaps):
    """The y to hold: aims at the first barrier still ahead of the square."""
    pairs = zip(gates, gaps)                                           # each barrier with its gap
    ahead = [(x, gap) for x, gap in pairs if x + GATE_W > PLAYER_X]    # not passed yet
    return min(ahead)[1] + AIM                                         # the nearest one's gap


def decide(mode, y, vy, gates, gaps, pressed):
    """Whether to press fire on this frame. pressed = the button's state on the last frame."""
    if mode == PLAYING:                                                # in a game
        target = aim_y(gates, gaps)                                    # where it should be
        press = y > target and (vy >= 0 or y - target > CLIMB_FROM)    # too low: flap
    else:                                                              # waiting or game over
        press = True                                                   # start or restart
    return press and not pressed                                       # the game needs a release between presses


def main():
    ap = argparse.ArgumentParser()                                     # command line
    ap.add_argument("--games", type=int, default=0,                    # when to stop
                    help="stop after N games (0 = until Ctrl+C)")
    args = ap.parse_args()                                             # read it

    with Link() as link:                                               # attach to the emulator; detaches at the end
        link.set_windows([(STATE, STATE_BYTES)])                       # the only memory the bot needs
        games, pressed, last_mode, score = 0, False, None, 0           # what it remembers
        try:
            while not args.games or games < args.games:                # one pass per frame
                frame = link.wait_frame(timeout=5.0)                   # emulator stopped: the frame is ours
                if frame is None:                                      # nothing in 5 s
                    # say so
                    print("no frames: is the emulator paused?", flush=True)
                    continue                                           # and keep waiting
                press = False                                          # by default, no button
                try:
                    # read
                    sig, mode, score, y, vy, gates, gaps, best = read_state(frame)
                except KeyError:                                       # the window arrives with the next frame
                    sig = None                                         # nothing to read yet
                if sig == SIGNATURE:                                   # the game has booted
                    press = decide(mode, y, vy, gates, gaps, pressed)  # decide
                    if mode != last_mode and mode == GAME_OVER:        # a game has just ended
                        games += 1                                     # count it
                        # report it
                        print(f"game {games}: score {score}, best {best}", flush=True)
                    last_mode = mode                                   # remember the mode
                link.joy(PORT, fire=press)                             # queue the button
                pressed = press                                        # remember it
                link.step()                                            # send it: the emulator runs the next frame
        except KeyboardInterrupt:                                      # Ctrl+C
            pass                                                       # normal way out
        except EmulatorGone:                                           # the emulator was closed
            print("the emulator has closed")                           # say so
        print(f"last score {score}")                                   # last thing it saw
    return 0                                                           # exit code


if __name__ == "__main__":
    sys.exit(main())
