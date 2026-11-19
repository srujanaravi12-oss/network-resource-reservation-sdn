#!/usr/bin/env python3
"""Mininet topology: 8 hosts, 2 switches, one 10 Mbps bottleneck trunk.

    sudo python3 -m topology.topo   # run from the project folder; opens the Mininet CLI (Ryu must be running)
"""
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

from config import settings as common


def build(ctrl_ip="127.0.0.1", ctrl_port=common.OPENFLOW_PORT):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=ctrl_ip, port=ctrl_port)
    switches = {s: net.addSwitch(s, protocols="OpenFlow13") for s in common.SWITCH_DPID}
    for name, info in common.HOSTS.items():
        host = net.addHost(name, ip=info["ip"] + "/8")
        net.addLink(host, switches[info["switch"]], bw=common.ACCESS_MBPS)
    a, b = common.TRUNK
    net.addLink(switches[a], switches[b], bw=common.TRUNK_MBPS, max_queue_size=100)
    return net


if __name__ == "__main__":
    setLogLevel("info")
    network = build()
    network.start()
    CLI(network)
    network.stop()
