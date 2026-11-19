# Network Resource Reservation System (TCP + SDN) — Team 14

Users ask a TCP service to reserve bandwidth between two hosts. The service runs admission
control; accepted reservations become OpenFlow **meter + flow** policies installed by a Ryu
controller, and are removed when the reservation expires or is released.

```
 client ──TCP :9000──> reservation server ──REST :8080──> Ryu controller ──OpenFlow──> s1, s2
 (client.py)           (server.py + manager.py)           (ryu_app.py + capacity.py)   (Mininet, topo.py)
                       resource table, admission,         capacity view, meters,
                       conflicts, expiry, utilisation     flow rules
```

Topology (`config/config.json`, `topology/topo.py`): h1–h4 on s1, h5–h8 on s2, access links 100 Mbps,
**s1–s2 trunk 10 Mbps**. Links are *directed* (`s1->s2` and `s2->s1` have separate capacity).

The core protocol uses Python's plain `socket` and `threading` modules only (no `socketserver`, no web
framework on the TCP side). TCP is a byte stream, so `app/server.py` has its own line reader that buffers
`recv()` data and splits it on `\n`. The only non-socket pieces are Mininet and Ryu, which the assignment requires.

## Repository layout
| Path | Role |
|---|---|
| `app/server.py` | concurrent TCP server (raw `socket` + one thread per client) |
| `app/client.py` | TCP client and command line |
| `app/manager.py` | resource table, admission, conflicts, expiry, utilisation (core logic, one lock) |
| `app/controller_client.py` | server → controller REST client (`NullController` is used only by unit tests) |
| `controller/ryu_app.py` | Ryu app: capacity view, installs/modifies/removes meters and flow rules |
| `controller/capacity.py` | the controller's capacity view (refuses over-subscription) |
| `topology/topo.py` | Mininet topology |
| `config/config.json`, `config/settings.py` | topology, capacities, ports, timeouts (one place) |
| `tests/test_reservation.py` | 31 unit/integration tests (no Mininet needed) |
| `tests/demo_mininet.py` | scripted demo: flow/meter dumps, iperf3, hard scenario (10 automatic checks) |
| `tests/evaluate.py` | low/medium/high load evaluation with real traffic, vs a best-effort run |
| `tests/contention_test.py` | optional: a reserved flow against competing UNRESERVED traffic |
| `tests/expected_output.txt` | what a passing run looks like |
| `results/` | CSV / PNG / JSON written by the evaluation scripts |
| `docs/PROTOCOL.md` | protocol specification (request fields, messages, reasons, lifecycle) |

## Setup (Ubuntu)
```bash
sudo apt install mininet openvswitch-switch iperf3 python3-pip curl
pip3 install matplotlib
# Ryu needs Python 3.9 (eventlet 0.30.2). On new Ubuntu use a venv:
sudo apt install pipx && pipx install uv && pipx ensurepath      # then reopen the terminal
uv venv --python 3.9 ~/ryu-env
uv pip install --python ~/ryu-env/bin/python "setuptools<68" wheel pbr
uv pip install --python ~/ryu-env/bin/python --no-build-isolation "eventlet==0.30.2" ryu
```

## Run (exact steps)
**Run every command from the project root folder** (the one containing `app/`, `controller/`, ...). The
`python3 -m ...` form is required so the packages `app`, `config`, `topology` can be imported.

```bash
# 0. Unit tests (no Mininet or Ryu needed)
python3 -m unittest -v                         # expect: Ran 31 tests ... OK

# 1. Clean old Mininet state. Do this BEFORE starting Ryu ('mn -c' also kills ryu-manager)
sudo mn -c

# 2. Terminal 1: controller (prints "capacity view: ..." after every policy change)
~/ryu-env/bin/ryu-manager "$PWD/controller/ryu_app.py"
#    (use the full path: Ryu cannot load a relative path like controller/ryu_app.py.
#     Alternative: cd controller && ~/ryu-env/bin/ryu-manager ryu_app.py, then cd .. for the next steps)

# 3. Terminal 2: scripted demo (starts Mininet and the server itself)
sudo python3 -m tests.demo_mininet             # expect 10 lines "CHECK ...: PASS"

# 4. Terminal 2: load evaluation -> results/evaluation.csv and .png
sudo python3 -m tests.evaluate

# 5. Optional: reserved flow vs competing unreserved traffic (and the control run)
sudo python3 -m tests.contention_test --bw 7
sudo python3 -m tests.contention_test --bw 7 --no-reserve

# 6. Results are created as root; give them back to yourself
sudo chown -R $USER:$USER results
```
Manual use (three terminals, Ryu already running):
```bash
sudo python3 -m topology.topo                  # Mininet CLI
python3 -m app.server --view                   # reservation server with live utilisation view
python3 -m app.client reserve h1 h5 4 30       # reserve 4 Mbps h1 -> h5 for 30 s
python3 -m app.client status
```
Inspect switches: `sudo ovs-ofctl -O OpenFlow13 dump-flows s1`, `dump-meters s1`, `meter-stats s1`.
Controller view: `curl http://127.0.0.1:8080/capacity`.

