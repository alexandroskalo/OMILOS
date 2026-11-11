"""
LAN Pong - reference example for lanplay.

    pip install pygame
    python example_pong.py

Computer A: press H to host.     Computer B: press J, pick the host (F1..F4) or type its IP + Enter.
Host plays the left paddle, guest the right one.  W/S or Up/Down to move.  Esc = back to menu.

Architecture shown here (use the same pattern for your own games):
  * The HOST is authoritative: it simulates the ball and keeps the score.
  * The CLIENT only sends its own input/paddle position and renders what the host says.
  * State is sent at 30 Hz, not 60; the client extrapolates the ball in between.
"""
import random
import sys

import pygame

from lanplay import Host, Client, Ticker, discover_async, get_local_ip, DEFAULT_PORT

GAME_ID = "lan-pong-demo"      # hosts and clients must use the same id
W, H = 800, 500
PAD_W, PAD_H, PAD_SPEED = 12, 90, 380
BALL = 14
LEFT_X, RIGHT_X = 20, W - 20 - PAD_W


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class World:
    """All game state. Only the host calls step()."""

    def __init__(self):
        self.l = self.r = (H - PAD_H) / 2      # paddle tops
        self.score = [0, 0]
        self.serve(random.choice((-1, 1)))

    def serve(self, direction):
        self.bx, self.by = W / 2, H / 2
        self.vx, self.vy = 320 * direction, random.choice((-220, -130, 130, 220))

    def step(self, dt):
        self.bx += self.vx * dt
        self.by += self.vy * dt
        if self.by < 0:
            self.by, self.vy = 0, abs(self.vy)
        elif self.by > H - BALL:
            self.by, self.vy = H - BALL, -abs(self.vy)
        ball = pygame.Rect(int(self.bx), int(self.by), BALL, BALL)
        left = pygame.Rect(LEFT_X, int(self.l), PAD_W, PAD_H)
        right = pygame.Rect(RIGHT_X, int(self.r), PAD_W, PAD_H)
        if self.vx < 0 and ball.colliderect(left):
            self.vx = abs(self.vx) * 1.05
            self.vy += ((self.by + BALL / 2) - (self.l + PAD_H / 2)) * 3
        elif self.vx > 0 and ball.colliderect(right):
            self.vx = -abs(self.vx) * 1.05
            self.vy += ((self.by + BALL / 2) - (self.r + PAD_H / 2)) * 3
        if self.bx < -BALL:
            self.score[1] += 1
            self.serve(-1)
        elif self.bx > W:
            self.score[0] += 1
            self.serve(1)


