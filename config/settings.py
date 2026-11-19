"""Settings and topology, read from config/config.json (edit that file, not this one).

    h1 h2 h3 h4            h5 h6 h7 h8
      \\  |  |  /             \\  |  |  /
          s1 ===== 10 Mbps ===== s2

Access links are 100 Mbps, the s1-s2 trunk is the 10 Mbps bottleneck.
Links are DIRECTED ("s1->s2" and "s2->s1" have independent capacity), because a
reservation is one-directional: src -> dst.

Set the environment variable SDN_CONFIG to use a different file.
Uses only the standard library, so the Ryu virtual environment can import it too.
"""
import json
import os

_PATH = os.environ.get("SDN_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(_PATH) as _f:
    _CFG = json.load(_f)

_T, _S, _C = _CFG["topology"], _CFG["server"], _CFG["controller"]

ACCESS_MBPS = float(_T["access_mbps"])
TRUNK_MBPS = float(_T["trunk_mbps"])
HOSTS = {h: dict(v) for h, v in _T["hosts"].items()}
SWITCH_DPID = dict(_T["switches"])
TRUNK = tuple(_T["trunk"])

SERVER_HOST = _S["host"]
SERVER_PORT = int(_S["port"])
IDLE_TIMEOUT_S = float(_S["idle_timeout_s"])
MAX_LINE = int(_S["max_line_bytes"])

CONTROLLER_URL = _C["rest_url"]
OPENFLOW_PORT = int(_C["openflow_port"])


def capacities():
    """Capacity (Mbps) of every directed link."""
    caps = {}
    for h, v in HOSTS.items():
        caps[f"{h}->{v['switch']}"] = ACCESS_MBPS
        caps[f"{v['switch']}->{h}"] = ACCESS_MBPS
    a, b = TRUNK
    caps[f"{a}->{b}"] = TRUNK_MBPS
    caps[f"{b}->{a}"] = TRUNK_MBPS
    return caps


def path(src, dst):
    """Static path: returns (directed links, switches traversed)."""
    s_sw, d_sw = HOSTS[src]["switch"], HOSTS[dst]["switch"]
    links, switches = [f"{src}->{s_sw}"], [s_sw]
    if s_sw != d_sw:
        links.append(f"{s_sw}->{d_sw}")
        switches.append(d_sw)
    links.append(f"{d_sw}->{dst}")
    return links, switches
