#!/usr/bin/env python3
"""
Tank Duel - 2-4 player LAN tank arena built on lanplay.

Steer your tank, bounce shots off the walls, be the last one alive.
First to WIN_SCORE round wins takes the match.

  python tankduel.py                  menu: type your name, [F1] host, or type an IP + ENTER to join
  python tankduel.py --host           start hosting immediately
  python tankduel.py --join 192.168.1.20
  python tankduel.py --name Alex

Controls:  W/S or UP/DOWN = drive,  A/D or LEFT/RIGHT = turn,  SPACE = fire
           ENTER (host, in lobby) = start match,  ESC = leave

Design (see lanplay README, section 4): host-authoritative.
  client -> host : {"t":"input","turn":-1|0|1,"move":-1|0|1,"fire":bool}  (30 Hz)
  host  -> all   : {"t":"state", ...whole world...}                        (30 Hz)
All game rules live in `World` (pure Python, no pygame), so they are testable headless.
"""
import argparse
import math
import time

GAME_ID = "tankduel"
MAX_PLAYERS = 4
NAME_MAX = 12

# ----------------------------------------------------------------------------
# Game rules (no pygame, no networking)
# ----------------------------------------------------------------------------
W, H = 960, 600
TANK_R = 16
BULLET_R = 4
TURN_SPEED = 3.2          # rad/s
MOVE_SPEED = 170.0        # px/s (reverse is 60% of this)
BULLET_SPEED = 420.0
FIRE_COOLDOWN = 0.45
BULLET_LIFE = 3.5
MAX_BOUNCES = 2
START_HP = 3
WIN_SCORE = 3
ROUND_PAUSE = 2.5
MATCH_PAUSE = 6.0
SELF_HIT_GRACE = 0.25     # a shot can't hurt its owner this soon after firing
SUBSTEP = 1 / 60

# (x, y, w, h)
WALLS = [
    (0, 0, W, 12), (0, H - 12, W, 12), (0, 0, 12, H), (W - 12, 0, 12, H),   # border
    (W // 2 - 10, H // 2 - 90, 20, 180),                                     # centre pillar
    (180, 140, 20, 120), (W - 200, H - 260, 20, 120),
    (300, H - 160, 160, 20), (W - 460, 140, 160, 20),
]
SPAWNS = [(70, 70), (W - 70, H - 70), (W - 70, 70), (70, H - 70)]
NO_INPUT = {"turn": 0, "move": 0, "fire": False}


def point_in_rect(x, y, r):
    return r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]


def circle_rect(x, y, rad, rect):
    """Push a circle out of a rect. Returns (x, y)."""
    rx, ry, rw, rh = rect
    cx = min(max(x, rx), rx + rw)
    cy = min(max(y, ry), ry + rh)
    dx, dy = x - cx, y - cy
    d2 = dx * dx + dy * dy
    if d2 >= rad * rad:
        return x, y
    if d2 > 1e-9:
        d = math.sqrt(d2)
        n = rad - d
        return x + dx / d * n, y + dy / d * n
    # centre is inside the rect: leave through the nearest side
    l, r_, t, b = x - rx, rx + rw - x, y - ry, ry + rh - y
    m = min(l, r_, t, b)
    if m == l:
        return rx - rad, y
    if m == r_:
        return rx + rw + rad, y
    if m == t:
        return x, ry - rad
    return x, ry + rh + rad


def clean_name(n, fallback="Player"):
    n = "".join(ch for ch in str(n) if ch.isprintable()).strip()[:NAME_MAX]
    return n or fallback


