"""
lanplay - a tiny LAN multiplayer backend for pygame (or any Python game).

One computer HOSTS, the others JOIN. Pure standard library, no dependencies.

    Host side                         Client side
    ---------                         -----------
    net = Host("mygame", "Alice")     found = discover("mygame")
    net.start()                       net = Client("mygame", "Bob")
                                      net.connect(found[0]["ip"], found[0]["port"])

    # then, identically on both sides, once per frame:
    for ev in net.poll():
        if ev.type == "connected":     ...   # ev.peer = id of the player who joined
        elif ev.type == "message":     ...   # ev.peer = sender id, ev.data = your dict
        elif ev.type == "disconnected": ...
        elif ev.type == "failed":      ...   # Client.connect_async() only, ev.data = reason
    net.send({"anything": "json-able"})

Player ids: the host is always 0 (HOST_ID). Clients get 1, 2, 3 ...

Everything network-related runs in background threads; poll() never blocks, so it
is safe to call from the pygame main loop.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import struct
import threading
import time
from collections import namedtuple

__all__ = [
    "Host", "Client", "Event", "ConnectError", "Ticker",
    "discover", "discover_async", "get_local_ip",
    "DEFAULT_PORT", "DISCOVERY_PORT", "HOST_ID",
]

DEFAULT_PORT = 50007        # TCP: game traffic
DISCOVERY_PORT = 50008      # UDP: "who is hosting?" broadcasts
PROTOCOL = 1                # bump if the wire format changes
HOST_ID = 0
MAX_MESSAGE = 1 << 20       # 1 MiB per message
PING_INTERVAL = 1.0         # seconds between keep-alive pings
TIMEOUT = 6.0               # no data for this long -> connection is dead
HANDSHAKE_TIMEOUT = 5.0

# type is one of: "connected", "disconnected", "message", "failed"
Event = namedtuple("Event", ["type", "peer", "data"])


class ConnectError(Exception):
    """Raised by Client.connect() when joining fails."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def get_local_ip() -> str:
    """Best guess at this machine's LAN IP (what the other player should type)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Ticker:
    """Rate limiter so you can send at e.g. 30 Hz while rendering at 60 FPS.

        sender = Ticker(30)
        ...
        if sender.ready():
            net.send(state)
    """

    def __init__(self, hz: float):
        self.interval = 1.0 / hz
        self._next = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.interval
            return True
        return False


# --------------------------------------------------------------------------- #
# one TCP connection, length-prefixed JSON frames
# --------------------------------------------------------------------------- #
class _Connection:
    def __init__(self, sock, peer_id, on_message, on_close):
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = sock
        self.peer_id = peer_id
        self.name = ""
        self.latency = None
        self.created = time.time()
        self.last_recv = time.time()
        self.closed = threading.Event()
        self._send_lock = threading.Lock()
        self._on_message = on_message
        self._on_close = on_close

    def start(self):
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def send_raw(self, obj) -> bool:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_MESSAGE:
            raise ValueError("message too large (limit is 1 MiB)")
        if self.closed.is_set():
            return False
        with self._send_lock:
            try:
                self.sock.sendall(struct.pack("!I", len(payload)) + payload)
                return True
            except OSError:
                pass
        self.close()
        return False

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return bytes(buf)

    def _recv_loop(self):
        try:
            while not self.closed.is_set():
                (n,) = struct.unpack("!I", self._recv_exact(4))
                if n > MAX_MESSAGE:
                    break
                obj = json.loads(self._recv_exact(n))
                self.last_recv = time.time()
                if isinstance(obj, dict):
                    self._on_message(self, obj)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            self.close()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass
        self._on_close(self)


# --------------------------------------------------------------------------- #
# shared base
# --------------------------------------------------------------------------- #
class Session:
    is_host = False

    def __init__(self, game: str, name: str):
        self.game = game
        self.name = name
        self.my_id = None
        self._events: "queue.Queue[Event]" = queue.Queue()
        self._lock = threading.RLock()
        self._stop = threading.Event()

    def poll(self) -> list:
        """Return (and clear) all events since the last call. Never blocks."""
        out = []
        try:
            while True:
                out.append(self._events.get_nowait())
        except queue.Empty:
            pass
        return out

    def _emit(self, type_, peer, data=None):
        self._events.put(Event(type_, peer, data))


# --------------------------------------------------------------------------- #
# HOST
# --------------------------------------------------------------------------- #
class Host(Session):
    """Listens for players. You are player 0.

    game         identifier string; only clients using the same string can join
    name         shown in the lobby/discovery list
    port         TCP port (0 = pick any free port; read it back from .port)
    max_players  total players including the host
    advertise    answer LAN discovery broadcasts so clients can find you
    """

    is_host = True

    def __init__(self, game="game", name="Host", port=DEFAULT_PORT,
                 max_players=2, advertise=True):
        super().__init__(game, name)
        self.port = port
        self.max_players = max(2, int(max_players))
        self.advertise = advertise
        self.my_id = HOST_ID
        self.discoverable = False
        self._peers = {}          # peer_id -> _Connection
        self._pending = set()     # connected but no hello yet
        self._server = None
        self._udp = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self):
        """Open the port. Raises OSError if it is already in use."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", self.port))
        except OSError:
            srv.close()
            raise
        srv.listen(8)
        srv.settimeout(0.5)
        self._server = srv
        self.port = srv.getsockname()[1]
        threads = [self._accept_loop, self._keepalive_loop]
        if self.advertise:
            threads.append(self._discovery_loop)
        for fn in threads:
            threading.Thread(target=fn, daemon=True).start()
        return self

    def close(self):
        self._stop.set()
        with self._lock:
            conns = list(self._peers.values()) + list(self._pending)
        for c in conns:
            c.close()
        for s in (self._server, self._udp):
            if s:
                try:
                    s.close()
                except OSError:
                    pass

    # -- public API -------------------------------------------------------- #
    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._peers)

    @property
    def peers(self) -> list:
        """Ids of currently connected clients."""
        with self._lock:
            return sorted(self._peers)

    def player_name(self, peer_id):
        with self._lock:
            c = self._peers.get(peer_id)
            return c.name if c else None

    def latency(self, peer_id):
        """Round-trip time in seconds, or None if not measured yet."""
        with self._lock:
            c = self._peers.get(peer_id)
            return c.latency if c else None

    def send(self, data, to=None):
        """Send a JSON-serialisable object. to=None -> everyone; or a peer id /
        list of peer ids."""
        frame = {"_t": "msg", "d": data}
        with self._lock:
            if to is None:
                targets = list(self._peers.values())
            else:
                ids = [to] if isinstance(to, int) else list(to)
                targets = [self._peers[i] for i in ids if i in self._peers]
        for c in targets:
            c.send_raw(frame)

    def kick(self, peer_id):
        with self._lock:
            c = self._peers.get(peer_id)
        if c:
            c.close()

    # -- internals --------------------------------------------------------- #
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn = _Connection(sock, None, self._on_msg, self._on_close)
            with self._lock:
                self._pending.add(conn)
            conn.start()

    def _on_msg(self, conn, obj):
        t = obj.get("_t")
        if conn.peer_id is None:                      # not registered yet
            if t == "hello":
                self._handle_hello(conn, obj)
            else:
                conn.close()
            return
        if t == "msg":
            self._emit("message", conn.peer_id, obj.get("d"))
        elif t == "ping":
            conn.send_raw({"_t": "pong", "ts": obj.get("ts")})
        elif t == "pong":
            try:
                conn.latency = time.time() - float(obj.get("ts"))
            except (TypeError, ValueError):
                pass

    def _handle_hello(self, conn, obj):
        reason = None
        with self._lock:
            if obj.get("proto") != PROTOCOL:
                reason = "version mismatch"
            elif obj.get("game") != self.game:
                reason = "different game"
            elif len(self._peers) >= self.max_players - 1:
                reason = "game is full"
            else:
                pid = next(i for i in range(1, self.max_players + 1)
                           if i not in self._peers)
                conn.peer_id = pid
                conn.name = str(obj.get("name", "Player"))[:32]
                self._pending.discard(conn)
                self._peers[pid] = conn
        if reason:
            conn.send_raw({"_t": "reject", "reason": reason})
            conn.close()
            return
        conn.send_raw({"_t": "welcome", "id": pid, "host_name": self.name,
                       "max_players": self.max_players})
        self._emit("connected", pid, {"name": conn.name})

    def _on_close(self, conn):
        with self._lock:
            self._pending.discard(conn)
            registered = (conn.peer_id is not None
                          and self._peers.get(conn.peer_id) is conn)
            if registered:
                del self._peers[conn.peer_id]
        if registered and not self._stop.is_set():
            self._emit("disconnected", conn.peer_id, None)

    def _keepalive_loop(self):
        while not self._stop.wait(PING_INTERVAL):
            now = time.time()
            with self._lock:
                peers = list(self._peers.values())
                pending = list(self._pending)
            for c in pending:
                if now - c.created > HANDSHAKE_TIMEOUT:
                    c.close()
            for c in peers:
                if now - c.last_recv > TIMEOUT:
                    c.close()
                else:
                    c.send_raw({"_t": "ping", "ts": now})

    def _discovery_loop(self):
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            u.bind(("", DISCOVERY_PORT))
            u.settimeout(0.5)
        except OSError:
            return                       # discovery is optional; typing the IP still works
        self._udp = u
        self.discoverable = True
        while not self._stop.is_set():
            try:
                data, addr = u.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                q = json.loads(data)
            except ValueError:
                continue
            if not isinstance(q, dict) or q.get("q") != "LANPLAY":
                continue
            if q.get("game") not in (self.game, "*"):
                continue
            with self._lock:
                players = 1 + len(self._peers)
            reply = {"magic": "LANPLAY", "game": self.game, "name": self.name,
                     "port": self.port, "players": players,
                     "max_players": self.max_players}
            try:
                u.sendto(json.dumps(reply).encode(), addr)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# CLIENT
