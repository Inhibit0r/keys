#!/usr/bin/env python3
"""keys: manual switching of Tavily / Firecrawl API keys.

Keys live only on this device: ~/.config/api-keys/<service>, one `label=key` per line, chmod 600.
`use` writes the chosen key where MCP and CLI read it (on Windows also the user environment).
Never syncs keys and never rotates by itself.

  keys                           full-screen menu: arrows, Enter, Esc, q; live credits
  keys list [service]            labels, masked keys, * = active
  keys add <service> <label>     key from a hidden prompt (or stdin); first key becomes active
  keys import <service> <label>  store the currently active key under a label
  keys use <service> <label>     make a stored key active
  keys next <service>            activate the next stored key
  keys remove <service> <label>  forget a stored key (the active key stays active)
  keys check [service] [--all]   remaining credits of the active (or every) key
"""

import getpass
import io
import json
import os
import random
import re
import shutil
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stdout
from functools import lru_cache
from pathlib import Path

HOME = Path.home()
STORE = HOME / ".config/api-keys"
WINDOWS = os.name == "nt"


def firecrawl_cli_credentials(home=HOME, platform=sys.platform):
    # Same directory as getConfigDir() in firecrawl-cli.
    base = {"darwin": "Library/Application Support", "win32": "AppData/Roaming"}
    return home / base.get(platform, ".config") / "firecrawl-cli/credentials.json"


SERVICES = {
    "tavily": {
        "var": "TAVILY_API_KEY",
        "env": HOME / ".config/tavily/env",
        "usage": "https://api.tavily.com/usage",
    },
    "firecrawl": {
        "var": "FIRECRAWL_API_KEY",
        "env": HOME / ".config/firecrawl/env",
        "usage": "https://api.firecrawl.dev/v2/team/credit-usage",
        "cli": firecrawl_cli_credentials(),
    },
}
# The env file is sourced by the shell: only plain key characters may reach it.
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{8,200}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def die(msg):
    sys.exit(f"keys: {msg}")


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def load(service):
    f = STORE / service
    if not f.exists():
        return []
    pairs = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            label, key = line.split("=", 1)
            pairs.append((label.strip(), key.strip()))
    return pairs


def save(service, pairs):
    write_private(STORE / service, "".join(f"{label}={key}\n" for label, key in pairs))


def active(service):
    env = SERVICES[service]["env"]
    if not env.exists():
        return None
    m = re.search(
        r'^(?:export\s+)?%s=["\']?([^"\'\s]+)' % SERVICES[service]["var"],
        env.read_text(encoding="utf-8"),
        re.M,
    )
    return m[1] if m else None


def mask(key):
    return key[:5] + "…" + key[-4:]


def service_arg(name):
    if name not in SERVICES:
        die(f"unknown service {name!r}; use: {', '.join(SERVICES)}")
    return name


def export_windows(var, value):
    # Windows counterpart of sourcing the env file from ~/.zshenv: apps started later inherit it.
    import ctypes
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as k:
        winreg.SetValueEx(k, var, 0, winreg.REG_SZ, value)
    # WM_SETTINGCHANGE: Explorer passes the new value to programs it starts from now on.
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x1A, 0, "Environment", 2, 5000, None
    )


def activate(service, label, key):
    cfg = SERVICES[service]
    write_private(cfg["env"], f"export {cfg['var']}={key}\n")
    if WINDOWS:
        export_windows(cfg["var"], key)
    cli = cfg.get("cli")
    if cli and cli.exists():
        data = json.loads(cli.read_text(encoding="utf-8"))
        data["apiKey"] = key
        write_private(cli, json.dumps(data))
    print(
        f"{service}: active = {label} ({mask(key)}). "
        "CLI uses it from the next command; MCP after restarting the Claude/Codex session."
    )


def cmd_list(service=None):
    for name in [service_arg(service)] if service else SERVICES:
        cur = active(name)
        pairs = load(name)
        print(f"{name}:" + ("" if pairs else " no stored keys"))
        for label, key in pairs:
            print(f"  {'*' if key == cur else ' '} {label:<16} {mask(key)}")


