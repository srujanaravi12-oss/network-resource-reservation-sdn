#!/usr/bin/env python3
"""Scripted end-to-end demo.

Run everything from the project folder.
Terminal 1:  ryu-manager controller/ryu_app.py
Terminal 2:  sudo python3 -m tests.demo_mininet

The reservation server is started by this script with --view, so its utilisation view
("[utilisation t=...]" lines) prints in the same terminal and updates by itself.

Phases (each prints what it proves):
  1. best-effort baseline (no reservation)
  2. one accepted reservation: rule + meter installed, iperf3 gets about the reserved rate
  3. oversized request is rejected, nothing installed
  4. runtime change: MODIFY changes the meter; RELEASE removes the rule
  5. expiry: the rule disappears by itself when the reservation ends
  6. hard scenario: 12 simultaneous requests for the same bottleneck link, then a client
     that disappears without releasing; admission stays correct, cleanup is automatic
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from mininet.log import setLogLevel

from app.client import ReservationClient
from config import settings as common
from topology import topo

SECS = 8
os.makedirs("results", exist_ok=True)
results = {}


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def dump(title):
    print(f"\n--- {title}: s1 policy flows ---")
    print(sh("ovs-ofctl -O OpenFlow13 dump-flows s1 | grep 'priority=100'") or "(none)")
    print(f"--- {title}: s1 meters ---")
    print(sh("ovs-ofctl -O OpenFlow13 dump-meters s1 | grep -v 'OFPST'") or "(none)")


def policy_rules():
    return int(sh("ovs-ofctl -O OpenFlow13 dump-flows s1 | grep -c 'priority=100'").strip() or 0)


def controller_view():
    print("--- controller capacity view (trunk) ---")
    out = sh("curl -s http://127.0.0.1:8080/capacity")
    try:
        print("s1->s2:", json.loads(out)["s1->s2"])
    except (ValueError, KeyError):
        print("(could not read controller view)", out)


def iperf(net, flows, secs=SECS):
    """flows: list of (src, dst). All run at the same time. Returns Mbps received per flow."""
    procs = []
    sh("pkill iperf3")                    # a previous one-shot server may still hold the port
    time.sleep(0.5)
    for i, (s, d) in enumerate(flows):
        net.get(d).cmd(f"iperf3 -s -1 -D -p {5201 + i}")
    time.sleep(0.7)
    for i, (s, d) in enumerate(flows):
        procs.append(net.get(s).popen(
            f"iperf3 -c {common.HOSTS[d]['ip']} -p {5201 + i} -t {secs} -l 16K -J",
            stdout=subprocess.PIPE, text=True))
    out = []
    for p in procs:
        text = p.communicate()[0]
        try:
            out.append(round(json.loads(text)["end"]["sum_received"]["bits_per_second"] / 1e6, 2))
        except (ValueError, KeyError):
            out.append(float("nan"))
    return out


def say(msg):
    print("\n" + "=" * 70 + f"\n{msg}\n" + "=" * 70)


def check(label, ok):
    print(f"CHECK {label}: {'PASS' if ok else 'FAIL'}")
    results.setdefault("checks", {})[label] = bool(ok)


def hard_scenario(c):
    pairs = [(a, b) for a in ("h1", "h2", "h3") for b in ("h5", "h6", "h7", "h8")]   # 12 flows
    bws = [1.5, 2.0, 2.5, 1.0, 3.0, 1.5, 2.0, 2.5, 1.0, 3.0, 1.5, 2.0]
    out, lock, barrier = [], threading.Lock(), threading.Barrier(len(pairs))

    def worker(pair, bw):
        barrier.wait()                                     # all clients fire at the same instant
        with ReservationClient() as cl:
            st, info = cl.reserve(pair[0], pair[1], bw, 8)
        with lock:
            out.append((pair, bw, st, info))

    threads = [threading.Thread(target=worker, args=(p, b)) for p, b in zip(pairs, bws)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    accepted = [o for o in out if o[2] == "ACCEPT"]
    total = sum(o[1] for o in accepted)
    print(f"{len(accepted)} accepted, {len(out) - len(accepted)} rejected; accepted total = {total} Mbps on a 10 Mbps trunk")
    for o in out:
        print("  ", o[0], o[1], "Mbps ->", o[2], o[3].get("reason", ""))
    time.sleep(1)
    check("accepted bandwidth never exceeds trunk capacity", total <= 10 + 1e-9)
    check("one policy rule on s1 per accepted reservation", policy_rules() == len(accepted))
    controller_view()
    results["hard_accepted"], results["hard_rejected"] = len(accepted), len(out) - len(accepted)

    print("\nwaiting for the 8 s reservations to expire by themselves ...")
    time.sleep(9)
    check("all rules removed automatically after expiry", policy_rules() == 0)
    st, info = c.status()
    check("server table is empty again", info["links"]["s1->s2"]["reserved_mbps"] == 0)

    print("\nA client reserves and then DIES without releasing (kill -9):")
    code = ("from app.client import ReservationClient; import time; c = ReservationClient(); "
            "print(c.reserve('h4', 'h8', 3, 5), flush=True); time.sleep(999)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    print("client says:", proc.stdout.readline().strip())
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    time.sleep(1)
    print("client is dead; reservation is still held until its lease ends:")
    check("reservation still present right after the client died", policy_rules() == 1)
    time.sleep(6)
    check("rule removed automatically after the lease ended", policy_rules() == 0)
    st, info = c.status()
    check("capacity freed in the server table", info["links"]["s1->s2"]["reserved_mbps"] == 0)


def main():
    setLogLevel("warning")
    net = topo.build()
    net.start()
    net.waitConnected()
    server = subprocess.Popen([sys.executable, "-m", "app.server", "--view"])
    time.sleep(1.0)
    try:
        say("0. sanity: h1 -> h5 ping")
        print(net.get("h1").cmd(f"ping -c 2 {common.HOSTS['h5']['ip']}"))
        c = ReservationClient()

        say("1. BASELINE (best effort, no reservation): h1->h5 alone, then h1->h5 + h2->h6 together")
        results["baseline_single"] = iperf(net, [("h1", "h5")])
        results["baseline_two_flows"] = iperf(net, [("h1", "h5"), ("h2", "h6")])
        print("alone:", results["baseline_single"], "Mbps   together:", results["baseline_two_flows"], "Mbps")

        say("2. RESERVE h1->h5 4 Mbps, then measure")
        status, info = c.reserve("h1", "h5", 4, 60)
        print(status, info)
        rid = info.get("id")
        time.sleep(1)
        dump("after RESERVE")
        controller_view()
        results["reserved_4mbps"] = iperf(net, [("h1", "h5")])
        print("reserved 4 Mbps -> achieved:", results["reserved_4mbps"], "Mbps")
        print(sh("ovs-ofctl -O OpenFlow13 meter-stats s1 | grep -v 'OFPST'"))

        say("3. oversized request (20 Mbps on a 10 Mbps trunk) must be REJECTED")
        status, info = c.reserve("h2", "h6", 20, 30)
        print(status, info)
        check("oversized request rejected", status == "REJECT")
        dump("after rejected request (still only the first rule)")

        say("4. runtime change: MODIFY to 7 Mbps, measure, then RELEASE")
        print(*c.modify(rid, bw_mbps=7))
        time.sleep(1)
        dump("after MODIFY")
        results["modified_7mbps"] = iperf(net, [("h1", "h5")])
        print("modified to 7 Mbps -> achieved:", results["modified_7mbps"], "Mbps")
        print(*c.release(rid))
        time.sleep(1)
        dump("after RELEASE (should be empty)")
        check("rule removed after RELEASE", policy_rules() == 0)

        say("5. EXPIRY: reserve 3 Mbps for 5 s, watch the rule disappear by itself")
        print(*c.reserve("h3", "h7", 3, 5))
        time.sleep(1)
        dump("while active")
        time.sleep(6)
        dump("after expiry (should be empty)")
        check("rule removed after expiry", policy_rules() == 0)

        say("6. HARD SCENARIO: simultaneous requests, then a client that disappears")
        hard_scenario(c)
        c.close()
    finally:
        server.terminate()
        server.wait()
        net.stop()
        with open("results/demo.json", "w") as f:
            json.dump(results, f, indent=2)
        print("\nSaved results/demo.json:\n", json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
