"""Resource table, admission control, conflict detection and expiry. Thread-safe.

Lifecycle of a reservation:
    (request) -> REJECTED                       (never stored; only counted)
              -> PENDING  -> ACTIVE -> REMOVING -> EXPIRED | RELEASED
PENDING   capacity is already reserved, policy install is in flight
ACTIVE    policy installed, traffic is rate-limited, lease timer running
MODIFYING temporary state while a bandwidth change is pushed to the controller
REMOVING  policy removal in flight (capacity is freed only AFTER removal)

Key design rule: the capacity check and the capacity commit happen inside ONE
critical section (self.lock). Otherwise two threads could both see "free" and
both be accepted -> over-subscription. The slow controller call happens OUTSIDE
the lock so one slow install does not block every other client.
"""
import itertools
import logging
import math
import threading
import time
from collections import Counter

from config import settings as common

log = logging.getLogger("manager")

PENDING, ACTIVE, MODIFYING, REMOVING = "PENDING", "ACTIVE", "MODIFYING", "REMOVING"
EXPIRED, RELEASED = "EXPIRED", "RELEASED"
MAX_BW_MBPS = 1000.0
MAX_DURATION_S = 86400.0
EPS = 1e-6
RETRY_DELAY_S = 1.0     # how long to wait before retrying a failed policy removal


class RequestError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _host(req, key):
    v = req.get(key)
    if not isinstance(v, str):
        raise RequestError("BAD_REQUEST", f"'{key}' is required (a host name)")
    if v not in common.HOSTS:
        raise RequestError("UNKNOWN_HOST", f"unknown host '{v}'")
    return v


def _number(req, key, hi):
    v = req.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not (0 < v <= hi):
        raise RequestError("BAD_REQUEST", f"'{key}' must be a number in (0, {hi}]")
    return float(v)


def _id(v):
    if isinstance(v, bool):
        raise RequestError("BAD_REQUEST", "'id' must be an integer")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise RequestError("BAD_REQUEST", "'id' must be an integer") from None


class Reservation:
    def __init__(self, rid, src, dst, bw, links, switches, created):
        self.id, self.src, self.dst, self.bw = rid, src, dst, bw
        self.links, self.switches = links, switches
        self.created = created
        self.state = PENDING
        self.start = self.end = self.policy_delay = 0.0

    def info(self, now):
        live = self.state in (ACTIVE, MODIFYING, REMOVING)
        return {"id": self.id, "src": self.src, "dst": self.dst, "bw_mbps": self.bw,
                "state": self.state, "path": self.links,
                "remaining_s": round(max(0.0, self.end - now), 2) if live else None}


