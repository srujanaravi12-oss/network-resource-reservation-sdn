#!/usr/bin/env python3
"""Optional check: does a reserved flow keep its rate when UNRESERVED traffic competes on the same trunk?

Run everything from the project folder.
Terminal 1:  ryu-manager controller/ryu_app.py
Terminal 2:  sudo python3 -m tests.contention_test [--bw 7] [--no-reserve]        (about 40 seconds)

  A  the h1 -> h5 flow alone, sending at --bw Mbps (default 4); reserved unless --no-reserve
  B  the same flow PLUS an unreserved flow h2 -> h6 that sends as fast as it can
     (both cross the 10 Mbps trunk)

--no-reserve is the CONTROL run: identical traffic, but no reservation is made. Compare it with the
reserved run to see whether the reservation (and not just TCP sharing or iperf3 pacing) made the difference.

Results go to results/contention_<bw>mbps.json (control run: results/contention_<bw>mbps_control.json).
A meter only CAPS a flow; it does not protect it. If the reserved flow ends up well below its
reserved rate while competing, that is the known limitation and should be reported as such.
"""
import argparse
import json
import os
import subprocess
import sys
import time

from mininet.log import setLogLevel

from app.client import ReservationClient
from config import settings as common
from topology import topo

SECS = 8
SERVER_CMD = [sys.executable, "-m", "app.server"]


def run(net, flows, secs=SECS):
    """flows: list of dicts {src, dst, port, rate (Mbps or None = as fast as possible)}.
    All run at the same time. Returns the received Mbps of each flow."""
    subprocess.run("pkill iperf3", shell=True)
    time.sleep(0.5)
    for f in flows:
        net.get(f["dst"]).cmd(f"iperf3 -s -1 -D -p {f['port']}")
    time.sleep(0.8)
    procs = []
    for f in flows:
        rate = f"-b {f['rate']}M " if f["rate"] else ""
        procs.append(net.get(f["src"]).popen(
            f"iperf3 -c {common.HOSTS[f['dst']]['ip']} -p {f['port']} -t {secs} {rate}-l 16K -J",
            stdout=subprocess.PIPE, text=True))
    out = []
    for p in procs:
        text = p.communicate()[0]
        try:
            out.append(round(json.loads(text)["end"]["sum_received"]["bits_per_second"] / 1e6, 2))
        except (ValueError, KeyError):
            out.append(float("nan"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--bw", type=float, default=4.0, help="rate of the h1->h5 flow in Mbps (default 4)")
    ap.add_argument("--no-reserve", action="store_true",
                    help="control run: send the same traffic WITHOUT making a reservation")
    a = ap.parse_args()
    bw = a.bw

    setLogLevel("warning")
    os.makedirs("results", exist_ok=True)
    net = topo.build()
    net.start()
    net.waitConnected()
    server = subprocess.Popen(SERVER_CMD)      # also clears any leftover policies in Ryu at start
    time.sleep(1.0)
    client, rid, results = None, None, {}
    try:
        if a.no_reserve:
            print("CONTROL RUN: no reservation is made; the h1->h5 flow is only paced at %.1f Mbps." % bw)
        else:
            client = ReservationClient()
            status, info = client.reserve("h1", "h5", bw, 120)
            print(status, info)
            if status != "ACCEPT":
                raise SystemExit("the reservation was not accepted; is Ryu running and are the switches connected?")
            rid = info["id"]
        time.sleep(1)
        flow = {"src": "h1", "dst": "h5", "port": 5201, "rate": bw}
        unreserved = {"src": "h2", "dst": "h6", "port": 5202, "rate": None}

        print("\nA. h1->h5 flow alone ...", flush=True)
        alone = run(net, [flow])
        print("   h1->h5 flow:", alone, "Mbps (sending at %.1f)" % bw)

        print("\nB. h1->h5 flow + UNRESERVED flow competing ...", flush=True)
        both = run(net, [flow, unreserved])
        print("   h1->h5 flow:", both[0], "Mbps   unreserved h2->h6 flow:", both[1], "Mbps")

        pct = 100 * both[0] / bw
        print("\nRESULT (%s): under competition the h1->h5 flow got %.0f%% of %.1f Mbps."
              % ("CONTROL, no reservation" if a.no_reserve else "reserved", pct, bw))
        results = {"mode": "control (no reservation)" if a.no_reserve else "reserved",
                   "flow_mbps": bw, "alone": alone, "with_unreserved_competitor": both,
                   "percent_of_flow_rate_under_competition": round(pct, 1)}
    finally:
        if client is not None:
            if rid is not None:
                try:
                    client.release(rid)          # leave no policy behind in Ryu
                except Exception:
                    pass
            client.close()
        server.terminate()
        server.wait()
        net.stop()
        subprocess.run("pkill iperf3", shell=True)
        if results:
            path = "results/contention_%gmbps%s.json" % (bw, "_control" if a.no_reserve else "")
            with open(path, "w") as f:
                json.dump(results, f, indent=2)
            print("saved", path)


if __name__ == "__main__":
    main()
