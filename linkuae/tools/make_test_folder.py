"""Creates a portable WinUAE test folder cloned from an existing install.

Copies Configurations/Kickstarts/Savestates/adf, rewrites the old folder path
inside .uae files, and exports the WinUAE registry settings into winuae.ini
(ini mode: the test build never touches the registry of the normal install).

  python -I make_test_folder.py D:\\WinUAE6030_x64 D:\\LinkUAE
"""
import os
import shutil
import sys
import winreg

REG_ROOT = r"Software\Arabuusimiehet\WinUAE"
COPY_DIRS = ["Configurations", "Kickstarts", "Savestates", "adf"]
MAKE_DIRS = ["Screenshots", "SaveImages"]


def replace_path(text, old, new):
    # Case-insensitive replace of the old install folder (with trailing backslash).
    low, old_low = text.lower(), old.lower()
    out, i = [], 0
    while True:
        j = low.find(old_low, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        out.append(new)
        i = j + len(old)


def export_key(key, section, sections):
    values = []
    i = 0
    while True:
        try:
            name, data, kind = winreg.EnumValue(key, i)
        except OSError:
            break
        i += 1
        if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            text = data
        elif kind in (winreg.REG_DWORD, winreg.REG_QWORD):
            text = str(data)
        elif kind == winreg.REG_BINARY:
            text = data.hex().upper()
        else:
            continue
        values.append((name, text))
    sections.append((section, values))
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(key, i)
        except OSError:
            break
        i += 1
        with winreg.OpenKey(key, sub) as k:
            child = sub if section == "WinUAE" else f"{section}/{sub}"
            export_key(k, child, sections)


def main(src, dst):
    src = os.path.abspath(src).rstrip("\\") + "\\"
    dst = os.path.abspath(dst).rstrip("\\") + "\\"
    os.makedirs(dst, exist_ok=True)
    for d in COPY_DIRS:
        if os.path.isdir(src + d):
            shutil.copytree(src + d, dst + d, dirs_exist_ok=True)
    for d in MAKE_DIRS:
        os.makedirs(dst + d, exist_ok=True)

    for root, _, files in os.walk(dst + "Configurations"):
        for f in files:
            if f.lower().endswith(".uae"):
                p = os.path.join(root, f)
                raw = open(p, "rb").read()
                bom = raw.startswith(b"\xef\xbb\xbf")
                text = raw.decode("utf-8-sig", errors="surrogateescape")
                new = replace_path(text, src, dst)
                if new != text:
                    open(p, "wb").write((b"\xef\xbb\xbf" if bom else b"") +
                                        new.encode("utf-8", errors="surrogateescape"))
                    print("rewrote", p)

    sections = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_ROOT) as k:
        export_key(k, "WinUAE", sections)
    lines = []
    for section, values in sections:
        lines.append(f"[{section}]")
        for name, text in values:
            lines.append(f"{name}={replace_path(text, src, dst)}")
    ini = dst + "winuae.ini"
    with open(ini, "wb") as f:
        f.write(b"\xef\xbb\xbf" + ("\n".join(lines) + "\n").encode("utf-8"))
    print("wrote", ini, f"({sum(len(v) for _, v in sections)} values)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