class World:
    """The one true game state. Only the host runs this."""

    def __init__(self):
        self.players = {}        # int id -> dict(name,slot,x,y,a,hp,alive,score,cd)
        self.inputs = {}         # int id -> sanitised input dict
        self.bullets = []        # dicts: x,y,vx,vy,owner,age,bounces
        self.phase = "lobby"     # lobby | play | roundover | matchover
        self.timer = 0.0
        self.round = 0
        self.winner = None       # round winner id (or None = draw)
        self.champion = None     # match winner id
        self.events = []         # effects to ship to clients with next snapshot
        self.fx_local = []       # effects for the host's own screen

    # -- roster ---------------------------------------------------------
    def add_player(self, pid, name):
        if pid in self.players:
            return
        used = {p["slot"] for p in self.players.values()}
        slot = next((s for s in range(MAX_PLAYERS) if s not in used), len(used))
        self.players[pid] = {"name": clean_name(name, "Player %d" % (pid + 1)),
                             "slot": slot, "x": -100.0, "y": -100.0, "a": 0.0,
                             "hp": 0, "alive": False, "score": 0, "cd": 0.0}

    def remove_player(self, pid):
        self.players.pop(pid, None)
        self.inputs.pop(pid, None)
        if len(self.players) < 2 and self.phase != "lobby":
            self.phase = "lobby"
            self.bullets = []

    def set_input(self, pid, m):
        if pid not in self.players or not isinstance(m, dict):
            return

        def sgn(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return 0
            return 1 if v > 0.3 else -1 if v < -0.3 else 0

        self.inputs[pid] = {"turn": sgn(m.get("turn")), "move": sgn(m.get("move")),
                            "fire": bool(m.get("fire"))}

    # -- lifecycle ------------------------------------------------------
    def start_match(self):
        if len(self.players) < 2 or self.phase != "lobby":
            return False
        for p in self.players.values():
            p["score"] = 0
        self.round = 0
        self.champion = None
        self.start_round()
        return True

    def start_round(self):
        self.round += 1
        self.bullets = []
        self.winner = None
        for p in self.players.values():
            sx, sy = SPAWNS[p["slot"] % len(SPAWNS)]
            p.update(x=float(sx), y=float(sy), a=math.atan2(H / 2 - sy, W / 2 - sx),
                     hp=START_HP, alive=True, cd=0.4)
        self.phase = "play"
        self.timer = 0.0

    def _emit(self, x, y, kind):
        fx = [round(x, 1), round(y, 1), kind]
        self.events.append(fx)
        self.fx_local.append(fx)

    # -- simulation -----------------------------------------------------
    def step(self, dt):
        dt = max(0.0, min(dt, 0.1))
        n = max(1, math.ceil(dt / SUBSTEP))
        for _ in range(n):
            self._tick(dt / n)

    def _tick(self, dt):
        if self.phase == "play":
            self._move_tanks(dt)
            self._move_bullets(dt)
            self._hit_tanks()
            alive = [pid for pid, p in self.players.items() if p["alive"]]
            if len(alive) <= 1:
                self.phase = "roundover"
                self.timer = ROUND_PAUSE
                self.winner = alive[0] if alive else None
                if self.winner is not None:
                    self.players[self.winner]["score"] += 1
        elif self.phase in ("roundover", "matchover"):
            self._move_bullets(dt)
            self.timer -= dt
            if self.timer <= 0:
                self._after_pause()

    def _after_pause(self):
        if self.phase == "roundover":
            best = max(self.players.items(), key=lambda kv: kv[1]["score"], default=None)
            if best and best[1]["score"] >= WIN_SCORE:
                self.champion = best[0]
                self.phase = "matchover"
                self.timer = MATCH_PAUSE
            else:
                self.start_round()
        else:
            self.phase = "lobby"
            self.bullets = []

    def _move_tanks(self, dt):
        for pid, p in self.players.items():
            if not p["alive"]:
                continue
            inp = self.inputs.get(pid, NO_INPUT)
            p["cd"] = max(0.0, p["cd"] - dt)
            p["a"] = (p["a"] + inp["turn"] * TURN_SPEED * dt) % (2 * math.pi)
            mv = inp["move"]
            if mv:
                sp = mv * (MOVE_SPEED if mv > 0 else MOVE_SPEED * 0.6)
                p["x"] += math.cos(p["a"]) * sp * dt
                p["y"] += math.sin(p["a"]) * sp * dt
            for _ in range(2):
                for r in WALLS:
                    p["x"], p["y"] = circle_rect(p["x"], p["y"], TANK_R, r)
            if inp["fire"] and p["cd"] <= 0:
                p["cd"] = FIRE_COOLDOWN
                ca, sa = math.cos(p["a"]), math.sin(p["a"])
                bx, by = p["x"] + ca * (TANK_R + 8), p["y"] + sa * (TANK_R + 8)
                if any(point_in_rect(bx, by, r) for r in WALLS):
                    bx, by = p["x"], p["y"]        # muzzle is in a wall: fire from the centre
                self.bullets.append({"x": bx, "y": by, "vx": ca * BULLET_SPEED,
                                     "vy": sa * BULLET_SPEED, "owner": pid,
                                     "age": 0.0, "bounces": 0})
        # tanks don't drive through each other
        live = [p for p in self.players.values() if p["alive"]]
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                a, b = live[i], live[j]
                dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                d = math.hypot(dx, dy)
                if 1e-6 < d < 2 * TANK_R:
                    push = (2 * TANK_R - d) / 2
                    ux, uy = dx / d, dy / d
                    a["x"] -= ux * push; a["y"] -= uy * push
                    b["x"] += ux * push; b["y"] += uy * push

    def _move_bullets(self, dt):
        keep = []
        for b in self.bullets:
            b["age"] += dt
            if b["age"] > BULLET_LIFE:
                continue
            ox, oy = b["x"], b["y"]
            nx, ny = ox + b["vx"] * dt, oy + b["vy"] * dt
            wall = next((r for r in WALLS if point_in_rect(nx, ny, r)), None)
            if wall is None:
                b["x"], b["y"] = nx, ny
                keep.append(b)
                continue
            b["bounces"] += 1
            self._emit(ox, oy, "spark")
            if b["bounces"] > MAX_BOUNCES:
                continue
            hit_x = point_in_rect(nx, oy, wall)
            hit_y = point_in_rect(ox, ny, wall)
            if hit_x and not hit_y:
                b["vx"] = -b["vx"]
            elif hit_y and not hit_x:
                b["vy"] = -b["vy"]
            else:
                b["vx"], b["vy"] = -b["vx"], -b["vy"]
            keep.append(b)
        self.bullets = keep

    def _hit_tanks(self):
        keep = []
        lim = (TANK_R + BULLET_R) ** 2
        for b in self.bullets:
            hit = False
            for pid, p in self.players.items():
                if not p["alive"]:
                    continue
                if pid == b["owner"] and b["age"] < SELF_HIT_GRACE:
                    continue
                if (p["x"] - b["x"]) ** 2 + (p["y"] - b["y"]) ** 2 < lim:
                    p["hp"] -= 1
                    hit = True
                    if p["hp"] <= 0:
                        p["alive"] = False
                        self._emit(p["x"], p["y"], "boom")
                    else:
                        self._emit(b["x"], b["y"], "hit")
                    break
            if not hit:
                keep.append(b)
        self.bullets = keep

    # -- network snapshot -----------------------------------------------
    def snapshot(self, clear_events=True):
        snap = {
            "t": "state", "phase": self.phase, "timer": round(self.timer, 2),
            "round": self.round, "winner": self.winner, "champion": self.champion,
            "players": {str(pid): {"name": p["name"], "slot": p["slot"],
                                   "x": round(p["x"], 1), "y": round(p["y"], 1),
                                   "a": round(p["a"], 3), "hp": p["hp"],
                                   "alive": p["alive"], "score": p["score"]}
                        for pid, p in self.players.items()},
            "bullets": [[round(b["x"], 1), round(b["y"], 1), round(b["vx"], 1),
                         round(b["vy"], 1), b["owner"]] for b in self.bullets],
            "fx": list(self.events),
        }
        if clear_events:
            self.events = []
        return snap


class View:
    """What a screen draws. Clients fill it from snapshots; the host mirrors its World."""

    def __init__(self):
        self.phase, self.timer, self.round = "lobby", 0.0, 0
        self.winner = self.champion = None
        self.players = {}        # int id -> dict
        self.bullets = []        # [x, y, vx, vy, owner]
        self.disp = {}           # int id -> [x, y, a]  smoothed draw position
        self.recv = time.monotonic()
        self.have_state = False

    def apply(self, snap, now):
        """Load a network snapshot. Returns the list of new effects (or [])."""
        try:
            players = {int(k): dict(v) for k, v in snap["players"].items()}
            bullets = [[float(b[0]), float(b[1]), float(b[2]), float(b[3]), b[4]]
                       for b in snap["bullets"]]
            phase, timer, rnd = str(snap["phase"]), float(snap["timer"]), int(snap["round"])
            for p in players.values():
                float(p["x"]); float(p["y"]); float(p["a"]); p["name"]; p["slot"]
                p["hp"]; p["alive"]; p["score"]
            fx = [list(f) for f in snap.get("fx", [])]
        except (KeyError, ValueError, TypeError, AttributeError, IndexError):
            return []
        self.phase, self.timer, self.round = phase, timer, rnd
        self.winner, self.champion = snap.get("winner"), snap.get("champion")
        self.players, self.bullets, self.recv = players, bullets, now
        self.have_state = True
        for pid in list(self.disp):
            if pid not in players:
                del self.disp[pid]
        for pid, p in players.items():
            d = self.disp.get(pid)
            if d is None or math.hypot(d[0] - p["x"], d[1] - p["y"]) > 120:
                self.disp[pid] = [p["x"], p["y"], p["a"]]
        return fx

    def update(self, dt):
        """Ease drawn positions toward the latest authoritative ones."""
        k = 1 - math.exp(-22 * dt)
        for pid, p in self.players.items():
            d = self.disp.setdefault(pid, [p["x"], p["y"], p["a"]])
            d[0] += (p["x"] - d[0]) * k
            d[1] += (p["y"] - d[1]) * k
            d[2] += (((p["a"] - d[2] + math.pi) % (2 * math.pi)) - math.pi) * k

    def mirror(self, world):
        """Host: show the live world exactly (no smoothing, no extrapolation)."""
        self.phase, self.timer, self.round = world.phase, world.timer, world.round
        self.winner, self.champion = world.winner, world.champion
        self.players = world.players
        self.bullets = [[b["x"], b["y"], 0.0, 0.0, b["owner"]] for b in world.bullets]
        self.disp = {pid: [p["x"], p["y"], p["a"]] for pid, p in world.players.items()}
        self.recv = time.monotonic()
        self.have_state = True


# ----------------------------------------------------------------------------
# Networking glue (still no pygame - takes lanplay events, easy to test)
# ----------------------------------------------------------------------------
def host_process(world, events):
    for ev in events:
        if ev.type == "connected":
            name = ev.data.get("name") if isinstance(ev.data, dict) else None
            world.add_player(ev.peer, name or "Player")
        elif ev.type == "disconnected":
            world.remove_player(ev.peer)
        elif ev.type == "message":
            m = ev.data
            if isinstance(m, dict) and m.get("t") == "input":
                world.set_input(ev.peer, m)          # unknown "t" values are ignored


def client_process(view, events, now):
    """Returns (new_fx, statuses). statuses: [("connected", data) | ("failed", reason) | ("disconnected", None)]"""
    fx, statuses = [], []
    for ev in events:
        if ev.type == "message":
            m = ev.data
            if isinstance(m, dict) and m.get("t") == "state":
                fx += view.apply(m, now)
        elif ev.type in ("connected", "failed", "disconnected"):
            statuses.append((ev.type, ev.data))
    return fx, statuses


# ----------------------------------------------------------------------------
# The pygame front end
# ----------------------------------------------------------------------------
COLORS = [(80, 200, 120), (90, 160, 255), (255, 190, 70), (240, 100, 160)]
BG = (24, 26, 33)
WALL_C = (88, 94, 112)


def run(args):
    import random
    import pygame
    from lanplay import Host, Client, Ticker, discover_async, get_local_ip

    pygame.init()
    pygame.display.set_caption("Tank Duel")
    screen = pygame.display.set_mode((W, H))
    clock = pygame.time.Clock()
    f_big, f_mid, f_small = (pygame.font.Font(None, s) for s in (84, 40, 26))

    def text(surf, s, font, color, pos, center=False):
        img = font.render(s, True, color)
        r = img.get_rect()
        if center:
            r.center = pos
        else:
            r.topleft = pos
        surf.blit(img, r)

    name = (args.name or "Player")[:NAME_MAX]     # editable in the menu; cleaned when used
    focus = "ip" if (args.name or args.join) else "name"   # which menu field receives typing
    state = "menu"                    # menu | connecting | game
    net = world = None
    view = View()
    ip_text = args.join or ""
    message = ""
    scan = discover_async(GAME_ID)
    host_ip = ""
    particles = []
    send_tick = Ticker(30)
    connect_started = 0.0

    def close_net():
        nonlocal net
        if net is not None:
            try:
                net.close()
            except Exception:
                pass
            net = None

    def leave(msg=""):
        nonlocal state, message, scan, world, view, particles
        close_net()
        state, message, world, view, particles = "menu", msg, None, View(), []
        scan = discover_async(GAME_ID)

    def start_host():
        nonlocal net, world, state, host_ip, message, view
        try:
            h = Host(GAME_ID, "%s's room" % clean_name(name), max_players=MAX_PLAYERS)
            h.start()
        except OSError as ex:
            message = "Can't host: %s" % ex
            return
        net, world, view, state = h, World(), View(), "game"
        world.add_player(0, clean_name(name))
        host_ip = get_local_ip()
        message = ""

    def start_join(ip, port=None):
        nonlocal net, state, view, connect_started, message
        c = Client(GAME_ID, clean_name(name))   # single-use: new one per attempt
        if port:
            c.connect_async(ip, port)
        else:
            c.connect_async(ip)
        net, view, state, message = c, View(), "connecting", ""
        connect_started = time.monotonic()

    def burst(x, y, kind):
        n, col, spd = {"boom": (30, (255, 150, 50), 260), "hit": (10, (255, 220, 120), 160),
                       "spark": (4, (230, 230, 240), 120)}.get(kind, (4, (255, 255, 255), 100))
        for _ in range(n):
            a, s = random.uniform(0, 6.283), random.uniform(0.3, 1.0) * spd
            particles.append([x, y, math.cos(a) * s, math.sin(a) * s, random.uniform(0.25, 0.6), col])

    def draw_tank(surf, x, y, a, color, hp):
        c, s = math.cos(a), math.sin(a)
        pts = [(x + px * c - py * s, y + px * s + py * c)
               for px, py in ((-16, -12), (16, -12), (16, 12), (-16, 12))]
        pygame.draw.polygon(surf, color, pts)
        pygame.draw.polygon(surf, (15, 15, 20), pts, 2)
        dark = tuple(int(v * 0.6) for v in color)
        pygame.draw.line(surf, dark, (x, y), (x + c * 27, y + s * 27), 6)
        pygame.draw.circle(surf, dark, (int(x), int(y)), 9)
        for i in range(START_HP):
            col = (235, 235, 240) if i < hp else (70, 72, 82)
            pygame.draw.rect(surf, col, (x - 14 + i * 10, y - 30, 8, 5))

    def draw_game():
        screen.fill(BG)
        for r in WALLS:
            pygame.draw.rect(screen, WALL_C, r)
        now = time.monotonic()
        for bx, by, vx, vy, owner in view.bullets:
            e = min(now - view.recv, 0.1)
            pl = view.players.get(owner)
            col = COLORS[pl["slot"] % 4] if pl else (255, 255, 255)
            pygame.draw.circle(screen, col, (int(bx + vx * e), int(by + vy * e)), BULLET_R)
        for pid, p in view.players.items():
            d = view.disp.get(pid)
            if not p["alive"] or d is None:
                continue
            draw_tank(screen, d[0], d[1], d[2], COLORS[p["slot"] % 4], p["hp"])
            text(screen, p["name"], f_small, (210, 212, 220), (d[0], d[1] + 30), center=True)
        for q in particles:
            pygame.draw.circle(screen, q[5], (int(q[0]), int(q[1])), 2)
        # scoreboard
        x = 24
        for pid in sorted(view.players):
            p = view.players[pid]
            lbl = "%s %d" % (p["name"], p["score"]) + ("  (you)" if pid == net.my_id else "")
            text(screen, lbl, f_small, COLORS[p["slot"] % 4], (x, 20))
            x += 190
        if view.phase != "lobby":
            text(screen, "Round %d - first to %d" % (view.round, WIN_SCORE), f_small,
                 (150, 154, 168), (W - 230, 20))

        def nm(pid):
            p = view.players.get(pid)
            return p["name"] if p else "?"

        if view.phase == "lobby":
            veil = pygame.Surface((W, H), pygame.SRCALPHA)
            veil.fill((10, 11, 15, 190))
            screen.blit(veil, (0, 0))
            text(screen, "TANK DUEL", f_big, (235, 235, 240), (W // 2, 120), center=True)
            y = 200
            for pid in sorted(view.players):
                p = view.players[pid]
                pygame.draw.rect(screen, COLORS[p["slot"] % 4], (W // 2 - 140, y + 4, 22, 22))
                text(screen, p["name"] + ("  (host)" if pid == 0 else ""), f_mid, (235, 235, 240), (W // 2 - 105, y))
                y += 46
            if net.is_host:
                text(screen, "Others join with IP:  %s" % host_ip, f_mid, (150, 200, 255), (W // 2, 440), center=True)
                if len(view.players) >= 2:
                    text(screen, "Press ENTER to start", f_mid, (120, 230, 140), (W // 2, 490), center=True)
                else:
                    text(screen, "Waiting for another player...", f_mid, (170, 172, 184), (W // 2, 490), center=True)
            else:
                text(screen, "Waiting for the host to start...", f_mid, (170, 172, 184), (W // 2, 470), center=True)
            if message:
                text(screen, message, f_small, (255, 120, 120), (W // 2, 540), center=True)
        elif view.phase == "roundover":
            s = "%s wins the round!" % nm(view.winner) if view.winner is not None else "Draw!"
            text(screen, s, f_mid, (255, 255, 255), (W // 2, H // 2), center=True)
        elif view.phase == "matchover":
            text(screen, "%s wins the match!" % nm(view.champion), f_big, (255, 220, 100), (W // 2, H // 2), center=True)

    def draw_menu():
        screen.fill(BG)
        text(screen, "TANK DUEL", f_big, (235, 235, 240), (W // 2, 110), center=True)
        cur = "_" if int(time.monotonic() * 2) % 2 == 0 else " "

        def field(label, value, y, active):
            text(screen, label, f_small, (150, 154, 168), (W // 2, y - 20), center=True)
            box = pygame.Rect(W // 2 - 170, y, 340, 46)
            pygame.draw.rect(screen, (40, 43, 54), box, border_radius=6)
            pygame.draw.rect(screen, (140, 190, 255) if active else (80, 86, 104), box, 2, border_radius=6)
            text(screen, value + (cur if active else ""), f_mid, (235, 235, 240), (box.x + 14, box.y + 8))

        field("Your name", name, 200, focus == "name")
        text(screen, "[F1]  Host a game", f_mid, (120, 230, 140), (W // 2, 285), center=True)
        field("or type the host's IP and press ENTER to join", ip_text, 345, focus == "ip")
        text(screen, "TAB switches field", f_small, (110, 114, 128), (W - 120, H - 24), center=True)
        found = list(scan.results) if scan else []
        if found:
            text(screen, "Found on your network (ENTER with an empty IP box joins the first):", f_small, (150, 200, 255), (W // 2, 425), center=True)
            for i, h in enumerate(found[:4]):
                text(screen, "%s  -  %s  (%d/%d)" % (h["name"], h["ip"], h["players"], h["max_players"]),
                     f_small, (210, 212, 220), (W // 2, 455 + i * 28), center=True)
        else:
            lbl = "Searching the LAN...   (F5 to search again)" if (scan and not scan.done) else \
                  "No games found. Type the host's IP.   (F5 to search again)"
            text(screen, lbl, f_small, (130, 134, 148), (W // 2, 435), center=True)
        if message:
            text(screen, message, f_small, (255, 120, 120), (W // 2, 560), center=True)

    if args.host:
        start_host()
    elif args.join:
        start_join(args.join)

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        now = time.monotonic()

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if state == "menu":
                    if e.key == pygame.K_ESCAPE:
                        running = False
                    elif e.key == pygame.K_F1:
                        start_host()
                    elif e.key == pygame.K_F5:
                        scan = discover_async(GAME_ID)
                    elif e.key == pygame.K_TAB:
                        focus = "ip" if focus == "name" else "name"
                    elif e.key == pygame.K_BACKSPACE:
                        if focus == "name":
                            name = name[:-1]
                        else:
                            ip_text = ip_text[:-1]
                    elif e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        if focus == "name":
                            focus = "ip"                 # done typing the name
                        elif ip_text.strip():
                            start_join(ip_text.strip())
                        elif scan and scan.results:
                            h = scan.results[0]
                            start_join(h["ip"], h.get("port"))
                        else:
                            message = "Type the host's IP address first (or F1 to host)."
                    elif focus == "name":
                        if e.unicode and e.unicode.isprintable() and len(name) < NAME_MAX:
                            name += e.unicode
                    elif e.unicode and e.unicode in "0123456789." and len(ip_text) < 15:
                        ip_text += e.unicode
                elif e.key == pygame.K_ESCAPE:
                    leave()
                elif state == "game" and e.key in (pygame.K_RETURN, pygame.K_KP_ENTER) \
                        and net is not None and net.is_host and world.phase == "lobby":
                    message = "" if world.start_match() else "Need at least 2 players."

        if state in ("connecting", "game") and net is not None:
            if net.is_host:
                host_process(world, net.poll())
            else:
                fx, statuses = client_process(view, net.poll(), now)
                for x, y, kind in fx:
                    burst(x, y, kind)
                for kind, data in statuses:
                    if kind == "connected":
                        state = "game"
                    elif kind == "failed":
                        leave("Connection failed: %s" % (data,))
                        break
                    elif kind == "disconnected":
                        leave("Disconnected from the host.")
                        break
                if state == "connecting" and now - connect_started > 12:
                    leave("Connection timed out.")

        if state == "game" and net is not None:
            keys = pygame.key.get_pressed()
            inp = {"turn": int(keys[pygame.K_RIGHT] or keys[pygame.K_d]) - int(keys[pygame.K_LEFT] or keys[pygame.K_a]),
                   "move": int(keys[pygame.K_UP] or keys[pygame.K_w]) - int(keys[pygame.K_DOWN] or keys[pygame.K_s]),
                   "fire": bool(keys[pygame.K_SPACE])}
            if net.is_host:
                world.set_input(0, inp)
                world.step(dt)
                view.mirror(world)
                for x, y, kind in world.fx_local:
                    burst(x, y, kind)
                world.fx_local = []
                if send_tick.ready():
                    net.send(world.snapshot())
            else:
                view.update(dt)
                if send_tick.ready():
                    net.send(dict(inp, t="input"))

        for q in particles:
            q[0] += q[2] * dt; q[1] += q[3] * dt; q[4] -= dt
        particles[:] = [q for q in particles if q[4] > 0]

        if state == "menu":
            draw_menu()
        elif state == "connecting" or not view.have_state:
            screen.fill(BG)
            text(screen, "Connecting...   (ESC to cancel)", f_mid, (200, 202, 212), (W // 2, H // 2), center=True)
        else:
            draw_game()
        pygame.display.flip()

    close_net()
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tank Duel - LAN tank arena")
    ap.add_argument("--name", help="your display name")
    ap.add_argument("--host", action="store_true", help="start hosting immediately")
    ap.add_argument("--join", metavar="IP", help="join this host immediately")
    run(ap.parse_args())
