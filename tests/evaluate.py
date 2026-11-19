#!/usr/bin/env python3
"""Load evaluation on the real stack: Mininet + Ryu + reservation server + iperf3.

Run everything from the project folder.
Terminal 1:  ryu-manager controller/ryu_app.py
Terminal 2:  sudo python3 -m tests.evaluate

For each load level (low / medium / high = 3 / 6 / 12 simultaneous requests) the SAME set of
requests is run twice:

  WITH reservations : each request goes to the reservation server; accepted flows send iperf3
                      traffic at their reserved rate, rejected flows send nothing.
  BEST-EFFORT       : no reservation system; every requested flow just sends at its wanted rate.

Measured (results/evaluation.csv, results/evaluation.png):
  acceptance / rejection ratio, reserved utilisation of the 10 Mbps trunk (from the server's
  table), request-to-policy delay (server ACCEPT reply, includes the real Ryu round trip), and
  achieved vs reserved (or wanted) bandwidth per flow.
"""
import csv
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time

from mininet.log import setLogLevel

from app.client import ReservationClient
from config import settings as common
from topology import topo

LEVELS = {"low": 3, "medium": 6, "high": 12}
LEASE_S = 8              # reservation duration
FLOW_S = 6               # iperf3 traffic duration (inside the lease)
ARRIVAL_WINDOW_S = 2.0   # requests arrive at random times within this window
PAIRS = [(a, b) for a in ("h1", "h2", "h3", "h4") for b in ("h5", "h6", "h7", "h8")]
TRUNK = "s1->s2"
SERVER_CMD = [sys.executable, "-m", "app.server"]


def make_requests(n, seed):
    rng = random.Random(seed)
    return [{"src": a, "dst": b, "bw": round(rng.uniform(1, 4), 1),
             "at": round(rng.uniform(0, ARRIVAL_WINDOW_S), 2), "port": 5201 + i}
            for i, (a, b) in enumerate(rng.sample(PAIRS, n))]


def run_iperf(net, r):
    p = net.get(r["src"]).popen(
        f"iperf3 -c {common.HOSTS[r['dst']]['ip']} -p {r['port']} -t {FLOW_S} -b {r['bw']}M -l 16K -J",
        stdout=subprocess.PIPE, text=True)
    text = p.communicate()[0]
    try:
        return round(json.loads(text)["end"]["sum_received"]["bits_per_second"] / 1e6, 3)
    except (ValueError, KeyError):
        return 0.0