def cmd_add(service, label):
    service_arg(service)
    if not LABEL_RE.match(label):
        die("label: letters, digits, . _ - only")
    key = (
        getpass.getpass(f"{service} key for {label}: ")
        if sys.stdin.isatty()
        else sys.stdin.readline()
    ).strip()
    if not KEY_RE.match(key):
        die("this does not look like an API key (letters, digits, . _ - only)")
    pairs = [(l, k) for l, k in load(service) if l != label and k != key] + [
        (label, key)
    ]
    save(service, pairs)
    print(f"{service}: stored {label} ({mask(key)})")
    if not active(service):
        activate(service, label, key)


def cmd_import(service, label):
    key = active(service_arg(service))
    if not key:
        die(f"{service} has no active key to import")
    if not LABEL_RE.match(label):
        die("label: letters, digits, . _ - only")
    save(
        service,
        [(l, k) for l, k in load(service) if l != label and k != key] + [(label, key)],
    )
    print(f"{service}: stored active key as {label} ({mask(key)})")


def cmd_remove(service, label):
    pairs = load(service_arg(service))
    key = dict(pairs).get(label)
    if not key:
        die(f"{service} has no key {label!r}; see `keys list {service}`")
    save(service, [(l, k) for l, k in pairs if l != label])
    still = "; it stays active until you switch keys" if key == active(service) else ""
    print(f"{service}: removed {label} ({mask(key)}){still}")


def cmd_use(service, label):
    key = dict(load(service_arg(service))).get(label)
    if not key:
        die(f"{service} has no key {label!r}; see `keys list {service}`")
    activate(service, label, key)


def next_label(pairs, cur):
    keys = [k for _, k in pairs]
    i = (keys.index(cur) + 1) % len(pairs) if cur in keys else 0
    return pairs[i]


def cmd_next(service):
    pairs = load(service_arg(service))
    if not pairs:
        die(f"{service} has no stored keys")
    activate(service, *next_label(pairs, active(service)))


