#!/usr/bin/env python3
"""Concurrent TCP reservation server, built directly on Python's socket module.

No server framework is used: this file creates the listening socket, accepts connections,
starts one thread per client and cuts the byte stream into lines itself.

Run from the project folder:   python3 -m app.server [--view]

Protocol: one request per line:   VERB {json}\n     reply: one line   STATUS {json}\n
    RESERVE {"src":"h1","dst":"h5","bw_mbps":4,"duration_s":30}   -> ACCEPT | REJECT
    RELEASE {"id":3}                                               -> OK | REJECT
    MODIFY  {"id":3,"bw_mbps":6}                                   -> OK | REJECT
    STATUS  {} or {"id":3}                                         -> OK | REJECT
See docs/PROTOCOL.md.
"""
import argparse
import json
import logging
import signal
import socket
import threading

from app.controller_client import HttpController, NullController
from app.manager import ReservationManager
from config import settings

RECV_SIZE = 4096
log = logging.getLogger("server")


class LineReader:
    """Cuts a TCP byte stream into lines. TCP has no message boundaries: one recv() can return
    half a request or several requests, so we keep a buffer and split on the newline ourselves."""

    def __init__(self, conn, max_line):
        self.conn = conn
        self.max_line = max_line
        self.buf = b""

    def readline(self):
        """One line including its newline; b'' when the client closed the connection;
        None when the line is longer than max_line bytes."""
        while True:
            nl = self.buf.find(b"\n")
            if nl != -1:
                if nl + 1 > self.max_line:
                    return None
                line, self.buf = self.buf[:nl + 1], self.buf[nl + 1:]
                return line
            if len(self.buf) >= self.max_line:
                return None                       # no newline yet and already too long
            data = self.conn.recv(RECV_SIZE)      # blocks; raises socket.timeout if the client is idle
            if not data:                          # client closed: hand back any unfinished last line
                line, self.buf = self.buf, b""
                return line
            self.buf += data


def send(conn, status, body):
    conn.sendall(f"{status} {json.dumps(body)}\n".encode())


def dispatch(mgr, line):
    """Turn one request line into (status, body)."""
    verb, _, rest = line.partition(" ")
    verb = verb.upper()
    try:
        body = json.loads(rest) if rest.strip() else {}
    except json.JSONDecodeError:
        return "REJECT", {"reason": "BAD_REQUEST", "detail": "body is not valid JSON"}
    if not isinstance(body, dict):
        return "REJECT", {"reason": "BAD_REQUEST", "detail": "body must be a JSON object"}
    if verb == "RESERVE":
        ok, info = mgr.reserve(body)
        return ("ACCEPT" if ok else "REJECT"), info
    if verb == "RELEASE":
        ok, info = mgr.release(body.get("id"))
    elif verb == "MODIFY":
        ok, info = mgr.modify(body)
    elif verb == "STATUS":
        ok, info = mgr.status(body.get("id"))
    else:
        return "REJECT", {"reason": "BAD_REQUEST", "detail": f"unknown verb '{verb}'"}
    return ("OK" if ok else "REJECT"), info


def handle_client(conn, addr, mgr, idle_timeout, max_line):
    """Runs in its own thread: serves one client until it disconnects."""
    owner = "%s:%s" % addr[:2]
    conn.settimeout(idle_timeout)                 # dead peers get dropped after this many idle seconds
    reader = LineReader(conn, max_line)
    try:
        with conn:
            while True:
                raw = reader.readline()
                if raw is None:
                    send(conn, "REJECT", {"reason": "BAD_REQUEST", "detail": "line too long"})
                    break
                if not raw:
                    break                         # client closed the connection
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    send(conn, *dispatch(mgr, line))
    except OSError as exc:                        # timeout, reset, broken pipe ...
        log.info("connection %s ended: %s", owner, exc)
    # A client that vanishes simply stops sending. Its reservation is a lease, so the
    # reaper thread removes it when the lease ends: nothing to clean up here.


class Server:
    """Listening socket + accept loop. One thread per connected client."""

    def __init__(self, addr, manager, idle_timeout=None, max_line=None):
        self.manager = manager
        self.idle_timeout = settings.IDLE_TIMEOUT_S if idle_timeout is None else idle_timeout
        self.max_line = settings.MAX_LINE if max_line is None else max_line
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(addr)
        self.sock.listen(128)
        self.sock.settimeout(0.2)                 # so the accept loop notices shutdown()
        self.server_address = self.sock.getsockname()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._done.set()

    def serve_forever(self):
        self._done.clear()
        try:
            while not self._stop.is_set():
                try:
                    conn, addr = self.sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=handle_client, daemon=True,
                                 args=(conn, addr, self.manager, self.idle_timeout, self.max_line)).start()
        finally:
            self._done.set()

    def shutdown(self):
        self._stop.set()
        self._done.wait(timeout=5)

    def server_close(self):
        self.sock.close()


def make_server(manager, host=None, port=None, idle_timeout=None):
    return Server((host or settings.SERVER_HOST, settings.SERVER_PORT if port is None else port),
                  manager, idle_timeout)


def clear_leftovers(ctrl):
    """A crashed earlier run can leave policies in the controller; drop them before serving."""
    try:
        ok = ctrl.clear()
    except Exception:
        ok = False
    log.info("leftover policies cleared" if ok else
             "could not clear leftover policies (controller down?)")


def _stop(*_):
    raise KeyboardInterrupt      # lets main()'s cleanup run on SIGTERM (kill, terminate())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--host", default=settings.SERVER_HOST)
    ap.add_argument("--port", type=int, default=settings.SERVER_PORT)
    ap.add_argument("--controller", default=settings.CONTROLLER_URL, help="Ryu REST base URL")
    ap.add_argument("--no-controller", action="store_true", help="fake controller (testing without Ryu)")
    ap.add_argument("--view", action="store_true", help="print the utilisation view whenever it changes")
    ap.add_argument("--idle-timeout", type=float, default=settings.IDLE_TIMEOUT_S)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)

    ctrl = NullController() if args.no_controller else HttpController(args.controller)
    mgr = ReservationManager(ctrl, view=args.view)
    clear_leftovers(ctrl)
    mgr.start()
    srv = make_server(mgr, args.host, args.port, args.idle_timeout)
    log.info("reservation server on %s:%d (controller: %s)", srv.server_address[0], srv.server_address[1],
             "FAKE" if args.no_controller else args.controller)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        mgr.shutdown()           # removes all installed policies
        log.info("stopped, policies removed")


if __name__ == "__main__":
    main()