def run_mode(net, reqs, reserve):
    """Run every request once. Returns (per-request records, trunk utilisation samples)."""
    subprocess.run("pkill iperf3", shell=True)         # drop servers left over from earlier runs
    time.sleep(0.5)
    for r in reqs:                                     # iperf3 servers, one port per request
        net.get(r["dst"]).cmd(f"iperf3 -s -1 -D -p {r['port']}")
    time.sleep(0.8)

    records = [None] * len(reqs)
    samples, stop = [], threading.Event()

    def sampler():
        with ReservationClient() as c:
            while not stop.is_set():
                _, info = c.status()
                samples.append(info["links"][TRUNK]["reserved_mbps"])
                time.sleep(0.5)

    def worker(i, r):
        time.sleep(r["at"])
        rec = {**r, "accepted": True, "reason": "", "policy_delay_ms": None, "achieved": None}
        if reserve:
            with ReservationClient() as c:
                status, info = c.reserve(r["src"], r["dst"], r["bw"], LEASE_S)
            rec["accepted"] = status == "ACCEPT"
            rec["reason"] = info.get("reason", "")
            rec["policy_delay_ms"] = info.get("policy_delay_ms")
        if rec["accepted"]:
            rec["achieved"] = run_iperf(net, r)
        records[i] = rec

    sampler_thread = threading.Thread(target=sampler) if reserve else None
    if sampler_thread:
        sampler_thread.start()
    threads = [threading.Thread(target=worker, args=(i, r)) for i, r in enumerate(reqs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stop.set()
    if sampler_thread:
        sampler_thread.join()
    return records, samples


def wait_until_idle():
    """Wait until every reservation has expired (and policies are gone)."""
    with ReservationClient() as c:
        for _ in range(40):
            _, info = c.status()
            if not info["reservations"]:
                return
            time.sleep(0.5)
    time.sleep(1)


def summarise(level, reqs, with_res, best_effort, samples):
    n = len(reqs)
    acc = [r for r in with_res if r["accepted"]]
    delays = [r["policy_delay_ms"] for r in acc if r["policy_delay_ms"] is not None]
    busy = [s for s in samples if s > 0]
    return {
        "load": level, "requests": n, "accepted": len(acc), "rejected": n - len(acc),
        "accept_ratio": round(len(acc) / n, 3), "reject_ratio": round(1 - len(acc) / n, 3),
        "reserved_util_pct_mean": round(100 * statistics.mean(busy) / common.TRUNK_MBPS, 1) if busy else 0,
        "reserved_util_pct_peak": round(100 * max(samples) / common.TRUNK_MBPS, 1) if samples else 0,
        "offered_mbps": round(sum(r["bw"] for r in reqs), 1),
        "policy_delay_ms_mean": round(statistics.mean(delays), 1) if delays else 0,
        "policy_delay_ms_p95": round(sorted(delays)[int(0.95 * (len(delays) - 1))], 1) if delays else 0,
        "reserved_achieved_over_reserved": round(statistics.mean(r["achieved"] / r["bw"] for r in acc), 3) if acc else 0,
        "besteffort_achieved_over_wanted": round(statistics.mean(r["achieved"] / r["bw"] for r in best_effort), 3),
        "reserved_flows_getting_90pct": sum(1 for r in acc if r["achieved"] >= 0.9 * r["bw"]),
        "besteffort_flows_getting_90pct": sum(1 for r in best_effort if r["achieved"] >= 0.9 * r["bw"]),
    }


def main():
    setLogLevel("warning")
    os.makedirs("results", exist_ok=True)
    net = topo.build()
    net.start()
    net.waitConnected()
    server = subprocess.Popen(SERVER_CMD)
    time.sleep(1.0)
    rows, detail = [], {}
    try:
        for level, n in LEVELS.items():
            reqs = make_requests(n, seed=n)
            print(f"\n=== {level} load: {n} requests, {sum(r['bw'] for r in reqs):.1f} Mbps wanted "
                  f"on a {common.TRUNK_MBPS:.0f} Mbps trunk ===", flush=True)
            print(" with reservations ...", flush=True)
            with_res, samples = run_mode(net, reqs, reserve=True)
            wait_until_idle()
            print(" best-effort (no reservations) ...", flush=True)
            best_effort, _ = run_mode(net, reqs, reserve=False)
            time.sleep(2)
            row = summarise(level, reqs, with_res, best_effort, samples)
            rows.append(row)
            detail[level] = {"with_reservations": with_res, "best_effort": best_effort}
            print(" ", row, flush=True)
    finally:
        server.terminate()
        server.wait()
        net.stop()
        subprocess.run("pkill iperf3", shell=True)
    if not rows:
        return
    with open("results/evaluation.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open("results/evaluation_detail.json", "w") as f:
        json.dump(detail, f, indent=2)
    plot(rows)
    print("\nwrote results/evaluation.csv, evaluation_detail.json, evaluation.png")


def plot(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = [r["load"] for r in rows]
    x = range(len(rows))
    w = 0.38
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0][0].bar(names, [r["accept_ratio"] * 100 for r in rows], color="#3b8ea5", label="accepted")
    ax[0][0].bar(names, [r["reject_ratio"] * 100 for r in rows],
                 bottom=[r["accept_ratio"] * 100 for r in rows], color="#d9534f", label="rejected")
    ax[0][0].set_title("Accepted vs rejected requests (%)")
    ax[0][0].legend()
    ax[0][1].bar([i - w / 2 for i in x], [r["reserved_util_pct_mean"] for r in rows], w, label="mean", color="#5cb85c")
    ax[0][1].bar([i + w / 2 for i in x], [r["reserved_util_pct_peak"] for r in rows], w, label="peak", color="#2e7d32")
    ax[0][1].set_xticks(list(x)); ax[0][1].set_xticklabels(names)
    ax[0][1].set_title("Reserved utilisation of the 10 Mbps trunk (%)")
    ax[0][1].set_ylim(0, 105)
    ax[0][1].legend()
    ax[1][0].bar(names, [r["policy_delay_ms_mean"] for r in rows], color="#f0ad4e", label="mean")
    ax[1][0].plot(names, [r["policy_delay_ms_p95"] for r in rows], "ko-", label="p95")
    ax[1][0].set_title("Request-to-policy delay (ms)")
    ax[1][0].legend()
    ax[1][1].bar([i - w / 2 for i in x], [r["reserved_achieved_over_reserved"] * 100 for r in rows], w,
                 label="with reservations (accepted flows)", color="#3b8ea5")
    ax[1][1].bar([i + w / 2 for i in x], [r["besteffort_achieved_over_wanted"] * 100 for r in rows], w,
                 label="best-effort (all flows)", color="#999999")
    ax[1][1].set_xticks(list(x)); ax[1][1].set_xticklabels(names)
    ax[1][1].set_title("Achieved / reserved (or wanted) bandwidth (%)")
    ax[1][1].axhline(100, color="k", lw=0.6)
    ax[1][1].set_ylim(0, 130)
    ax[1][1].legend(loc="upper center", ncol=1)
    fig.suptitle("Reservation system vs best-effort under low / medium / high load")
    fig.tight_layout()
    fig.savefig("results/evaluation.png", dpi=130)


if __name__ == "__main__":
    main()
