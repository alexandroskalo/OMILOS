# lanplay — LAN multiplayer for pygame

One computer hosts, others join over the local network. Pure Python standard
library (no pip install besides pygame itself). Drop `lanplay.py` next to your game.

```
lanplay/
  lanplay.py        the library
  example_pong.py   complete 2-player game using it
  test_lanplay.py   headless tests (python test_lanplay.py)
```

## 1. Mental model

* **Host** = player 0, listens on a TCP port, answers LAN "who's hosting?" broadcasts.
* **Client** = player 1, 2, … joins a host by IP (or finds it via `discover`).
* Both expose the same API: `net.poll()` gives you events, `net.send(data)` sends.
* All networking runs on background threads. `poll()` and `send()` never block
  your game loop. Events are only ever delivered when *you* call `poll()`, so
  all your game code stays single-threaded.
* A message is any JSON-serialisable thing (dict/list/str/number/bool/None).
  Tuples arrive as lists; dict keys arrive as strings.

## 2. Quick start

**Hosting**
```python
from lanplay import Host, get_local_ip

net = Host("mygame", "Alice's room", max_players=2)   # "mygame" = your game's id
net.start()                                           # raises OSError if port is taken
print("Tell your friend to join:", get_local_ip())
```

**Joining**
```python
from lanplay import Client, discover_async

scan = discover_async("mygame")        # background scan; check scan.done / scan.results
# scan.results -> [{"name","ip","port","players","max_players","game"}, ...]

net = Client("mygame", "Bob")
net.connect_async("192.168.1.20")      # or scan.results[0]["ip"]; result arrives as an event
```

**Every frame, on both sides**
```python
for ev in net.poll():
    if ev.type == "connected":      # host: a player joined (ev.peer = their id, ev.data["name"])
        ...                         # client: you're in (ev.data["my_id"], ev.data["host_name"])
    elif ev.type == "message":      # ev.peer = sender id, ev.data = what they sent
        ...
    elif ev.type == "disconnected": # ev.peer = who left
        ...
    elif ev.type == "failed":       # client only, from connect_async: ev.data = reason string
        ...
```

**Sending**
```python
net.send({"t": "input", "dx": 1})             # client -> host; host -> everyone
net.send({"t": "state", ...}, to=1)           # host only: to one peer (or a list of ids)
```

## 3. API reference

| Thing | What it does |
|---|---|
| `Host(game, name, port=50007, max_players=2, advertise=True)` | `max_players` counts the host. `port=0` picks a free port (read `.port`). |
| `Host.start()` / `.close()` | Open / shut down. Call `close()` on exit. |
| `Host.send(data, to=None)` | Broadcast, or to a peer id / list of ids. |
| `Host.peers`, `.player_name(id)`, `.latency(id)`, `.kick(id)` | Roster, names, round-trip time in seconds, remove a player. |
| `Client(game, name)` | **Single-use**: make a new `Client` for each attempt. |
| `Client.connect(ip, port)` | Blocking; raises `ConnectError`. Fine for console tools, not for a game loop. |
| `Client.connect_async(ip, port)` | Non-blocking; emits `"connected"` or `"failed"`. Use this in pygame. |
| `Client.send(data)` | Send to the host. Returns `False` if not connected. |
| `Client.connected`, `.my_id`, `.host_name`, `.latency()` | Status. |
| `net.poll()` | List of `Event(type, peer, data)` since last call. |
| `net.is_host`, `net.my_id` | Branch on role. Host is always id `0` (`HOST_ID`). |
| `discover(game, timeout)` / `discover_async(game)` | Find hosts on the LAN. |
| `get_local_ip()` | The LAN IP to show the other player. |
| `Ticker(hz)` | `if t.ready(): ...` to send at a fixed rate. |