## Assignment requirements → where they are
| Requirement | Where |
|---|---|
| TCP reservation protocol | `app/server.py`, `app/client.py`, `docs/PROTOCOL.md` (RESERVE, RELEASE, STATUS, MODIFY) |
| Handle concurrent requests | thread per connection (`app/server.py`), one lock around check+commit (`app/manager.py`) |
| Maintain resource information | `manager.reserved` (`app/manager.py`) / `STATUS`; controller view in `controller/capacity.py` |
| Detect reservation conflicts | `manager.reserve` → `CONFLICT`, `INSUFFICIENT_BANDWIDTH` |
| Install corresponding SDN policies | `controller/ryu_app.py` (meter + flow per switch on the path) |
| Reservation expiry | reaper thread in `manager._loop` |
| Track resource utilisation | `STATUS`, `util_log`, live view (`app.server --view`), controller log |
| Evaluate accepted / rejected requests | `manager.stats`, `tests/evaluate.py` |
| To-do 1–2 architecture, fields, rules, lifecycle | this README, `docs/PROTOCOL.md`, header of `app/manager.py` (the architecture *drawing* is for your SRS) |
| To-do 3–4 server/client, locking, bad requests, dropped connections | `server.py`, `manager.py`, tests `test_garbage_*`, `test_dropped_client_*` |
| To-do 5 Mininet topology | `topology/topo.py` |
| To-do 6 controller holds capacity view + installs policy; dumps | `controller/ryu_app.py`, `controller/capacity.py`, demo phase 2 |
| To-do 7 install on accept, remove on expiry/release; measured effect | demo phases 2, 4, 5 |
| To-do 8 basics: accepted + rule + iperf, oversized rejected | demo phases 2–3, `test_oversized_*` |
| To-do 9 concurrency, conflicts, over-subscription, expiry, early release, utilisation | tests, demo phases 4–6 |
| To-do 9 runtime install/modify/remove with utilisation view updating automatically | demo phases 2, 4, 5 (server `--view`, Ryu log) |
| To-do 9 hard scenario | demo phase 6; `test_concurrent_requests_never_oversubscribe`, `test_many_clients_compete` |
| To-do 10 acceptance/rejection, utilisation, delay, achieved vs reserved, vs best-effort | `tests/evaluate.py` |
| To-do 11 spec, README, topology, controller, server, tests, graphs, scripted demo | all of the above |

## Design decisions (be ready to explain)
**Admission logic.** For every directed link on the path: `reserved[link] + bw <= capacity[link]`. All links must
pass. Check and commit happen in one critical section, so two simultaneous requests can never both claim the
last free bandwidth. The slow controller call happens *outside* the lock; if it fails the reservation is rolled
back (`CONTROLLER_ERROR`). The controller independently refuses any policy that would over-subscribe a link.

**Conflict detection.** (1) A second reservation for the same `src->dst` flow while one is live is `CONFLICT`
(a flow rule is keyed on src/dst; two would overwrite each other) — use MODIFY. (2) Over-subscription is
`INSUFFICIENT_BANDWIDTH` and names the link and what is available.

**Expiry and cleanup.** A reservation is a lease. One reaper thread removes expired leases: policy removed from the
controller **first**, then capacity freed. If removal fails, capacity stays reserved and removal is retried.
A client that vanishes without RELEASE is handled by the lease; the server also drops idle sockets after 60 s.

## Assumptions to confirm with your teacher
1. The assignment gives no topology, so I chose 8 hosts / 2 switches / one 10 Mbps trunk.
2. "Conflict" is not defined in the assignment; I used "same src→dst already reserved" (above).
3. MODIFY is included because to-do 9 says policies are "modified at runtime as reservations … change".
4. A "reservation" is one-directional (src → dst).

## Measured results (real Mininet + Ryu + iperf3; latest run, iperf3 `-l 16K`)
| Load | Requests | Accepted / rejected | Reserved trunk utilisation (mean while busy / peak) | Reserved flows: achieved / reserved | Best-effort flows: achieved / wanted | Flows reaching 90% of their rate, out of all requested (reserved / best-effort) | Mean request-to-policy delay |
|---|---|---|---|---|---|---|---|
| low | 3 | 3 / 0 | 62.8% / 66.0% | 100.7% | 100.5% | 3 of 3 / 3 of 3 | 4.2 ms |
| medium | 6 | 4 / 2 | 83.0% / 85.0% | 100.7% | 82.2% | 4 of 6 / 4 of 6 | 2.7 ms |
| high | 12 | 4 / 8 | 91.6% / 93.0% | 100.2% | 47.1% | 4 of 12 / 1 of 12 | 3.0 ms |

Demo (`tests/demo_mininet.py`): one unreserved iperf3 flow reaches 9.58 Mbps; with a 4 Mbps reservation the same
unconstrained flow is held to 3.93 Mbps, and after MODIFY to 7 Mbps it reaches 7.05 Mbps. Two unreserved flows
sharing the trunk got 8.76 and 0.80 Mbps (best effort gives no one a guarantee). All 10 automatic checks pass.