def draw(screen, world, font):
    screen.fill((12, 12, 18))
    for y in range(0, H, 30):
        pygame.draw.rect(screen, (60, 60, 70), (W // 2 - 2, y, 4, 16))
    pygame.draw.rect(screen, (230, 230, 240), (LEFT_X, int(world.l), PAD_W, PAD_H))
    pygame.draw.rect(screen, (230, 230, 240), (RIGHT_X, int(world.r), PAD_W, PAD_H))
    pygame.draw.rect(screen, (255, 210, 80), (int(world.bx), int(world.by), BALL, BALL))
    txt = font.render(f"{world.score[0]}   {world.score[1]}", True, (200, 200, 210))
    screen.blit(txt, (W // 2 - txt.get_width() // 2, 15))


def main():
    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("LAN Pong")
    clock = pygame.time.Clock()
    font = pygame.font.Font(None, 34)
    big = pygame.font.Font(None, 64)

    state = "menu"            # menu | waiting | join | connecting | playing
    net = None
    scan = None
    ip_text = ""
    note = ""
    world = World()
    sender = Ticker(30)       # network send rate

    def lines(items, y=140, f=font):
        for s in items:
            surf = f.render(s, True, (220, 220, 230))
            screen.blit(surf, (W // 2 - surf.get_width() // 2, y))
            y += 40

    def stop_net():
        nonlocal net
        if net:
            net.close()
        net = None

    def join(addr):
        nonlocal net, state, note
        net = Client(GAME_ID, "Guest")
        net.connect_async(addr, DEFAULT_PORT)     # never blocks the game loop
        state, note = "connecting", ""

    while True:
        dt = min(clock.tick(60) / 1000, 0.05)

        # ---- input -------------------------------------------------------
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                stop_net()
                pygame.quit()
                sys.exit()
            if e.type != pygame.KEYDOWN:
                continue
            if e.key == pygame.K_ESCAPE:
                stop_net()
                scan, state, note = None, "menu", ""
            elif state == "menu":
                if e.key == pygame.K_h:
                    net = Host(GAME_ID, "Pong host", max_players=2)
                    try:
                        net.start()
                        state, note = "waiting", ""
                    except OSError as ex:
                        net, note = None, f"Can't host: {ex}"
                elif e.key == pygame.K_j:
                    scan, ip_text, state, note = discover_async(GAME_ID), "", "join", ""
            elif state == "join":
                if e.key == pygame.K_F5:
                    scan = discover_async(GAME_ID)
                elif pygame.K_F1 <= e.key <= pygame.K_F4:
                    i = e.key - pygame.K_F1
                    if scan and i < len(scan.results):
                        join(scan.results[i]["ip"])
                elif e.key == pygame.K_RETURN and ip_text:
                    join(ip_text)
                elif e.key == pygame.K_BACKSPACE:
                    ip_text = ip_text[:-1]
                elif e.unicode and e.unicode in "0123456789.":
                    ip_text += e.unicode

        # ---- network events ---------------------------------------------
        if net:
            for ev in net.poll():
                if ev.type == "connected":
                    world, state = World(), "playing"
                elif ev.type == "failed":
                    stop_net()
                    state, note = "join", ev.data
                elif ev.type == "disconnected":
                    if net.is_host:                        # keep hosting, wait for a new guest
                        state, note = "waiting", "Guest left"
                    else:
                        stop_net()
                        state, note = "menu", "Host left the game"
                elif ev.type == "message" and state == "playing":
                    m = ev.data
                    if net.is_host and m.get("t") == "pad":
                        world.r = clamp(float(m["y"]), 0, H - PAD_H)
                    elif not net.is_host and m.get("t") == "state":
                        world.l, world.bx, world.by = m["l"], m["bx"], m["by"]
                        world.vx, world.vy, world.score = m["vx"], m["vy"], m["s"]

        # ---- simulation --------------------------------------------------
        if state == "playing" and net:
            keys = pygame.key.get_pressed()
            move = int(keys[pygame.K_DOWN] or keys[pygame.K_s]) - \
                int(keys[pygame.K_UP] or keys[pygame.K_w])
            if net.is_host:
                world.l = clamp(world.l + move * PAD_SPEED * dt, 0, H - PAD_H)
                world.step(dt)
                if sender.ready():
                    net.send({"t": "state", "l": world.l, "bx": world.bx, "by": world.by,
                              "vx": world.vx, "vy": world.vy, "s": world.score})
            else:
                world.r = clamp(world.r + move * PAD_SPEED * dt, 0, H - PAD_H)
                world.bx += world.vx * dt            # extrapolate between host updates
                world.by += world.vy * dt
                if sender.ready():
                    net.send({"t": "pad", "y": world.r})

        # ---- drawing -----------------------------------------------------
        if state == "playing":
            draw(screen, world, big)
            who = "host (left)" if net.is_host else "guest (right)"
            lat = net.latency(1 if net.is_host else None)
            ping = f"  ping {lat * 1000:.0f} ms" if lat is not None else ""
            screen.blit(font.render(f"you: {who}{ping}", True, (110, 110, 125)), (10, H - 28))
        else:
            screen.fill((12, 12, 18))
            if state == "menu":
                lines(["LAN PONG", "", "H  -  host a game", "J  -  join a game"], 120)
            elif state == "waiting":
                lines(["Waiting for a player...", f"Tell them to join {get_local_ip()}",
                       "(or press J on the other computer and pick this host)", "", "Esc - cancel"])
            elif state == "join":
                found = scan.results if scan else []
                rows = [f"F{i + 1}  {r['name']}  ({r['ip']})  {r['players']}/{r['max_players']}"
                        for i, r in enumerate(found[:4])]
                status = "Scanning..." if scan and not scan.done else \
                    ("" if rows else "No hosts found (F5 = rescan)")
                lines(["Join a game", status] + rows + ["", f"or type IP:  {ip_text}_", "Enter - connect   Esc - back"], 80)
            elif state == "connecting":
                lines(["Connecting..."], 200)
            if note:
                n = font.render(note, True, (255, 120, 120))
                screen.blit(n, (W // 2 - n.get_width() // 2, H - 60))
        pygame.display.flip()


if __name__ == "__main__":
    main()
