"""Headless tests (no pygame needed):  python test_lanplay.py"""
import time
import unittest

from lanplay import Host, Client, ConnectError, discover, HOST_ID


def wait_for(session, type_, timeout=3.0):
    """Poll until an event of `type_` shows up; return it (or None)."""
    end = time.time() + timeout
    while time.time() < end:
        for ev in session.poll():
            if ev.type == type_:
                return ev
        time.sleep(0.01)
    return None


class LanplayTests(unittest.TestCase):
    def setUp(self):
        self.host = Host("test", "TestHost", port=0, max_players=2).start()
        self.to_close = [self.host]

    def tearDown(self):
        for s in self.to_close:
            s.close()

    def join(self, game="test", name="Guest"):
        c = Client(game, name)
        self.to_close.append(c)
        c.connect("127.0.0.1", self.host.port)
        return c

    def test_handshake_and_ids(self):
        c = self.join()
        ev = wait_for(self.host, "connected")
        self.assertEqual(ev.peer, 1)
        self.assertEqual(ev.data["name"], "Guest")
        self.assertEqual(c.my_id, 1)
        self.assertEqual(c.host_name, "TestHost")
        self.assertEqual(self.host.peers, [1])

    def test_messages_both_ways(self):
        c = self.join()
        wait_for(self.host, "connected")
        c.send({"hello": [1, 2, 3]})
        ev = wait_for(self.host, "message")
        self.assertEqual((ev.peer, ev.data), (1, {"hello": [1, 2, 3]}))
        self.host.send({"pong": True})
        ev = wait_for(c, "message")
        self.assertEqual((ev.peer, ev.data), (HOST_ID, {"pong": True}))

    def test_many_messages_arrive_in_order(self):
        c = self.join()
        wait_for(self.host, "connected")
        for i in range(500):
            c.send({"i": i})
        got = []
        end = time.time() + 3
        while len(got) < 500 and time.time() < end:
            got += [e.data["i"] for e in self.host.poll() if e.type == "message"]
            time.sleep(0.01)
        self.assertEqual(got, list(range(500)))

    def test_wrong_game_rejected(self):
        with self.assertRaises(ConnectError):
            self.join(game="other")

    def test_full_rejected(self):
        self.join()
        with self.assertRaises(ConnectError):
            self.join(name="Third")

    def test_connection_refused(self):
        c = Client("test", "x")
        with self.assertRaises(ConnectError):
            c.connect("127.0.0.1", 1, timeout=1)   # nothing listens on port 1

    def test_client_leaves(self):
        c = self.join()
        wait_for(self.host, "connected")
        c.close()
        ev = wait_for(self.host, "disconnected")
        self.assertEqual(ev.peer, 1)
        self.assertEqual(self.host.peers, [])

    def test_host_leaves(self):
        c = self.join()
        wait_for(self.host, "connected")
        self.host.close()
        self.assertIsNotNone(wait_for(c, "disconnected"))
        self.assertFalse(c.connected)

    def test_slot_is_reused(self):
        c = self.join()
        wait_for(self.host, "connected")
        c.close()
        wait_for(self.host, "disconnected")
        c2 = self.join(name="Again")
        self.assertEqual(c2.my_id, 1)

    def test_async_connect(self):
        c = Client("test", "Async")
        self.to_close.append(c)
        c.connect_async("127.0.0.1", self.host.port)
        self.assertIsNotNone(wait_for(c, "connected"))
        bad = Client("nope", "Async")
        bad.connect_async("127.0.0.1", self.host.port)
        ev = wait_for(bad, "failed")
        self.assertIn("refused", ev.data)

    def test_discovery(self):
        if not self.host.discoverable:
            time.sleep(0.3)
        if not self.host.discoverable:
            self.skipTest("discovery port busy / blocked on this machine")
        found = discover("test", timeout=1.0)
        self.assertTrue(any(f["port"] == self.host.port and f["name"] == "TestHost"
                            for f in found), found)
        self.assertEqual(discover("some-other-game", timeout=0.6), [])

    def test_latency_measured(self):
        c = self.join()
        wait_for(self.host, "connected")
        time.sleep(1.5)
        self.assertIsNotNone(self.host.latency(1))
        self.assertIsNotNone(c.latency())


if __name__ == "__main__":
    unittest.main(verbosity=2)
