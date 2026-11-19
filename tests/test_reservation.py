"""Run:  python3 -m unittest -v      (from the project folder; no Mininet needed)"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import server                          # noqa: E402
from app.client import ReservationClient        # noqa: E402
from app.controller_client import HttpController, NullController    # noqa: E402
from app.manager import ReservationManager      # noqa: E402
from config import settings as common           # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controller"))
from capacity import CapacityView               # noqa: E402

TRUNK = "s1->s2"


def req(src="h1", dst="h5", bw=4, dur=30, **kw):
    return {"src": src, "dst": dst, "bw_mbps": bw, "duration_s": dur, **kw}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = NullController()
        self.m = ReservationManager(self.ctrl)
        self.m.start()

    def tearDown(self):
        self.m.shutdown()

    def test_accept_installs_policy(self):
        ok, info = self.m.reserve(req())
        self.assertTrue(ok)
        self.assertEqual(self.ctrl.count("install"), 1)
        self.assertAlmostEqual(self.m.reserved[TRUNK], 4)

    def test_oversized_rejected_nothing_installed(self):
        ok, info = self.m.reserve(req(bw=20))
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "INSUFFICIENT_BANDWIDTH")
        self.assertEqual(info["link"], TRUNK)
        self.assertEqual(self.ctrl.count("install"), 0)
        self.assertEqual(self.m.reserved[TRUNK], 0)

    def test_oversubscription_across_requests(self):
        self.assertTrue(self.m.reserve(req("h1", "h5", 6))[0])
        ok, info = self.m.reserve(req("h2", "h6", 5))
        self.assertFalse(ok)
        self.assertEqual(info["available_mbps"], 4.0)
        self.assertTrue(self.m.reserve(req("h2", "h6", 4))[0])     # exactly fills the link
        self.assertFalse(self.m.reserve(req("h3", "h7", 0.5))[0])

    def test_conflict_same_flow(self):
        self.assertTrue(self.m.reserve(req())[0])
        ok, info = self.m.reserve(req(bw=1))
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "CONFLICT")

    def test_opposite_directions_independent(self):
        self.assertTrue(self.m.reserve(req("h1", "h5", 10))[0])
        self.assertTrue(self.m.reserve(req("h5", "h1", 10))[0])    # s2->s1 is a separate link

    def test_bad_requests(self):
        bad = [None, [], {}, req(src="hx"), req(dst="h1"), req(bw=-1), req(bw=0), req(bw="4"),
               req(bw=float("nan")), req(bw=float("inf")), req(dur=0), req(bw=True),
               {"src": "h1", "dst": "h5"}]
        for b in bad:
            ok, info = self.m.reserve(b)
            self.assertFalse(ok, b)
            self.assertIn(info["reason"], ("BAD_REQUEST", "UNKNOWN_HOST"), b)
        self.assertEqual(self.ctrl.count("install"), 0)

    def test_release_frees_capacity_and_removes_policy(self):
        ok, info = self.m.reserve(req(bw=10))
        self.assertFalse(self.m.reserve(req("h2", "h6", 1))[0])
        self.assertTrue(self.m.release(info["id"])[0])
        self.assertEqual(self.ctrl.count("remove"), 1)
        self.assertEqual(self.m.reserved[TRUNK], 0)
        self.assertTrue(self.m.reserve(req("h2", "h6", 10))[0])
        self.assertEqual(self.m.release(999)[1]["reason"], "UNKNOWN_ID")
        self.assertEqual(self.m.release(info["id"])[1]["reason"], "UNKNOWN_ID")   # double release
        self.assertEqual(self.m.release("abc")[1]["reason"], "BAD_REQUEST")

    def test_expiry_cleans_up(self):
        ok, info = self.m.reserve(req(bw=10, dur=0.4))
        time.sleep(1.0)
        self.assertEqual(self.m.reserved[TRUNK], 0)
        self.assertEqual(self.ctrl.count("remove"), 1)
        self.assertEqual(self.m.status(info["id"])[1]["state"], "EXPIRED")
        self.assertTrue(self.m.reserve(req("h2", "h6", 10))[0])

    def test_modify(self):
        _, info = self.m.reserve(req(bw=4))
        rid = info["id"]
        self.assertTrue(self.m.modify({"id": rid, "bw_mbps": 8})[0])
        self.assertAlmostEqual(self.m.reserved[TRUNK], 8)
        self.assertEqual(self.ctrl.count("modify"), 1)
        ok, info = self.m.modify({"id": rid, "bw_mbps": 11})
        self.assertFalse(ok)
        self.assertEqual(self.m.reserved[TRUNK], 8)
        self.assertTrue(self.m.modify({"id": rid, "bw_mbps": 2})[0])
        self.assertAlmostEqual(self.m.reserved[TRUNK], 2)
        self.assertFalse(self.m.modify({"id": rid})[0])
        self.assertEqual(self.m.modify({"id": 77, "bw_mbps": 1})[1]["reason"], "UNKNOWN_ID")

    def test_controller_failure_rolls_back(self):
        self.ctrl.fail = True
        ok, info = self.m.reserve(req())
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "CONTROLLER_ERROR")
        self.assertEqual(self.m.reserved[TRUNK], 0)
        self.assertEqual(len(self.m.res), 0)
        self.ctrl.fail = False
        self.assertTrue(self.m.reserve(req())[0])

    def test_failed_removal_keeps_capacity_then_retries(self):
        from app import manager
        _, info = self.m.reserve(req(bw=10))
        self.ctrl.fail = True
        self.assertEqual(self.m.release(info["id"])[1]["reason"], "CONTROLLER_ERROR")
        self.assertAlmostEqual(self.m.reserved[TRUNK], 10)         # rule may still exist
        self.ctrl.fail = False
        time.sleep(manager.RETRY_DELAY_S + 0.6)                    # reaper retries the removal
        self.assertEqual(self.m.reserved[TRUNK], 0)

    def test_policy_gets_the_path_links(self):
        self.m.reserve(req("h1", "h5", 2))
        self.assertEqual(self.ctrl.calls[0][3], ("h1->s1", "s1->s2", "s2->h5"))

    def test_concurrent_requests_never_oversubscribe(self):
        pairs = [(a, b) for a in ("h1", "h2", "h3", "h4") for b in ("h5", "h6", "h7", "h8")]
        for _ in range(5):
            barrier = threading.Barrier(len(pairs))
            results = []

            def worker(a, b):
                barrier.wait()
                results.append(self.m.reserve(req(a, b, 1.0)))

            threads = [threading.Thread(target=worker, args=p) for p in pairs]
            [t.start() for t in threads]
            [t.join() for t in threads]
            accepted = [info["id"] for ok, info in results if ok]
            self.assertEqual(len(accepted), 10)                    # exactly capacity/1.0
            self.assertLessEqual(self.m.reserved[TRUNK], 10 + 1e-9)
            for rid in accepted:
                self.m.release(rid)
            self.assertEqual(self.m.reserved[TRUNK], 0)

    def test_utilisation_view_text(self):
        _, info = self.m.reserve(req(bw=5))
        text = self.m.format_view()
        self.assertIn("s1->s2 5.0/10.0 Mbps (50%)", text)
        self.assertIn("#%d h1->h5" % info["id"], text)

    def test_utilisation_sampled(self):
        self.m.reserve(req(bw=5))
        time.sleep(1.3)
        self.assertGreater(self.m.avg_utilisation(TRUNK), 40)


class TcpTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = NullController()
        self.m = ReservationManager(self.ctrl)
        self.m.start()
        self.srv = server.make_server(self.m, "127.0.0.1", 0, idle_timeout=5)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.m.shutdown()

    def test_full_flow(self):
        with ReservationClient(port=self.port) as c:
            st, info = c.reserve("h1", "h5", 4, 30)
            self.assertEqual(st, "ACCEPT")
            rid = info["id"]
            st, info = c.reserve("h2", "h6", 20, 30)
            self.assertEqual((st, info["reason"]), ("REJECT", "INSUFFICIENT_BANDWIDTH"))
            st, info = c.status()
            self.assertEqual(st, "OK")
            self.assertEqual(info["links"][TRUNK]["reserved_mbps"], 4.0)
            self.assertEqual(c.modify(rid, bw_mbps=6)[0], "OK")
            self.assertEqual(c.release(rid)[0], "OK")
            self.assertEqual(c.status(rid)[1]["state"], "RELEASED")

    def test_garbage_does_not_kill_connection(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        f = s.makefile("rw", newline="\n")
        for line in ("HELLO", "RESERVE {not json", "RESERVE [1,2]", "RESERVE {}", "STATUS"):
            f.write(line + "\n"); f.flush()
            reply = f.readline()
            self.assertTrue(reply, line)
        self.assertTrue(reply.startswith("OK"))                    # STATUS still works at the end
        s.close()

    def test_line_longer_than_limit_is_rejected(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.sendall(b"X" * 5000 + b"\n")
        reply = s.recv(300).decode()
        s.close()
        self.assertIn("BAD_REQUEST", reply)
        self.assertIn("line too long", reply)

    def test_request_split_across_packets_is_reassembled(self):
        """TCP is a byte stream: half a request must wait for the rest."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.sendall(b'RESERVE {"src":"h1","dst"')
        time.sleep(0.3)
        s.sendall(b':"h5","bw_mbps":4,"duration_s":30}\n')
        reply = s.recv(1000).decode()
        s.close()
        self.assertTrue(reply.startswith("ACCEPT"), reply)

    def test_two_requests_in_one_packet_get_two_replies(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        f = s.makefile("rw", newline="\n")
        s.sendall(b"STATUS {}\nSTATUS {}\n")
        self.assertTrue(f.readline().startswith("OK"))
        self.assertTrue(f.readline().startswith("OK"))
        s.close()

    def test_dropped_client_reservation_expires_by_itself(self):
        c = ReservationClient(port=self.port)
        self.assertEqual(c.reserve("h1", "h5", 5, 0.5)[0], "ACCEPT")
        c.sock.shutdown(socket.SHUT_RDWR)
        c.sock.close()
        time.sleep(0.4)
        self.assertAlmostEqual(self.m.reserved[TRUNK], 5)          # still held until lease ends
        time.sleep(1.0)
        self.assertEqual(self.m.reserved[TRUNK], 0)

    def test_many_clients_compete(self):
        pairs = [(a, b) for a in ("h1", "h2", "h3", "h4") for b in ("h5", "h6", "h7", "h8")]
        out, lock = [], threading.Lock()

        def go(a, b):
            with ReservationClient(port=self.port) as c:
                st, _ = c.reserve(a, b, 1.0, 30)
            with lock:
                out.append(st)

        ts = [threading.Thread(target=go, args=p) for p in pairs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out.count("ACCEPT"), 10)
        self.assertEqual(out.count("REJECT"), 6)


class CapacityViewTests(unittest.TestCase):
    """The controller's own capacity view (capacity.py, used by ryu_app.py)."""

    def setUp(self):
        self.v = CapacityView({"a": 10000, "b": 100000})

    def test_add_and_remove(self):
        self.v.add(1, ["a", "b"], 4000)
        self.assertEqual(self.v.reserved, {"a": 4000, "b": 4000})
        self.v.remove(1)
        self.assertEqual(self.v.reserved, {"a": 0, "b": 0})
        self.v.remove(1)                                           # idempotent

    def test_refuses_oversubscription(self):
        self.v.add(1, ["a"], 6000)
        with self.assertRaises(ValueError):
            self.v.add(2, ["a"], 5000)
        self.assertEqual(self.v.reserved["a"], 6000)               # unchanged by the refusal
        self.v.add(2, ["a"], 4000)                                 # exactly full is fine

    def test_duplicate_and_unknown(self):
        self.v.add(1, ["a"], 1000)
        with self.assertRaises(ValueError):
            self.v.add(1, ["a"], 1000)
        with self.assertRaises(ValueError):
            self.v.add(2, ["zzz"], 1000)
        with self.assertRaises(KeyError):
            self.v.change(99, 1000)

    def test_change(self):
        self.v.add(1, ["a"], 4000)
        self.v.change(1, 9000)
        self.assertEqual(self.v.reserved["a"], 9000)
        with self.assertRaises(ValueError):
            self.v.change(1, 11000)
        self.assertEqual(self.v.reserved["a"], 9000)

    def test_summary(self):
        self.assertEqual(self.v.summary(), "no reservations")
        self.v.add(1, ["a"], 4000)
        self.assertIn("a 4.0/10.0 Mbps", self.v.summary())


class StartupAndShutdownTests(unittest.TestCase):
    def test_clear_leftovers_calls_controller(self):
        ctrl = NullController()
        server.clear_leftovers(ctrl)
        self.assertEqual(ctrl.count("clear"), 1)

    def test_clear_leftovers_survives_controller_failure(self):
        server.clear_leftovers(NullController(fail=True))          # must not raise

    def test_http_controller_clear_sends_delete(self):
        import http.server
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_DELETE(self):
                seen.append((self.command, self.path))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ok = HttpController("http://127.0.0.1:%d" % srv.server_address[1]).clear()
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertTrue(ok)
        self.assertEqual(seen, [("DELETE", "/policy")])

    def test_sigterm_shuts_down_cleanly(self):
        import signal
        import subprocess
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.Popen([sys.executable, "-m", "app.server", "--no-controller", "--port", "0"],
                             cwd=here, stderr=subprocess.PIPE, text=True)
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("policies removed", err)


if __name__ == "__main__":
    unittest.main()