class ReservationManager:
    def __init__(self, controller, capacities=None, clock=time.monotonic, sample_interval=0.5,
                 view=False):
        self.controller = controller
        self.cap = dict(capacities or common.capacities())
        self.clock = clock
        self.sample_interval = sample_interval
        self.view = view                 # print the utilisation view whenever it changes
        self._last_view_key = None
        self.lock = threading.RLock()
        self.reserved = {l: 0.0 for l in self.cap}   # Mbps promised per directed link
        self.res = {}        # live reservations (PENDING/ACTIVE/...)
        self.done = {}       # finished reservations (EXPIRED/RELEASED), for STATUS
        self._ids = itertools.count(1)
        self.stats = Counter()
        self.install_delays = []   # request -> policy-installed, seconds
        self.util_log = []         # (seconds since start, {link: reserved Mbps})
        self.t0 = clock()
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self._stop.clear()
        self.t0 = self.clock()
        self._thread = threading.Thread(target=self._loop, name="reaper", daemon=True)
        self._thread.start()

    def shutdown(self):
        """Stop the reaper and remove every policy we installed."""
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        with self.lock:
            active = [r for r in self.res.values() if r.state == ACTIVE]
        for r in active:
            self._teardown(r, RELEASED)

    def now(self):
        return self.clock() - self.t0

    # ---------------------------------------------------------------- RESERVE
    def _reject(self, reason, **extra):
        with self.lock:
            self.stats["rejected"] += 1
            self.stats["rejected_" + reason] += 1
        return False, {"reason": reason, **extra}

    @staticmethod
    def _err(reason, **extra):
        return False, {"reason": reason, **extra}

    def reserve(self, req):
        t0 = self.clock()
        if not isinstance(req, dict):
            return self._reject("BAD_REQUEST", detail="body must be a JSON object")
        try:
            src, dst = _host(req, "src"), _host(req, "dst")
            if src == dst:
                raise RequestError("BAD_REQUEST", "src and dst must differ")
            bw = _number(req, "bw_mbps", MAX_BW_MBPS)
            dur = _number(req, "duration_s", MAX_DURATION_S)
        except RequestError as e:
            return self._reject(e.reason, detail=e.detail)
        links, switches = common.path(src, dst)

        # ---- admission: check + commit atomically ----
        with self.lock:
            for r in self.res.values():          # conflict: same flow already reserved
                if r.src == src and r.dst == dst:
                    return self._reject("CONFLICT", existing_id=r.id,
                                        detail="a reservation for this src->dst already exists; "
                                               "MODIFY or RELEASE it first")
            for l in links:                      # capacity on EVERY link of the path
                free = self.cap[l] - self.reserved[l]
                if bw > free + EPS:
                    return self._reject("INSUFFICIENT_BANDWIDTH", link=l, requested_mbps=bw,
                                        available_mbps=round(max(free, 0.0), 3))
            r = Reservation(next(self._ids), src, dst, bw, links, switches, t0)
            for l in links:
                self.reserved[l] += bw
            self.res[r.id] = r

        # ---- install policy (outside the lock) ----
        try:
            ok = self.controller.install(r.id, common.HOSTS[src]["ip"], common.HOSTS[dst]["ip"],
                                         int(bw * 1000), [common.SWITCH_DPID[s] for s in switches], links)
        except Exception:
            log.exception("controller.install raised")
            ok = False
        if not ok:
            try:
                self.controller.remove(r.id)     # best effort: clean partial installs
            except Exception:
                pass
        now = self.clock()
        with self.lock:
            if not ok:                           # roll back the reservation
                for l in links:
                    self.reserved[l] = max(0.0, self.reserved[l] - bw)
                del self.res[r.id]
                return self._reject("CONTROLLER_ERROR", detail="could not install the policy")
            r.state, r.start, r.end = ACTIVE, now, now + dur
            r.policy_delay = now - t0
            self.stats["accepted"] += 1
            self.install_delays.append(r.policy_delay)
            log.info("ACCEPT id=%d %s->%s %.2f Mbps for %.1fs", r.id, src, dst, bw, dur)
            return True, {"id": r.id, "expires_in_s": dur, "path": links,
                          "policy_delay_ms": round(r.policy_delay * 1000, 2)}

    # ---------------------------------------------------------------- RELEASE
    def _teardown(self, r, final_state):
        """Remove the policy, THEN free the capacity. Returns 'ok'|'not_active'|'controller_error'."""
        with self.lock:
            if r.state != ACTIVE:
                return "not_active"
            r.state = REMOVING
        try:
            ok = self.controller.remove(r.id)
        except Exception:
            log.exception("controller.remove raised")
            ok = False
        with self.lock:
            if not ok:
                # keep the capacity reserved (the rule may still be on the switch) and retry later
                r.state = ACTIVE
                r.end = self.clock() + RETRY_DELAY_S
                self.stats["remove_failed"] += 1
                return "controller_error"
            for l in r.links:
                self.reserved[l] = max(0.0, self.reserved[l] - r.bw)
            r.state = final_state
            self.res.pop(r.id, None)
            self.done[r.id] = r
            self.stats[final_state.lower()] += 1
            log.info("%s id=%d", final_state, r.id)
            return "ok"

    def release(self, rid):
        try:
            rid = _id(rid)
        except RequestError as e:
            return self._err(e.reason, detail=e.detail)
        with self.lock:
            r = self.res.get(rid)
        if r is None:
            return self._err("UNKNOWN_ID")
        result = self._teardown(r, RELEASED)
        if result == "ok":
            return True, {"id": rid}
        return self._err("CONTROLLER_ERROR" if result == "controller_error" else "BUSY")

    # ---------------------------------------------------------------- MODIFY
    def modify(self, req):
        if not isinstance(req, dict):
            return self._err("BAD_REQUEST", detail="body must be a JSON object")
        try:
            rid = _id(req.get("id"))
            new_bw = _number(req, "bw_mbps", MAX_BW_MBPS) if "bw_mbps" in req else None
            new_dur = _number(req, "duration_s", MAX_DURATION_S) if "duration_s" in req else None
            if new_bw is None and new_dur is None:
                raise RequestError("BAD_REQUEST", "give bw_mbps and/or duration_s")
        except RequestError as e:
            return self._err(e.reason, detail=e.detail)

        with self.lock:
            r = self.res.get(rid)
            if r is None:
                return self._err("UNKNOWN_ID")
            if r.state != ACTIVE:
                return self._err("BUSY")
            old_bw, old_end = r.bw, r.end
            bw = new_bw if new_bw is not None else old_bw
            for l in r.links:                    # my own old share counts as free
                free = self.cap[l] - self.reserved[l] + old_bw
                if bw > free + EPS:
                    return self._err("INSUFFICIENT_BANDWIDTH", link=l,
                                     available_mbps=round(max(free, 0.0), 3))
            for l in r.links:
                self.reserved[l] += bw - old_bw
            r.bw = bw
            if new_dur is not None:
                r.end = self.clock() + new_dur
            changed = abs(bw - old_bw) > EPS
            if changed:
                r.state = MODIFYING              # blocks expiry/release during the update

        if changed:
            try:
                ok = self.controller.modify(r.id, int(bw * 1000))
            except Exception:
                log.exception("controller.modify raised")
                ok = False
            with self.lock:
                if not ok:                       # roll back
                    for l in r.links:
                        self.reserved[l] += old_bw - bw
                    r.bw, r.end = old_bw, old_end
                    r.state = ACTIVE
                    return self._err("CONTROLLER_ERROR")
                r.state = ACTIVE
        log.info("MODIFY id=%d bw=%.2f", r.id, r.bw)
        return True, {"id": r.id, "bw_mbps": r.bw, "remaining_s": round(max(0, r.end - self.clock()), 2)}

    # ---------------------------------------------------------------- STATUS
    def status(self, rid=None):
        now = self.clock()
        with self.lock:
            if rid is not None:
                try:
                    rid = _id(rid)
                except RequestError as e:
                    return self._err(e.reason, detail=e.detail)
                r = self.res.get(rid) or self.done.get(rid)
                return (True, r.info(now)) if r else self._err("UNKNOWN_ID")
            links = {l: {"capacity_mbps": c, "reserved_mbps": round(self.reserved[l], 3),
                         "utilisation_pct": round(100 * self.reserved[l] / c, 1)}
                     for l, c in self.cap.items() if self.reserved[l] > 0 or "s1->s2" in l
                     or "s2->s1" in l}
            return True, {"links": links,
                          "reservations": [r.info(now) for r in self.res.values()],
                          "stats": dict(self.stats)}

    # ---------------------------------------------------------------- background thread
    def _loop(self):
        next_sample = 0.0
        while not self._stop.wait(0.1):
            now = self.clock()
            with self.lock:
                due = [r for r in self.res.values() if r.state == ACTIVE and r.end <= now]
            for r in due:
                self._teardown(r, EXPIRED)
            if self.view:
                self._maybe_print_view()
            if self.now() >= next_sample:
                with self.lock:
                    self.util_log.append((self.now(), dict(self.reserved)))
                next_sample = self.now() + self.sample_interval

    def format_view(self):
        """One-line utilisation view: both trunk directions + every live reservation."""
        now = self.clock()
        with self.lock:
            trunk = " | ".join("%s %.1f/%.1f Mbps (%d%%)" % (l, self.reserved[l], self.cap[l],
                                                              round(100 * self.reserved[l] / self.cap[l]))
                               for l in ("s1->s2", "s2->s1"))
            live = ", ".join("#%d %s->%s %.1fM %s" % (r.id, r.src, r.dst, r.bw, r.state)
                             for r in sorted(self.res.values(), key=lambda r: r.id)) or "none"
        return "[utilisation t=%6.1fs] %s | reservations: %s" % (self.now(), trunk, live)

    def _maybe_print_view(self):
        with self.lock:
            key = tuple(sorted((r.id, r.bw, r.state) for r in self.res.values()))
        if key != self._last_view_key:
            self._last_view_key = key
            print(self.format_view(), flush=True)

    def avg_utilisation(self, link, t_from=None, t_to=None):
        """Time-averaged reserved/capacity (percent) of one link between two times."""
        with self.lock:
            pts = [s[link] for t, s in self.util_log
                   if (t_from is None or t >= t_from) and (t_to is None or t <= t_to)]
        return 100 * (sum(pts) / len(pts)) / self.cap[link] if pts else 0.0
