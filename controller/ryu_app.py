"""Ryu (OpenFlow 1.3) controller: holds the capacity view and installs per-reservation policies.

Run:   ryu-manager controller/ryu_app.py          (OpenFlow on :6653, REST on :8080)

* Capacity view: the controller knows every directed link's capacity and how much of it is
  reserved (capacity.CapacityView). It REFUSES a policy that would over-subscribe a link,
  and logs the updated view after every install / modify / remove.

* Default forwarding: every switch gets a table-miss rule "output:NORMAL", so the
  Open vSwitch acts as an ordinary learning switch (no per-packet controller work).
* A reservation = on every switch of the path:
      meter <id>   : one DROP band at the reserved rate
      flow         : match ipv4_src/ipv4_dst  ->  meter <id>, output:NORMAL   (priority 100)
  cookie == meter id == reservation id, so a policy can be removed by cookie.

REST (used by controller_client.HttpController):
    POST   /policy        {"id","src_ip","dst_ip","rate_kbps","dpids":[1,2],"links":[...]}
    PUT    /policy/<id>   {"rate_kbps"}
    DELETE /policy/<id>
    DELETE /policy        remove every policy (the reservation server calls this when it starts)
    GET    /policy
    GET    /capacity      the controller's capacity view (per directed link)
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                    # find capacity.py
sys.path.insert(0, os.path.dirname(_HERE))   # find the config package in the project root

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from webob import Response

from capacity import CapacityView
from config import settings as common

POLICY_PRIORITY = 100
BURST_FRACTION = 0.25      # burst size = 250 ms worth of traffic (kb); TCP needs some burst
MIN_BURST_KB = 64
ETH_IP = 0x0800


class ReservationApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}         # dpid -> datapath
        self.policies = {}    # reservation id -> policy dict
        self.view = CapacityView({l: int(c * 1000) for l, c in common.capacities().items()})
        kwargs["wsgi"].register(PolicyRest, {"app": self})

    # ------------------------------------------------------------ switch handling
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, p = dp.ofproto, dp.ofproto_parser
        self.dps[dp.id] = dp
        self._add_flow(dp, 0, p.OFPMatch(), [p.OFPActionOutput(ofp.OFPP_NORMAL)])
        self.logger.info("switch %s connected", dp.id)

    def _add_flow(self, dp, priority, match, actions, meter_id=None, cookie=0):
        ofp, p = dp.ofproto, dp.ofproto_parser
        inst = []
        if meter_id is not None:                       # meter instruction must come first
            inst.append(p.OFPInstructionMeter(meter_id))
        inst.append(p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        dp.send_msg(p.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority,
                                 match=match, instructions=inst))

    def _meter(self, dp, command, mid, rate_kbps=0):
        ofp, p = dp.ofproto, dp.ofproto_parser
        if command == ofp.OFPMC_DELETE:
            dp.send_msg(p.OFPMeterMod(datapath=dp, command=command, flags=ofp.OFPMF_KBPS,
                                      meter_id=mid))
            return
        burst = max(int(rate_kbps * BURST_FRACTION), MIN_BURST_KB)
        bands = [p.OFPMeterBandDrop(rate=rate_kbps, burst_size=burst)]
        dp.send_msg(p.OFPMeterMod(datapath=dp, command=command,
                                  flags=ofp.OFPMF_KBPS | ofp.OFPMF_BURST,
                                  meter_id=mid, bands=bands))

    # ------------------------------------------------------------ policy operations
    def install(self, pol):
        rid, dpids = int(pol["id"]), [int(d) for d in pol["dpids"]]
        missing = [d for d in dpids if d not in self.dps]
        if missing:
            raise ValueError("switches not connected: %s" % missing)
        self.view.add(rid, pol["links"], int(pol["rate_kbps"]))     # raises if over capacity
        for d in dpids:
            dp = self.dps[d]
            ofp, p = dp.ofproto, dp.ofproto_parser
            self._meter(dp, ofp.OFPMC_ADD, rid, int(pol["rate_kbps"]))
            match = p.OFPMatch(eth_type=ETH_IP, ipv4_src=pol["src_ip"], ipv4_dst=pol["dst_ip"])
            self._add_flow(dp, POLICY_PRIORITY, match, [p.OFPActionOutput(ofp.OFPP_NORMAL)],
                           meter_id=rid, cookie=rid)
        self.policies[rid] = {**pol, "id": rid, "dpids": dpids}
        self.logger.info("policy %d installed: %s -> %s @ %s kbps on %s", rid, pol["src_ip"],
                         pol["dst_ip"], pol["rate_kbps"], dpids)
        self.logger.info("capacity view: %s", self.view.summary())

    def modify(self, rid, rate_kbps):
        pol = self.policies.get(rid)
        if pol is None:
            raise KeyError("unknown policy %d" % rid)
        self.view.change(rid, rate_kbps)                            # raises if over capacity
        for d in pol["dpids"]:
            dp = self.dps[d]
            self._meter(dp, dp.ofproto.OFPMC_MODIFY, rid, rate_kbps)
        pol["rate_kbps"] = rate_kbps
        self.logger.info("policy %d modified -> %d kbps", rid, rate_kbps)
        self.logger.info("capacity view: %s", self.view.summary())

    def remove(self, rid):
        pol = self.policies.pop(rid, None)
        if pol is None:
            return                                      # idempotent delete
        self.view.remove(rid)
        for d in pol["dpids"]:
            dp = self.dps.get(d)
            if dp is None:
                continue
            ofp, p = dp.ofproto, dp.ofproto_parser
            dp.send_msg(p.OFPFlowMod(datapath=dp, cookie=rid, cookie_mask=0xFFFFFFFFFFFFFFFF,
                                     table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                     out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                     match=p.OFPMatch()))
            self._meter(dp, ofp.OFPMC_DELETE, rid)
        self.logger.info("policy %d removed", rid)
        self.logger.info("capacity view: %s", self.view.summary())


    def clear(self):
        """Remove every policy (drops leftovers from an earlier, interrupted run)."""
        for rid in list(self.policies):
            self.remove(rid)


class PolicyRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data["app"]

    @staticmethod
    def _reply(status, obj):
        return Response(status=status, content_type="application/json",
                        body=json.dumps(obj).encode())

    @route("policy_clear", "/policy", methods=["DELETE"])
    def clear_policies(self, req, **kwargs):
        try:
            self.app.clear()
        except Exception as exc:
            return self._reply(400, {"error": str(exc)})
        return self._reply(200, {"ok": True})

    @route("capacity", "/capacity", methods=["GET"])
    def capacity(self, req, **kwargs):
        return self._reply(200, self.app.view.snapshot())

    @route("policy_list", "/policy", methods=["GET"])
    def list_policies(self, req, **kwargs):
        return self._reply(200, list(self.app.policies.values()))

    @route("policy_add", "/policy", methods=["POST"])
    def add_policy(self, req, **kwargs):
        try:
            self.app.install(json.loads(req.body.decode()))
        except Exception as exc:
            return self._reply(400, {"error": str(exc)})
        return self._reply(200, {"ok": True})

    @route("policy_mod", "/policy/{rid}", methods=["PUT"], requirements={"rid": r"\d+"})
    def modify_policy(self, req, rid, **kwargs):
        try:
            self.app.modify(int(rid), int(json.loads(req.body.decode())["rate_kbps"]))
        except Exception as exc:
            return self._reply(400, {"error": str(exc)})
        return self._reply(200, {"ok": True})

    @route("policy_del", "/policy/{rid}", methods=["DELETE"], requirements={"rid": r"\d+"})
    def delete_policy(self, req, rid, **kwargs):
        try:
            self.app.remove(int(rid))
        except Exception as exc:
            return self._reply(400, {"error": str(exc)})
        return self._reply(200, {"ok": True})