Safety built in: the `game` id must match (different games can't join each other),
protocol version is checked, full rooms reject extra players, dead connections are
detected by keep-alive pings (≈6 s timeout), and a 1 MiB message cap stops garbage.

## 4. How to design a game on top of it

### Step 1 — Decide who owns the truth: **host-authoritative**
The host runs the real simulation. Clients send *inputs* (or their own position for
simple games) and draw whatever state the host sends back. This avoids "two
different versions of the game" bugs and makes cheating/desync impossible to
confuse with lag.

```
client:  read keys  -> net.send({"t":"input", ...})
host:    apply inputs from all players, step the world, net.send({"t":"state", ...})
client:  receive state -> store it -> draw it
```

The host is also a player: apply its own input locally (no network hop).

### Step 2 — Write the game around one `World` object
Keep all state in a plain object/dict that can be turned into JSON. Then
"networking" is just "copy the world from host to clients". `example_pong.py`
does exactly this.

### Step 3 — Give every message a type
Always send dicts with a `"t"` key and dispatch on it:

```python
if ev.type == "message":
    m = ev.data
    if m["t"] == "input":   ...
    elif m["t"] == "state": ...
    elif m["t"] == "chat":  ...
```
Unknown `t` → ignore it. That keeps old/new versions of your game from crashing each other.

### Step 4 — Pick send rates
* Render at 60 FPS, **send at 20–30 Hz** (`Ticker(30)`), not every frame.
* **Input events** (button presses, "player fired") → send immediately when they happen.
* **Continuous state** (positions) → send on a `Ticker`.
* **Big, rarely-changing data** (level layout, settings) → send once on `"connected"`,
  not every tick.

### Step 5 — Hide the latency
LAN latency is tiny (1–10 ms) but updates at 30 Hz still look steppy. Cheap fixes:
* **Extrapolate**: send velocity along with position; clients keep moving things
  between updates (Pong example does this for the ball).
* **Interpolate**: draw remote objects slightly behind (store the last 2 states,
  blend between them) for perfectly smooth motion.
* **Predict your own player**: move your own character immediately from local input;
  the host's state only *corrects* you.

### Step 6 — Handle the lifecycle
* **Lobby**: host calls `start()`, waits for `"connected"`; when the room is full
  (or the host presses "Start"), send `{"t":"start", "seed": 12345, ...}`.
* **Shared randomness**: send a seed in the `start` message so both sides can
  generate identical levels without sending the whole level.
* **Disconnects**: handle `"disconnected"` — pause, end the match, or let the host
  keep waiting (Pong shows both).
* **Quit cleanly**: call `net.close()` before `pygame.quit()`.

### Step 7 — More than 2 players
Set `Host(max_players=4)`. Clients only talk to the host, so to show player A's
actions to player B the host must forward them, e.g. broadcast one `"state"`
containing every player, keyed by id:

```python
net.send({"t": "state", "players": {"0": {"x": 10, "y": 5}, "1": {...}, "2": {...}}})
```
Remember JSON turns dict keys into strings. Use `ev.peer` (not a field inside the
message) to know who really sent something. Players joining mid-game get a
`"connected"` event: send them a full snapshot with `net.send(snapshot, to=ev.peer)`.

## 5. Minimal skeleton to copy

```python
import pygame
from lanplay import Host, Client, Ticker, discover

GAME_ID = "mygame"

def main(role, ip=None):
    pygame.init(); screen = pygame.display.set_mode((640, 360)); clock = pygame.time.Clock()
    if role == "host":
        net = Host(GAME_ID, "Room"); net.start()
    else:
        net = Client(GAME_ID, "Guest"); net.connect(ip)      # blocking is OK before the loop starts
    send_tick = Ticker(30)
    world = {"x": 100, "y": 100}                              # your game state

    running = True
    while running:
        dt = clock.tick(60) / 1000
        for e in pygame.event.get():
            if e.type == pygame.QUIT: running = False

        for ev in net.poll():                                 # 1. network in
            if ev.type == "message":
                m = ev.data
                if net.is_host and m["t"] == "input": ...     #    host: apply guest input
                if not net.is_host and m["t"] == "state": world = m["world"]
            elif ev.type == "disconnected": running = False

        keys = pygame.key.get_pressed()                       # 2. local input
        if net.is_host:
            ...                                               # 3. host simulates the world
            if send_tick.ready(): net.send({"t": "state", "world": world})   # 4. network out
        else:
            if send_tick.ready(): net.send({"t": "input", "keys": [keys[pygame.K_LEFT], keys[pygame.K_RIGHT]]})

        screen.fill((0, 0, 0))                                # 5. draw
        pygame.draw.circle(screen, (255, 255, 255), (int(world["x"]), int(world["y"])), 10)
        pygame.display.flip()
    net.close(); pygame.quit()
```

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Client can't connect | Allow Python through the **firewall** on the host machine (Windows prompts the first time — choose *Private networks*). Allow TCP 50007 and UDP 50008. |
| `discover` finds nothing | Some routers/Wi-Fi block broadcasts ("client isolation", guest networks). Type the host's IP instead — `get_local_ip()` shows it. Both machines must be on the same subnet. |
| `OSError: address already in use` | Another host is using the port. Use `Host(port=50010)` and `connect(ip, 50010)`. |
| "Host refused: different game" | The two sides use different `GAME_ID` strings. |
| `TypeError: Object of type ... is not JSON serializable` | You sent a pygame object (Vector2, Rect, Surface). Convert to lists/numbers first, e.g. `[v.x, v.y]`. |
| Game freezes while connecting | You used `connect()` inside the game loop. Use `connect_async()`. |
| Choppy remote movement | Add extrapolation/interpolation (§4, Step 5) or raise your send rate to 60 Hz. |
| Playing over the internet | Not supported out of the box: LAN only. Use a VPN (Tailscale/ZeroTier) and type the VPN IP, or port-forward TCP 50007. |

Note: the connection is not encrypted or authenticated. It is intended for friendly
LAN play; don't trust messages for anything security-sensitive.