# --------------------------------------------------------------------------- #
class Client(Session):
    """Joins a Host. One Client object = one connection attempt; make a new
    Client to retry or to join somewhere else."""

    def __init__(self, game="game", name="Player"):
        super().__init__(game, name)
        self.connected = False
        self.host_name = None
        self.max_players = None
        self.host_latency = None
        self._conn = None
        self._user_closed = False
        self._hs = threading.Event()
        self._hs_error = None

    def connect(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0):
        """Blocking connect + handshake. Raises ConnectError on failure.
        In a pygame loop prefer connect_async()."""
        if self._conn is not None:
            raise RuntimeError("Client is single-use: create a new Client to reconnect")
        try:
            sock = socket.create_connection((host, int(port)), timeout=timeout)
        except (OSError, ValueError) as e:
            self._conn = False
            raise ConnectError(f"Could not reach {host}:{port} ({e})") from None
        conn = _Connection(sock, HOST_ID, self._on_msg, self._on_close)
        self._conn = conn
        conn.start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        conn.send_raw({"_t": "hello", "proto": PROTOCOL,
                       "game": self.game, "name": self.name})
        if not self._hs.wait(timeout):
            self.close()
            raise ConnectError("Host did not answer the handshake")
        if self._hs_error:
            err = self._hs_error
            self.close()
            raise ConnectError(err)

    def connect_async(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0):
        """Non-blocking connect. Result arrives via poll() as a "connected" or
        "failed" event (ev.data = reason)."""
        def run():
            try:
                self.connect(host, port, timeout)
            except ConnectError as e:
                self._emit("failed", HOST_ID, str(e))
        threading.Thread(target=run, daemon=True).start()

    def send(self, data) -> bool:
        """Send a JSON-serialisable object to the host."""
        if not self.connected or not self._conn:
            return False
        return self._conn.send_raw({"_t": "msg", "d": data})

    def latency(self, peer_id=None):
        return self.host_latency

    def close(self):
        self._user_closed = True
        self._stop.set()
        if self._conn:
            self._conn.close()

    # -- internals --------------------------------------------------------- #
    def _on_msg(self, conn, obj):
        t = obj.get("_t")
        if t == "welcome":
            self.my_id = obj.get("id")
            self.host_name = obj.get("host_name")
            self.max_players = obj.get("max_players")
            self.connected = True
            self._emit("connected", HOST_ID,
                       {"host_name": self.host_name, "my_id": self.my_id})
            self._hs.set()
        elif t == "reject":
            self._hs_error = f"Host refused: {obj.get('reason', 'unknown reason')}"
            self._hs.set()
        elif t == "msg":
            self._emit("message", HOST_ID, obj.get("d"))
        elif t == "ping":
            conn.send_raw({"_t": "pong", "ts": obj.get("ts")})
        elif t == "pong":
            try:
                self.host_latency = time.time() - float(obj.get("ts"))
            except (TypeError, ValueError):
                pass

    def _on_close(self, conn):
        was_connected = self.connected
        self.connected = False
        self._stop.set()
        if not self._hs.is_set():
            self._hs_error = self._hs_error or "Connection closed by host"
            self._hs.set()
        if was_connected and not self._user_closed:
            self._emit("disconnected", HOST_ID, None)

    def _keepalive_loop(self):
        while not self._stop.wait(PING_INTERVAL):
            conn = self._conn
            if not conn or not self.connected:
                continue
            if time.time() - conn.last_recv > TIMEOUT:
                conn.close()
            else:
                conn.send_raw({"_t": "ping", "ts": time.time()})


