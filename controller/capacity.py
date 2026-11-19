"""The controller's capacity view: how much of each directed link is reserved.

Used by ryu_app.py. The controller refuses to install a policy that would push any link
over its capacity, so the data plane can never be over-subscribed even if the reservation
server had a bug. Pure Python (no Ryu) so it can be unit-tested.
"""


class CapacityView:
    def __init__(self, capacities_kbps):
        self.cap = dict(capacities_kbps)
        self.reserved = {l: 0 for l in self.cap}
        self.by_id = {}                       # policy id -> (links, kbps)

    def add(self, rid, links, kbps):
        if rid in self.by_id:
            raise ValueError("policy %s already exists" % rid)
        self._check(links, kbps)
        for l in links:
            self.reserved[l] += kbps
        self.by_id[rid] = (list(links), kbps)

    def change(self, rid, kbps):
        if rid not in self.by_id:
            raise KeyError("unknown policy %s" % rid)
        links, old = self.by_id[rid]
        self._check(links, kbps - old)
        for l in links:
            self.reserved[l] += kbps - old
        self.by_id[rid] = (links, kbps)

    def remove(self, rid):
        entry = self.by_id.pop(rid, None)     # idempotent
        if entry:
            links, kbps = entry
            for l in links:
                self.reserved[l] -= kbps

    def _check(self, links, extra_kbps):
        for l in links:
            if l not in self.cap:
                raise ValueError("unknown link %s" % l)
            if self.reserved[l] + extra_kbps > self.cap[l]:
                raise ValueError("capacity exceeded on %s" % l)

    def snapshot(self):
        return {l: {"capacity_kbps": self.cap[l], "reserved_kbps": self.reserved[l]}
                for l in self.cap}

    def summary(self):
        """One line: only links that carry a reservation."""
        busy = ["%s %.1f/%.1f Mbps" % (l, r / 1000, self.cap[l] / 1000)
                for l, r in sorted(self.reserved.items()) if r]
        return ", ".join(busy) or "no reservations"