def credits(service, key, timeout=15):
    """(left, total, error): error is None on success, "HTTP 432" and the like otherwise."""
    req = urllib.request.Request(
        SERVICES[service]["usage"], headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return None, None, f"HTTP {e.code}"
    except OSError as e:
        return None, None, f"no answer: {e}"
    if service == "tavily":  # Tavily updates this counter with a delay
        a = d.get("account") or {}
        limit, used = a.get("plan_limit"), a.get("plan_usage")
        left = (
            limit - used if isinstance(limit, int) and isinstance(used, int) else None
        )
        return left, limit, None
    data = d.get("data") or {}
    return data.get("remainingCredits"), data.get("planCredits"), None


def label_of(service, key):
    return next((l for l, k in load(service) if k == key), "active")


def remaining(service, key):
    left, total, err = credits(service, key)
    if err:
        return err + (" (invalid key)" if err == "HTTP 401" else "")
    return f"{left} credits left of {total}"


def cmd_check(service=None, all_keys=False):
    for name in [service_arg(service)] if service else SERVICES:
        cur = active(name)
        pairs = (
            load(name)
            if all_keys
            else [(l, k) for l, k in load(name) if k == cur]
            or ([("active", cur)] if cur else [])
        )
        for label, key in pairs:
            print(
                f"{name} {'*' if key == cur else ' '} {label:<16} {remaining(name, key)}"
            )


# Start-up splash: a bubbling radioactive flask and the ULTRAS*LABS banner.
# Interactive UTF-8 terminals only; KEYS_NO_SPLASH=1 turns it off, Ctrl-C skips it.

BANNER_FONT = {  # figlet "ANSI Shadow"; the asterisk is drawn by hand
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
    "B": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    "*": ["       ", " ▄ █ ▄ ", "  ███  ", " ▀ █ ▀ ", "       ", "       "],
}
GLOW = [
    226,
    190,
    154,
    118,
    82,
    46,
]  # banner rows, 256-colour: radioactive yellow -> green
FLASK = [
    "     ╶╮   ╭╴     ",
    "      │   │      ",
    "      │   │      ",
    "      │   │      ",
    "     ╱     ╲     ",
    "    ╱       ╲    ",
    "   ╱         ╲   ",
    "  ╱           ╲  ",
    " ╱             ╲ ",
    " ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔ ",
]
BOTTOM = 8  # lowest flask row that holds liquid


def paint(color, text):
    return f"\x1b[38;5;{color}m{text}\x1b[0m"


KERN = {("L", "T"): 2}  # T tucks its bar over the empty top of L, else T looks far away


def banner_rows(word):
    """Banner rows as lists of (char, colour) cells."""
    rows = [[] for _ in range(6)]
    prev = None
    for letter in word:
        k = KERN.get((prev, letter), 0)
        for r, line in enumerate(BANNER_FONT[letter]):
            cells = [
                (ch, 226 if letter == "*" else 28 if ch in "╗║╔╝╚═" else GLOW[r])
                for ch in line
            ]
            if k:  # overlapping columns keep whichever letter has a glyph there
                tail = rows[r][-k:]
                del rows[r][-k:]
                cells[:k] = [b if b[0] != " " else a for a, b in zip(tail, cells)]
            rows[r] += cells
        prev = letter
    return rows


def flask_span(r):
    """Inner columns [start, end) of flask row r; rows -2 and -1 are the vapour above the lip."""
    if r < 0:
        return 5, 12
    walls = [i for i, ch in enumerate(FLASK[r]) if ch in "│╱╲╮╭"]
    return walls[0] + 1, walls[-1]


def step_bubbles(bubbles, level, rng):
    moved = []
    for r, x in bubbles:
        r -= 1
        if r >= -2:
            a, b = flask_span(r)
            # drift towards the flask axis (column 8) with a little wobble
            x += (x < 8) - (x > 8) if rng.random() < 0.5 else rng.choice((-1, 0, 1))
            moved.append((r, min(max(x, a), b - 1)))
    if level and rng.random() < 0.7:
        moved.append((BOTTOM, rng.randrange(*flask_span(BOTTOM))))
    return moved


def flask_frame(t, level, bubbles):
    """Two vapour rows plus the flask, `level` liquid rows deep, as coloured strings."""
    grid = [[(" ", 0)] * 17 for _ in range(2)] + [
        [(ch, 152) for ch in row] for row in FLASK
    ]
    for depth in range(level):
        r = BOTTOM - depth
        for x in range(*flask_span(r)):
            if depth == level - 1:
                cell = ("≈~"[(x + t) % 2], 118)  # the surface ripples
            else:
                cell = ("▒", 40) if depth == level - 2 else ("▓", 34)
            grid[r + 2][x] = cell
    for r, x in bubbles:
        in_liquid = r > BOTTOM - level
        grid[r + 2][x] = (
            ("o", 194) if in_liquid else ("°", 157) if r >= 0 else ("·", 71)
        )
    lines = [
        "".join(ch if ch == " " else paint(c, ch) for ch, c in row) for row in grid
    ]
    lines[8] += "   " + paint(226 if t % 4 < 2 else 136, "☢")  # blinking hazard sign
    return lines


def splash(out=sys.stdout):
    size = shutil.get_terminal_size()
    if (
        not out.isatty()
        or os.environ.get("KEYS_NO_SPLASH")
        or os.environ.get("TERM") == "dumb"
        or "utf" not in (out.encoding or "").lower()
    ):
        return
    words = ["ULTRAS*LABS"] if size.columns > 92 else ["ULTRAS", "*LABS"]
    banners = [banner_rows(w) for w in words]
    width = max(len(b[0]) for b in banners)
    height = 2 + len(FLASK) + 1 + 6 * len(banners) + 2
    if size.columns < width + 2 or size.lines < height + 2:
        return
    if WINDOWS:
        # Constant empty command, no input reaches the shell: the call only switches
        # the classic Windows console into ANSI escape mode.
        os.system("")
    rng, bubbles, drawn = random.Random(), [], 0
    pad = " " * ((width - 17) // 2)
    tagline = "☢  keys · Tavily / Firecrawl key switcher  ☢"
    try:
        out.write("\x1b[?25l")
        for t in range(31):
            level = min(5, 1 + t // 3)  # the flask fills during the first half
            bubbles = step_bubbles(bubbles, level, rng)
            reveal = max(0, t - 14) * width // 14  # then the banner wipes in
            lines = [pad + line for line in flask_frame(t, level, bubbles)] + [""]
            for b in banners:
                off = (width - len(b[0])) // 2
                for row in b:
                    cells = []
                    for x, (ch, c) in enumerate(row, off):
                        if ch != " " and x < reveal:
                            cells.append(paint(c, ch))
                        elif ch != " " and x < reveal + 3:
                            cells.append(paint(46, rng.choice("░▒▓")))
                        else:
                            cells.append(" ")
                    lines.append(" " * off + "".join(cells))
            pad_tag = " " * ((width - len(tagline)) // 2)
            lines += ["", pad_tag + paint(245, tagline) if t == 30 else ""]
            if drawn:
                out.write(f"\x1b[{drawn}F")
            out.write("".join(f"\x1b[2K{line}\n" for line in lines))
            out.flush()
            drawn = len(lines)
            time.sleep(0.04)
        time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h\x1b[0m")
        out.flush()


# Interactive mode: `keys` with no arguments in a terminal. A full-screen menu redrawn in
# place on the alternate screen, so nothing piles up in the scrollback: up/down move,
# Enter or right picks, Esc or left goes back, q quits, a digit jumps straight to an item.
# The screen animates (gradient banner, spinners, titles decoding out of noise), so keys
# are polled with a short timeout and every tick repaints the whole frame.

CSI = "\x1b["
UI = {
    "rule": 238,
    "title": 226,
    "dim": 245,
    "on": 118,
    "ok": 114,
    "warn": 220,
    "err": 203,
}
TRUECOLOR = os.environ.get("COLORTERM") in ("truecolor", "24bit")
GRADIENT = [  # silver: dark steel up to white and back
    (120, 126, 138),
    (185, 190, 200),
    (245, 247, 250),
    (255, 255, 255),
    (210, 214, 222),
    (150, 155, 166),
]
LIQUID = [(0, 110, 40), (40, 200, 70), (150, 255, 60), (40, 200, 70)]
ARROW_SPEED = 0.08  # rainbow loops per second for the selection arrow: one every 12.5 s
RAINBOW = [  # the selection arrow
    (255, 214, 0),
    (170, 255, 0),
    (0, 255, 120),
    (0, 200, 255),
    (140, 90, 255),
    (255, 70, 170),
]
GAP = 3  # columns on each side of a credit bar
TRACK = 238  # the empty part of a credit bar
HEAD_W = len(banner_rows("ULTRAS*LABS")[0]) + 4 + 17  # banner, gap, flask: widest
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
NOISE = "▓▒░<>/\\#*+=_"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TICK = 0.08  # seconds per frame while waiting for a key
CREDITS = {}  # (service, key) -> (left, total, error); None while the request runs
TTY = None  # terminal attributes saved by interactive(); None outside it
SCREEN = None  # (terminal size, lines) of the last frame; None repaints everything
ASLEEP = False  # the terminal window lost focus: animation paused


def fg(rgb):
    if TRUECOLOR:
        return "%s38;2;%d;%d;%dm" % (CSI, *rgb)
    r, g, b = (c * 5 // 255 for c in rgb)  # nearest colour of the 256-colour cube
    return f"{CSI}38;5;{16 + 36 * r + 6 * g + b}m"


def gradient(pos, stops=GRADIENT):
    """Colour at `pos` along the looping `stops`; 1.0 is one full loop."""
    f = pos % 1 * len(stops)
    i, k = int(f), f - int(f)
    a, b = stops[i], stops[(i + 1) % len(stops)]
    return tuple(int(x + (y - x) * k) for x, y in zip(a, b))


def vlen(text):
    return len(ANSI.sub("", text))


def runs(cells):
    """(colour code, char) cells as one string, a code written only where the colour changes."""
    out, last = [], None
    for code, ch in cells:
        if ch != " " and code != last:
            out.append(code)
            last = code
        out.append(ch)
    return "".join(out) + CSI + "0m"


def coarse(rgb):
    # 32 steps per channel: neighbouring cells share a colour code far more often
    return tuple(c & ~7 for c in rgb)


@lru_cache(maxsize=None)
def silver(step, glow, shadow):
    """Colour code for gradient `step` of 128, glint `glow` of 8, darker if `shadow`.
    Cached: after the first loop every banner cell is a lookup, not colour maths."""
    rgb = gradient(step / 128)
    rgb = tuple(int(c + (255 - c) * glow / 8) for c in rgb)
    if shadow:  # the shadow stays darker, so the letters look raised
        rgb = tuple(c * 45 // 100 for c in rgb)
    return fg(coarse(rgb))


@lru_cache(maxsize=None)
def liquid(step):
    return fg(coarse(gradient(step / 64, LIQUID)))


LOGO = [[ch for ch, _ in row] for row in banner_rows("ULTRAS*LABS")]


def banner(t, width):
    """ULTRAS*LABS: a silver gradient flowing sideways with a glint sweeping across it."""
    w = len(LOGO[0])
    if width < w:  # no room for the big letters: one spaced-out line instead
        text = "▓▒░ U L T R A S * L A B S ░▒▓"
        cells = [
            (fg(coarse(gradient(x / len(text) - t * 0.15))), ch)
            for x, ch in enumerate(text)
        ]
        return [" " * max(0, (width - len(text)) // 2) + runs(cells)]
    sweep = int(t * 45 % (w + 50)) - 25
    shift = int(t * 0.12 * 128)
    lines = []
    for r, row in enumerate(LOGO):
        cells = [
            (
                None
                if ch == " "
                else silver(
                    (x * 102 // w + r * 4 - shift) % 128,  # 102 / 128 = 0.8 of a loop
                    max(0, 7 - abs(x - sweep)) * 6 // 7,  # glint: up to 6 / 8 white
                    ch in "╗║╔╝╚═",
                ),
                ch,
            )
            for x, ch in enumerate(row)
        ]
        lines.append(" " * ((width - w) // 2) + runs(cells))
    return lines


def flask_still(t):
    """The splash flask without bubbles: silver glass, liquid in a slowly flowing gradient."""
    glass, lines, shift = f"{CSI}38;5;250m", [], int(t * 0.2 * 64)
    for r, row in enumerate(FLASK):
        cells = [(glass, ch) for ch in row]
        depth = BOTTOM - r  # 0 at the bottom; the flask holds five rows of liquid
        if 0 <= depth < 5:
            for x in range(*flask_span(r)):
                code = liquid((depth * 8 + x * 2 - shift) % 64)
                cells[x] = (code, "≈" if depth == 4 else "█")
        lines.append(runs(cells))
    return lines


def header(t, width):
    """Banner with the flask in the right corner, centred in `width`."""
    w = len(LOGO[0])
    big, glass = banner(t, w), flask_still(t)
    rows = [
        (big[r - 3] if 3 <= r < 9 else " " * w) + "    " + glass[r]
        for r in range(len(glass))
    ]
    return [" " * ((width - HEAD_W) // 2) + row for row in rows]


def decode(text, age):
    """`text` typing itself out of noise during the first moments after it appears."""
    n = int(age / 0.025)
    noise = "".join(ch if ch == " " else random.choice(NOISE) for ch in text[n:])
    return paint(UI["title"], text[:n]) + paint(46, noise)


def box(title, rows, width):
    def border(s):
        return paint(UI["rule"], s)

    if title:
        fill = "─" * (width - 5 - vlen(title))
        top = border("╭─") + f" {title} " + border(fill + "╮")
    else:
        top = border("╭" + "─" * (width - 2) + "╮")
    body = [
        border("│")
        + " "
        + row
        + " " * max(0, width - 4 - vlen(row))
        + " "
        + border("│")
        for row in rows
    ]
    return [top, *body, border("╰" + "─" * (width - 2) + "╯")]


def fetch_credits(service, key):
    CREDITS[(service, key)] = credits(service, key)


def refresh_credits():
    """Ask once for the credits of every stored key; answers arrive in the background."""
    for name in SERVICES:
        for _, key in load(name):
            if (name, key) not in CREDITS:
                CREDITS[(name, key)] = None
                threading.Thread(
                    target=fetch_credits, args=(name, key), daemon=True
                ).start()


def stats(state):
    left, total, _ = state
    return f"{left}/{total}"


def credit_cell(state, t, bar, stats_w):
    """`bar` cells of the credit bar, GAP spaces, the numbers right-aligned in `stats_w`."""
    if state is None:
        return paint(UI["dim"], SPINNER[int(t * 12) % len(SPINNER)] + " checking")
    left, total, err = state
    if err:
        return paint(UI["err"], "✗ " + err[:26])
    if not isinstance(left, int) or not total:
        return paint(UI["dim"], f"{left} left")
    ratio = max(0.0, min(1.0, left / total))
    color = UI["ok"] if ratio > 0.5 else UI["warn"] if ratio > 0.2 else UI["err"]
    # Box-drawing lines are drawn by the terminal itself, cell-exact and centred on the text
    # line: segments meet side by side, rows stay apart. Whole cells only, so the fill runs
    # straight into the track with no half-empty cell between them.
    full = int(ratio * bar + 0.5)
    return (
        paint(color, "━" * full)
        + paint(TRACK, "━" * (bar - full))
        + " " * (GAP + stats_w - len(stats(state)))
        + paint(color, str(left))
        + paint(UI["dim"], f"/{total}")
    )


def overview(t, width):
    """Keys of every service; credit bars stretch so the numbers end at the right edge."""
    ready = [s for s in CREDITS.values() if s and not s[2] and isinstance(s[0], int)]
    stats_w = max([9] + [len(stats(s)) for s in ready])
    bar = max(5, width - 4 - 26 - 2 * GAP - stats_w)  # 26 = mark, label and masked key
    rows = []
    for name in SERVICES:
        pairs, now = load(name), active(name)
        empty = "" if pairs else paint(UI["dim"], "  no stored keys")
        rows.append(paint(UI["title"], name) + empty)
        for label, key in pairs:
            on = key == now
            name_cell = f"{label[:12]:<12}"
            rows.append(
                " "
                + (paint(UI["on"], "●") if on else paint(UI["dim"], "○"))
                + " "
                + (paint(UI["on"], name_cell) if on else name_cell)
                + " "
                + paint(UI["dim"], mask(key))
                + " " * GAP
                + credit_cell(CREDITS.get((name, key)), t, bar, stats_w)
            )
    return rows


def draw(title, items=(), cur=0, notes=(), age=1.0):
    """Repaint the whole screen in place: banner, keys with credits, menu, result, hints."""
    refresh_credits()
    size = shutil.get_terminal_size()
    width = max(44, min(size.columns - 2, HEAD_W))
    t = time.monotonic()
    rows = []
    for i, item in enumerate(items):
        num = paint(UI["dim"], f"{i + 1} ")
        if i == cur:
            bg = CSI + "48;5;236m"
            arrow = fg(gradient(t * ARROW_SPEED, RAINBOW))
            rows.append(
                f"{bg}{arrow}❯ {num}{bg}{CSI}1;38;5;231m"
                + item.ljust(width - 8)
                + CSI
                + "0m"
            )
        else:
            rows.append(f"  {num}{item}")
    body = box(paint(UI["dim"], "keys"), overview(t, width), width)
    body += box(decode(title, age), rows or [""], width)
    if notes:
        lines = []
        for ok, text in notes:
            color, mark = (UI["ok"], "✓ ") if ok else (UI["err"], "✗ ")
            for j, part in enumerate(textwrap.wrap(text, width - 6) or [""]):
                lines.append(paint(color, (mark if j == 0 else "  ") + part))
        body += box(paint(UI["dim"], "result"), lines, width)
    body.append(paint(UI["dim"], "  ↑↓ move · enter select · esc back · q quit"))
    room = size.lines - len(body) - 2
    if width >= HEAD_W and room >= len(FLASK):
        head = header(t, width)
    else:  # no room for the flask: the banner alone, or its one-line form
        head = banner(t, width if room >= 6 else 0)
    lines = [""] + head + [""] + body
    # Only lines that differ from the previous frame go out: borders, menu and hints stay
    # put while the banner, the flask and the spinners move.
    global SCREEN
    out = [CSI + "?2026h"]  # synchronized update: the terminal shows complete frames
    if SCREEN is None or SCREEN[0] != size:
        out.append(CSI + "H" + CSI + "2J")
        SCREEN = (size, [])
    shown = SCREEN[1]
    for i, line in enumerate(lines):
        if i >= len(shown) or shown[i] != line:
            out.append(f"{CSI}{i + 1};1H{line}{CSI}K")
    if len(lines) < len(shown):
        out.append(f"{CSI}{len(lines) + 1};1H{CSI}J")
    SCREEN = (size, lines)
    out.append(f"{CSI}{len(lines) + 1};1H{CSI}?2026l")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def read_key(timeout=None):
    """One keypress as a token (up/down/left/right/enter/esc/backspace or the character);
    None if nothing was pressed within `timeout` seconds."""
    if WINDOWS:
        import msvcrt

        end = time.monotonic() + (timeout or 0)
        while not msvcrt.kbhit():
            if timeout is not None and time.monotonic() >= end:
                return None
            time.sleep(0.01)
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrows arrive as a two-character scan code
            arrows = {"H": "up", "P": "down", "K": "left", "M": "right"}
            return arrows.get(msvcrt.getwch(), "")
    else:
        import select

        fd = sys.stdin.fileno()  # interactive() put the terminal into cbreak mode
        if not select.select([fd], [], [], timeout)[0]:
            return None
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b" and select.select([fd], [], [], 0.05)[0]:
            seq = os.read(fd, 8).decode(errors="ignore")  # "[A" or "OA" for up
            arrows = {"A": "up", "B": "down", "C": "right", "D": "left"}
            # "[I" / "[O": the terminal window gained / lost focus (mode ?1004)
            arrows.update(I="focus", O="blur")
            return arrows.get(seq[1:2], "")
    if ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
        raise KeyboardInterrupt
    named = {
        "\r": "enter",
        "\n": "enter",
        "\x1b": "esc",
        "\x7f": "backspace",
        "\x08": "backspace",
    }
    return named.get(ch, ch)


def menu(title, items, cur=0, notes=()):
    """Index of the item picked with the arrows (or a digit), None for Esc / left / 0."""
    global ASLEEP
    shown = time.monotonic()
    while True:
        draw(title, items, cur, notes, time.monotonic() - shown)
        # in a window without focus nothing animates: wait for a key, use no CPU
        key = read_key(None if ASLEEP else TICK)
        if key in ("blur", "focus"):
            ASLEEP = key == "blur"
            continue
        if key is None:
            continue
        if key in ("up", "k"):
            cur = (cur - 1) % len(items)
        elif key in ("down", "j"):
            cur = (cur + 1) % len(items)
        elif key in ("enter", "right"):
            return cur
        elif key in ("esc", "left", "backspace", "0"):
            return None
        elif key == "q":
            raise KeyboardInterrupt
        elif key.isdigit() and int(key) <= len(items):
            return int(key) - 1


def failed(exit):
    return [(False, str(exit.code).removeprefix("keys: "))]


def quiet(fn, *args):
    """Run a command and return what it printed as (ok, line) results."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*args)
    except SystemExit as e:
        return failed(e)
    bad = ("HTTP", "no answer")
    lines = buf.getvalue().strip().splitlines()
    return [(not any(b in line for b in bad), line) for line in lines]


@contextmanager
def cooked():
    """Normal line input (echo, backspace) while a label or a key is typed."""
    if TTY is None:
        yield
        return
    import termios
    import tty

    fd = sys.stdin.fileno()
    termios.tcsetattr(fd, termios.TCSADRAIN, TTY)
    sys.stdout.write(CSI + "?25h")
    try:
        yield
    finally:
        global SCREEN
        SCREEN = None  # typed text is on the screen now: repaint everything next frame
        tty.setcbreak(fd)
        sys.stdout.write(CSI + "?25l")


def pick_service(title):
    i = menu(title, list(SERVICES))
    return None if i is None else list(SERVICES)[i]


def pick_key(title, service):
    pairs, now = load(service), active(service)
    if not pairs:
        die(f"{service} has no stored keys")
    keys = [k for _, k in pairs]
    rows = [
        f"{l[:14]:<14} {mask(k)}" + ("   active" if k == now else "") for l, k in pairs
    ]
    i = menu(f"{title} · {service}", rows, keys.index(now) if now in keys else 0)
    return None if i is None else pairs[i]


def act_use():
    service = pick_service("switch key · service")
    pair = service and pick_key("switch key", service)
    return quiet(activate, service, *pair) if pair else []


def act_next():
    service = pick_service("next key · service")
    return quiet(cmd_next, service) if service else []


def act_refresh():
    CREDITS.clear()  # the next frame asks again; spinners show until answers arrive
    return []


def act_store(title, cmd):
    service = pick_service(f"{title} · service")
    if not service:
        return []
    draw(f"{title} · {service}")
    with cooked():
        label = input("  label (empty = cancel): ").strip()
        return quiet(cmd, service, label) if label else []


def act_remove():
    service = pick_service("remove key · service")
    pair = service and pick_key("remove key", service)
    if not pair:
        return []
    label = pair[0]
    if menu(f"remove {label}?", ["No, keep it", f"Yes, remove {label}"]) != 1:
        return []
    return quiet(cmd_remove, service, label)


ACTIONS = [
    ("Switch to a stored key", act_use),
    ("Next key", act_next),
    ("Refresh credits", act_refresh),
    ("Add a key", lambda: act_store("add key", cmd_add)),
    (
        "Store the active key under a label",
        lambda: act_store("store active key", cmd_import),
    ),
    ("Remove a stored key", act_remove),
]


def interactive():
    global TTY
    if WINDOWS:
        os.system("")  # same constant call as in splash(): turns on ANSI escapes
    # Alternate screen: whatever happens here leaves the shell scrollback untouched.
    # ?1004h: the terminal reports focus changes, so a hidden menu stops animating
    sys.stdout.write(CSI + "?1049h" + CSI + "?1004h" + CSI + "H" + CSI + "2J")
    if not WINDOWS and sys.stdin.isatty():
        import termios
        import tty

        # cbreak: keys arrive one by one and unechoed, Ctrl-C still interrupts
        TTY = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    try:
        splash()
        sys.stdout.write(CSI + "?25l")
        notes, cur = [], 0
        while True:
            i = menu("menu", [name for name, _ in ACTIONS], cur, notes)
            if i is None:
                return
            cur = i
            try:
                notes = ACTIONS[i][1]() or []
            except SystemExit as e:  # die() inside an action: show it, stay in the menu
                notes = failed(e)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if TTY is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, TTY)
            TTY = None
        sys.stdout.write(CSI + "?25h" + CSI + "?1004l" + CSI + "?1049l")
        sys.stdout.flush()


def main(argv):
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        return interactive()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd, args = argv[0], argv[1:]
    all_keys = "--all" in args
    args = [a for a in args if a != "--all"]
    table = {
        "list": (cmd_list, 0, 1),
        "add": (cmd_add, 2, 2),
        "import": (cmd_import, 2, 2),
        "use": (cmd_use, 2, 2),
        "next": (cmd_next, 1, 1),
        "remove": (cmd_remove, 2, 2),
        "check": (cmd_check, 0, 1),
    }
    if cmd not in table:
        die(f"unknown command {cmd!r}; see `keys --help`")
    fn, lo, hi = table[cmd]
    if not lo <= len(args) <= hi:
        die(f"wrong arguments; see `keys --help`")
    if cmd == "check":
        cmd_check(args[0] if args else None, all_keys)
    else:
        fn(*args)


if __name__ == "__main__":
    main(sys.argv[1:])