# --------------------------------------------------------------------------- #
# DISCOVERY (client side)
# --------------------------------------------------------------------------- #
def discover(game: str = "game", timeout: float = 1.0, port: int = DISCOVERY_PORT) -> list:
    """Broadcast on the LAN and return hosts that answer, as a list of dicts:
    {"name", "ip", "port", "game", "players", "max_players"}.
    Blocks for `timeout` seconds - use discover_async() inside a game loop."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(0.15)
    query = json.dumps({"q": "LANPLAY", "game": game}).encode()
    local = get_local_ip()
    targets = [("255.255.255.255", port), ("127.0.0.1", port)]
    if local != "127.0.0.1":
        targets.append((local.rsplit(".", 1)[0] + ".255", port))   # subnet broadcast
    found = {}
    end = time.time() + timeout
    next_send = 0.0
    while time.time() < end:
        if time.time() >= next_send:
            for t in targets:
                try:
                    s.sendto(query, t)
                except OSError:
                    pass
            next_send = time.time() + 0.4
        try:
            data, addr = s.recvfrom(4096)
            info = json.loads(data)
        except socket.timeout:
            continue
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or info.get("magic") != "LANPLAY":
            continue
        key = (addr[0], info.get("port"))
        found[key] = {"name": info.get("name", "?"), "ip": addr[0],
                      "port": info.get("port", DEFAULT_PORT),
                      "game": info.get("game"), "players": info.get("players"),
                      "max_players": info.get("max_players")}
    s.close()
    return list(found.values())


class _Scan:
    def __init__(self):
        self.done = False
        self.results = []


def discover_async(game: str = "game", timeout: float = 1.5) -> _Scan:
    """Start a background scan. Check `.done` and read `.results` each frame."""
    scan = _Scan()

    def run():
        scan.results = discover(game, timeout)
        scan.done = True
    threading.Thread(target=run, daemon=True).start()
    return scan
