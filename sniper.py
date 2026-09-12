#!/usr/bin/env python3
"""Precision sniper v3 for Xiaomi BL unlock quota (Beijing midnight, window ~50-150ms).

v3 additions:
- SERVER-CLOCK sync via ts-second rollover detection (aligns to Xiaomi's own clock, not NTP)
- TCP_NODELAY (Nagle off) on all sockets
- Continuous keep-warm touches every 3s in the final phase + dual-socket failover
- NTP: 6-sample median, cloudflare-first ordering
- ARRIVAL_MS env to tune arrival target without code edits
Env: DRY=1 (plan only), TEST_TARGET_S=<sec> (synthetic target, taps -> safe GET),
     ARRIVAL_MS (default 40), WARM_WINDOW (default 15).
"""
import os, sys, time, json, socket, statistics, ntplib, requests
from datetime import datetime, timezone, timedelta
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from micommunity import get_headers, STATE_URL, APPLY_URL, state

DRY = os.environ.get("DRY") == "1"
TAP_OFFSETS = [float(x)/1000.0 for x in os.environ.get("TAP_OFFSETS", "-1200,-150,40").split(",")]
TEST_TARGET_S = float(os.environ["TEST_TARGET_S"]) if os.environ.get("TEST_TARGET_S") else None
WARM_AT = float(os.environ.get("WARM_WINDOW", "15"))
ARRIVAL = float(os.environ.get("ARRIVAL_MS", "40")) / 1000.0
BEIJING = timezone(timedelta(hours=8))
NTP_HOSTS = ("time.cloudflare.com", "ntp1.aliyun.com", "ntp.aliyun.com", "pool.ntp.org")

class NoDelayAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        opts = kw.setdefault("socket_options", [])
        opts.append((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
        return super().init_poolmanager(*a, **kw)

def new_session():
    s = requests.Session()
    s.mount("https://", NoDelayAdapter())
    return s

def ntp_ref(n=6):
    offs, refs = [], []
    for _ in range(n):
        for h in NTP_HOSTS:
            try:
                r = ntplib.NTPClient().request(h, version=3, timeout=4)
                offs.append(r.offset)
                refs.append((r.dest_time + r.offset, time.monotonic()))
                break
            except Exception:
                continue
        time.sleep(0.15)
    if not offs:
        raise SystemExit("all NTP servers unreachable")
    med = statistics.median(offs)
    best = min(zip(offs, refs), key=lambda x: abs(x[0] - med))
    return best[1]

print("[1/6] auth + state", flush=True)
headers = get_headers(silent=True)
s = new_session()
r = s.get(STATE_URL, headers=headers, timeout=15)
st = state(r)
print("state:", st.get("code"), st.get("message"), flush=True)
if st.get("code") == 1:
    print("ALREADY GRANTED - exiting", flush=True)
    sys.exit(0)
if st.get("code") == 3:
    print("ACCOUNT ERROR cooldown - not firing today", flush=True)
    sys.exit(0)

true0, mono0 = ntp_ref()
def true_now():
    return true0 + (time.monotonic() - mono0)

now_bj = datetime.fromtimestamp(true0, timezone.utc).astimezone(BEIJING)
if TEST_TARGET_S:
    target_ts = true0 + TEST_TARGET_S
else:
    target = (now_bj + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_ts = target.timestamp()
print(f"[2/6] now(BJ)={now_bj.strftime('%H:%M:%S')} target_ts={target_ts:.3f} ({'TEST' if TEST_TARGET_S else 'midnight GMT+8'})", flush=True)

if DRY:
    print(f"[DRY] arrival target +{ARRIVAL*1000:.0f}ms; server-clock sync + warm RTT at T-{WARM_AT:.0f}s")
    sys.exit(0)

def wait_until(ts, band=(20, 120)):
    global true0, mono0
    resynced = False
    while True:
        d = ts - true_now()
        if d <= 0:
            return
        if not resynced and band[0] < d < band[1]:
            true0, mono0 = ntp_ref()
            resynced = True
            print("      (NTP re-sync)", flush=True)
            continue
        if d > 5:
            time.sleep(min(d - 5, 30))
        elif d > 0.5:
            time.sleep(0.02)

# --- server clock sync: catch a ts-second rollover ---
def server_skew(budget=9.0, poll=0.12):
    """Returns (skew, oneway) where local_true_time_of_server_second_boundary = S + skew."""
    t_end = time.monotonic() + budget
    prev_ts, prev_recv, prev_rtt = None, None, None
    while time.monotonic() < t_end:
        sent = time.monotonic()
        try:
            rr = s.get(STATE_URL, headers=headers, timeout=5)
            recv = time.monotonic()
            ts = rr.json().get("ts")
            rtt = recv - sent
        except Exception:
            continue
        if ts is not None and prev_ts is not None and ts == prev_ts + 1:
            oneway = min(rtt, prev_rtt) / 2.0
            boundary_local = prev_recv + (recv - prev_recv) / 2.0  # midpoint between polls
            # refine with oneway of the catching request: server emitted new ts at recv-oneway
            boundary_local2 = recv - oneway
            b = (boundary_local + boundary_local2) / 2.0
            boundary_true = true0 + (b - mono0)  # convert monotonic-domain -> true-time domain
            skew = boundary_true - ts  # true local time when server rolled to integer ts
            return skew, oneway
        prev_ts, prev_recv, prev_rtt = ts, recv, rtt
        time.sleep(poll)
    return None, None

print(f"[3/6] waiting until T-{WARM_AT + 25:.0f}s", flush=True)
wait_until(target_ts - WARM_AT - 25)

print("[4/6] server-clock sync (ts rollover hunt)", flush=True)
skew, srv_oneway = server_skew()
if TEST_TARGET_S:
    skew = 0.0  # synthetic target is in NTP domain, not server domain
    print("      (test mode: skew ignored)", flush=True)
elif skew is None:
    print("      rollover not caught -> NTP-only alignment", flush=True)
    skew = 0.0
else:
    print(f"      server skew={skew*1000:+.0f}ms (server clock vs NTP)", flush=True)
FIRE_TARGET = target_ts + skew  # true-time moment of server's 00:00:00 (or test target)

wait_until(target_ts - WARM_AT)

# --- warm + RTT on the live connection; keep touching every 3s ---
def rtt_probe():
    t0 = time.monotonic()
    try:
        s.get(STATE_URL, headers=headers, timeout=5)
    except Exception:
        pass
    return time.monotonic() - t0

rtts = [rtt_probe() for _ in range(3)]
warm_rtt = min(rtts)
oneway = warm_rtt / 2.0
print(f"[5/6] warm_rtt={warm_rtt*1000:.0f}ms oneway~{oneway*1000:.0f}ms", flush=True)

def clamp(v, lo=-1.5, hi=0.5):
    return max(lo, min(hi, v))
send_off = clamp(TAP_OFFSETS[0] - oneway if TAP_OFFSETS[0] < 0 else TAP_OFFSETS[0] - oneway)
# For queue strategy: offsets are SEND times rel. midnight (arrival = offset + oneway)
offs = [clamp(o - oneway) if o < 0 else clamp(o - oneway) for o in TAP_OFFSETS]
send_off = offs[0]
print(f"      tap sends at {[f'{o*1000:+.0f}' for o in offs]}ms rel. server-midnight (oneway {oneway*1000:.0f}ms)", flush=True)

# keep-alive touches every 3s until T-1.5s; second socket prewarmed as failover
s2 = new_session()
s2.get(STATE_URL, headers=headers, timeout=5)
FIRST_OFF = min(offs)
while True:
    d = (FIRE_TARGET + FIRST_OFF) - true_now()
    if d <= 1.5:
        break
    time.sleep(min(d - 1.5, 3.0))
    try:
        s.get(STATE_URL, headers=headers, timeout=5)
    except Exception:
        pass

def tap(is_retry, sess):
    t = time.monotonic()
    try:
        if TEST_TARGET_S:
            rr = sess.get(STATE_URL, headers=headers, timeout=10)
            return f"lat={(time.monotonic()-t)*1000:.0f}ms http={rr.status_code} ts={rr.json().get('ts')}"
        rr = sess.post(APPLY_URL, headers=headers, json={"is_retry": bool(is_retry)}, timeout=10)
        return f"lat={(time.monotonic()-t)*1000:.0f}ms http={rr.status_code} {rr.text[:300]}"
    except Exception as e:
        return f"lat={(time.monotonic()-t)*1000:.0f}ms ERR={type(e).__name__}"

# final precise arm (busy-wait)
while (FIRE_TARGET + send_off) - true_now() > 0:
    pass

print("[6/6] FIRE", flush=True)
import time as _t
def fire_at(off, is_retry, sess, label):
    # wait for this tap's send time
    while (FIRE_TARGET + off) - true_now() > 0:
        pass
    res = tap(is_retry, sess)
    if "ERR" in res and sess is s:
        res += " | failover-> " + tap(is_retry, s2)
    print(label, res, flush=True)

labels = ["TAP1", "TAP2", "TAP3", "TAP4"]
for i, off in enumerate(offs[:4]):
    fire_at(off, i > 0, s if i % 2 == 0 else s2, labels[i])

if TEST_TARGET_S:
    print("DONE (test mode)", flush=True)
    sys.exit(0)

time.sleep(90)
try:
    r2 = s.get(STATE_URL, headers=headers, timeout=15)
    print("post-shot state:", state(r2).get("code"), state(r2).get("message"), flush=True)
except Exception as e:
    print("post-shot state check failed:", e, flush=True)
print("DONE", flush=True)