How to read these numbers:
* Test flows send at exactly their reserved rate, so ~100% shows accepted flows are not squeezed under load.
  That the meter *caps* excess traffic is shown by the demo (the unconstrained flow is held to the reserved rate).
* The best-effort average includes flows the reservation system rejects. Reservations protect the accepted flows
  by turning others away; at low load best effort is just as good because nothing competes.
* At medium load the number of flows reaching 90% was the same for both (4 of 6), but best effort averaged only
  82% because two flows were badly starved. In an earlier run with identical code the best-effort count was
  1 of 6. These counts are noisy for best effort. The repeatable results are: every accepted reserved flow gets
  its rate at every load, and at high load best effort collapses (about 47%, with several flows near zero).
* Flows start at random times within 2 s, so best-effort contention is only partial (measured aggregates reach
  about 14 Mbps on the 10 Mbps trunk). Fully simultaneous contention would look worse for best effort.
* "Utilisation" is reserved bandwidth from the server's table, averaged over the samples taken while at least one
  reservation was active. It is not measured link usage.
* "Request-to-policy delay" runs from the request until Ryu's REST call returns. OpenFlow messages are sent
  asynchronously (no barrier), so the rule is on the switch very slightly later. The p95 column is not meaningful
  with only 3 to 12 samples; quote the mean.

## Honest limitations
1. **A meter is a cap, not a guarantee.** It limits a reserved flow to its rate but does not protect it from
   best-effort traffic. In `tests/evaluate.py` rejected flows send no traffic, so accepted flows never compete with
   unreserved traffic: **the main evaluation does not test a reserved flow against competing best-effort traffic.** `tests/contention_test.py` does. With one unreserved flow saturating the trunk, a 4 Mbps reservation measured 3.93 Mbps (98%) and a 7 Mbps reservation measured 6.39 Mbps (91%; the competitor got 3.16 Mbps). So the reserved flow kept most of its rate, but it lost about 9% at 7 Mbps, which is no guarantee. **The control run shows the reservation made no measurable difference here:** with the same traffic and NO reservation, the 7 Mbps flow got 6.33 Mbps (90%) and the competitor 3.22 Mbps, against 6.39 (91%) and 3.16 with the reservation. The difference is within run-to-run noise, so the flow's resilience came from iperf3's steady pacing, not from the reservation. The meter limits a flow to its rate; it does not protect it. A true guarantee
   would need queues with a minimum rate.
2. Verified on Ubuntu with Mininet 2.3.0, Open vSwitch 3.7.1, iperf3 3.20 and Ryu 4.34. Other versions may need
   small fixes (see Troubleshooting).
3. Forwarding uses the switch's `NORMAL` action; paths are static. No authentication on RELEASE.
4. **Measurement artefact found and fixed.** With iperf3's default 128 KB writes, reservations of 1.0 and 1.4 Mbps
   measured about 87% of their rate. With `-l 16K` (used by `tests/demo_mininet.py` and `tests/evaluate.py`) every reserved flow
   measured 99.8% to 101.4%. The change was to the traffic generator only.
5. Small sample. The figures above are one run per load level. Across two runs with `-l 16K`, reserved flows
   measured 100.2% to 100.7% both times, while the best-effort mean varied from 77% to 82% (medium load) and
   47% to 48% (high load), and the number of best-effort flows reaching 90% at medium load varied from
   1 to 4 of 6. Repeat the evaluation a few times if you need error bars.
6. Crash recovery is partial. The server now removes leftover controller policies when it starts and shuts down
   cleanly on Ctrl+C or SIGTERM. A power loss or `kill -9` of the server, or a Ryu restart while reservations are
   active (the switches can keep rules Ryu no longer knows about, or the server can think rules exist that were
   lost), is not handled and has not been tested.

## Troubleshooting
* `ryu-manager` crashes on import (eventlet): use Python 3.9–3.10, or `pip install eventlet==0.30.2`.
* Meters ignored / `dump-meters` errors: needs OVS ≥ 2.10 (Linux ≥ 4.15). Fallback: `datapath='user'` switches.
* `Unable to contact the remote controller at 127.0.0.1:6653` (and the demo hangs): Ryu is not running. **`sudo mn -c` also kills `ryu-manager`**, so run it only BEFORE starting Ryu, never while Ryu is up. Order: `sudo mn -c` → start Ryu → run the demo. (`sch_htb: quantum ... is big` warnings are harmless.)
* Every RESERVE gives `CONTROLLER_ERROR`: Ryu REST is not on :8080 the switches have not connected yet, or an older Ryu session is still holding policies (restart Ryu).
* `Error creating interface pair (s1-eth5,s2-eth5): File exists`: leftover Mininet ports from an interrupted run.
  Stop Ryu (Ctrl+C), run `sudo mn -c`, restart Ryu, then run the script again.
* Files in `results/` are owned by root (created under `sudo`): `sudo chown -R $USER:$USER results`.
