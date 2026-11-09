"""Assembles pulse.s and writes pulse.adf: an 880 KB disk with the game in its boot sector.
Only needed to change the game: the .adf comes ready-made. Requires vasm (vasmm68k_mot) on PATH.

  python build_demo.py
"""
import os
import struct
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))                   # this folder
BOOTBLOCK_BYTES = 1024                                              # the two sectors the Kickstart loads and runs
HEADER_BYTES = 12                                                   # 'DOS\0', checksum, root block
ROOT_BLOCK = 880                                                    # the usual value; nothing reads it here
ADF_BYTES = 901120                                                  # size of an 880 KB floppy image


def checksum(block):
    """Long sum with carry that the Kickstart requires of a boot sector."""
    total = 0                                                       # running sum
    for i in range(0, len(block), 4):                               # one long at a time
        total += struct.unpack_from(">I", block, i)[0]              # big-endian, as on the Amiga
        if total > 0xFFFFFFFF:                                      # it overflowed
            total = (total + 1) & 0xFFFFFFFF                        # the carry goes back in
    return ~total & 0xFFFFFFFF                                      # the value that makes the whole sum all ones


def main():
    binary = os.path.join(HERE, "pulse.bin")                        # temporary output of the assembler
    source = os.path.join(HERE, "pulse.s")                          # the game
    # assemble
    subprocess.run(["vasmm68k_mot", "-quiet", "-Fbin", "-m68000", "-o", binary, source],
                   check=True)
    with open(binary, "rb") as f:                                   # its machine code
        code = f.read()                                             # all of it
    os.remove(binary)                                               # no longer needed
    room = BOOTBLOCK_BYTES - HEADER_BYTES                           # what is left after the header
    if len(code) > room:                                            # the point of the demo: it must fit
        # say so
        raise SystemExit(f"the game takes {len(code)} bytes and only {room} fit")

    block = bytearray(BOOTBLOCK_BYTES)                              # all zeros
    block[0:4] = b"DOS\0"                                           # marks the disk as bootable
    struct.pack_into(">I", block, 8, ROOT_BLOCK)                    # root block number
    block[HEADER_BYTES:HEADER_BYTES + len(code)] = code             # execution starts right here
    struct.pack_into(">I", block, 4, checksum(block))               # computed with its field at zero
    with open(os.path.join(HERE, "pulse.adf"), "wb") as f:          # the disk
        f.write(bytes(block) + bytes(ADF_BYTES - BOOTBLOCK_BYTES))  # boot sector, then zeros
    print(f"pulse.adf: {len(code)} of {room} boot sector bytes")    # how close it is


if __name__ == "__main__":
    main()
