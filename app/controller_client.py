"""How the reservation server talks to the SDN controller.

Interface (all return True on success, False on failure):
    install(rid, src_ip, dst_ip, rate_kbps, dpids, links)
    modify(rid, rate_kbps)
    remove(rid)
    clear()
"""
import json
import logging
import threading
import time
import urllib.request

from config import settings

log = logging.getLogger("controller")


class NullController:
    """Stand-in controller used only by the unit tests (no Mininet needed).
    Records calls, can simulate latency, and can be told to fail."""

    def __init__(self, latency=0.0, fail=False):
        self.latency = latency
        self.fail = fail
        self.calls = []
        self._lock = threading.Lock()

    def _do(self, *call):
        if self.latency:
            time.sleep(self.latency)
        with self._lock:
            self.calls.append(call)
        return not self.fail

    def install(self, rid, src_ip, dst_ip, rate_kbps, dpids, links):
        return self._do("install", rid, rate_kbps, tuple(links))

    def modify(self, rid, rate_kbps):
        return self._do("modify", rid, rate_kbps)

    def remove(self, rid):
        return self._do("remove", rid)

    def clear(self):
        return self._do("clear")

    def count(self, name):
        with self._lock:
            return sum(1 for c in self.calls if c[0] == name)


class HttpController:
    """Talks to the Ryu app (ryu_app.py) over its REST API."""

    def __init__(self, base=None, timeout=3.0):
        self.base = (base or settings.CONTROLLER_URL).rstrip("/")
        self.timeout = timeout

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:  # HTTPError, URLError, timeout ...
            log.warning("controller %s %s failed: %s", method, path, exc)
            return False

    def install(self, rid, src_ip, dst_ip, rate_kbps, dpids, links):
        return self._call("POST", "/policy", {"id": rid, "src_ip": src_ip, "dst_ip": dst_ip,
                                              "rate_kbps": rate_kbps, "dpids": dpids,
                                              "links": links})

    def modify(self, rid, rate_kbps):
        return self._call("PUT", f"/policy/{rid}", {"rate_kbps": rate_kbps})

    def remove(self, rid):
        return self._call("DELETE", f"/policy/{rid}")

    def clear(self):
        """Remove every policy the controller holds."""
        return self._call("DELETE", "/policy")
