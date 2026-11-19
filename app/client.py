#!/usr/bin/env python3
"""Reservation client: a small library (ReservationClient) plus a command line.

    python3 client.py reserve h1 h5 4 30        # 4 Mbps for 30 s
    python3 client.py modify 1 --bw 6
    python3 client.py release 1
    python3 client.py status [id]
"""
import argparse
import json
import socket
import sys

from config import settings


class ReservationClient:
    def __init__(self, host=None, port=None, timeout=10.0):
        self.sock = socket.create_connection((host or settings.SERVER_HOST, port or settings.SERVER_PORT),
                                             timeout=timeout)
        self.f = self.sock.makefile("rw", encoding="utf-8", newline="\n")

    def request(self, verb, body=None):
        self.f.write(f"{verb} {json.dumps(body or {})}\n")
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        status, _, rest = line.strip().partition(" ")
        return status, json.loads(rest)

    def reserve(self, src, dst, bw_mbps, duration_s):
        body = {"src": src, "dst": dst, "bw_mbps": bw_mbps, "duration_s": duration_s}
        return self.request("RESERVE", body)

    def release(self, rid):
        return self.request("RELEASE", {"id": rid})

    def modify(self, rid, bw_mbps=None, duration_s=None):
        body = {"id": rid}
        if bw_mbps is not None:
            body["bw_mbps"] = bw_mbps
        if duration_s is not None:
            body["duration_s"] = duration_s
        return self.request("MODIFY", body)

    def status(self, rid=None):
        return self.request("STATUS", {} if rid is None else {"id": rid})

    def close(self):
        try:
            self.f.close()
        finally:
            self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--host", default=settings.SERVER_HOST)
    ap.add_argument("--port", type=int, default=settings.SERVER_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reserve")
    r.add_argument("src"); r.add_argument("dst")
    r.add_argument("bw", type=float, help="Mbps"); r.add_argument("duration", type=float, help="seconds")
    m = sub.add_parser("modify")
    m.add_argument("id", type=int); m.add_argument("--bw", type=float); m.add_argument("--duration", type=float)
    rl = sub.add_parser("release"); rl.add_argument("id", type=int)
    s = sub.add_parser("status"); s.add_argument("id", type=int, nargs="?")
    a = ap.parse_args()

    with ReservationClient(a.host, a.port) as c:
        if a.cmd == "reserve":
            status, info = c.reserve(a.src, a.dst, a.bw, a.duration)
        elif a.cmd == "modify":
            status, info = c.modify(a.id, a.bw, a.duration)
        elif a.cmd == "release":
            status, info = c.release(a.id)
        else:
            status, info = c.status(a.id)
        print(status, json.dumps(info, indent=2))
        sys.exit(0 if status in ("ACCEPT", "OK") else 1)


if __name__ == "__main__":
    main()
