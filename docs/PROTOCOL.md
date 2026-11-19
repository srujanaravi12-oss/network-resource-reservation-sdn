# Reservation protocol (v1)

Transport: TCP, default port 9000. One request per line, one reply per line, UTF-8, max 4096 bytes per line.

    request:  VERB <json object>\n
    reply:    STATUS <json object>\n

A reservation is a lease tied to its duration, not to the TCP connection: a client that disappears without
RELEASE loses its reservation automatically when the duration ends.

A connection may carry any number of requests. Malformed input gets a `REJECT` and the connection stays open
(except an over-long line, which closes it). Idle connections are closed after 60 s.

## Request fields
| field | type | meaning |
|---|---|---|
| `src`, `dst` | string | host names `h1`..`h8`, must differ |
| `bw_mbps` | number | 0 < bw ≤ 1000 |
| `duration_s` | number | 0 < duration ≤ 86400 (lease length) |
| `id` | integer | reservation id returned by ACCEPT |

## Messages
| Request | Success reply | Failure reply |
|---|---|---|
| `RESERVE {"src","dst","bw_mbps","duration_s"}` | `ACCEPT {"id","expires_in_s","path","policy_delay_ms"}` | `REJECT {"reason",...}` |
| `RELEASE {"id"}` | `OK {"id"}` | `REJECT {"reason"}` |
| `MODIFY {"id"[,"bw_mbps"][,"duration_s"]}` (new duration counts from now) | `OK {"id","bw_mbps","remaining_s"}` | `REJECT {"reason"}` |
| `STATUS {}` (all links + reservations + counters) or `STATUS {"id"}` | `OK {...}` | `REJECT {"reason":"UNKNOWN_ID"}` |

## Reject reasons
| reason | meaning |
|---|---|
| `BAD_REQUEST` | missing/invalid field, bad JSON, unknown verb (`detail` explains) |
| `UNKNOWN_HOST` | src/dst not in the topology |
| `INSUFFICIENT_BANDWIDTH` | some link on the path lacks capacity; reply names `link` and `available_mbps` |
| `CONFLICT` | a live reservation for the same src→dst exists (`existing_id`) |
| `CONTROLLER_ERROR` | policy could not be installed/changed/removed; nothing was changed (or retried) |
| `UNKNOWN_ID` | no such live reservation |
| `BUSY` | reservation is being modified/removed; retry |

## Lifecycle
```
RESERVE ──(validation/admission fails)──> REJECTED (not stored, counted)
   └─(admitted)─> PENDING ─(policy installed)─> ACTIVE ─┬─ lease ends ───> REMOVING ─> EXPIRED
                      └─(install failed)─> rolled back  └─ RELEASE ──────> REMOVING ─> RELEASED
```
Capacity is reserved from PENDING onward and freed only after the policy is removed from the switches.

## Example
```
> RESERVE {"src":"h1","dst":"h5","bw_mbps":4,"duration_s":30}
< ACCEPT {"id": 1, "expires_in_s": 30.0, "path": ["h1->s1", "s1->s2", "s2->h5"], "policy_delay_ms": 12.4}
> RESERVE {"src":"h2","dst":"h6","bw_mbps":20,"duration_s":30}
< REJECT {"reason": "INSUFFICIENT_BANDWIDTH", "link": "s1->s2", "requested_mbps": 20.0, "available_mbps": 6.0}
```
(The access links have 100 Mbps; it is the 10 Mbps trunk `s1->s2` that fails, and 4 Mbps of it is already reserved.)
