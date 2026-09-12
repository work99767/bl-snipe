#!/usr/bin/env python3
"""Precision sniper v2 for Xiaomi BL unlock quota (Beijing midnight, window ~50-150ms).

v2 fixes vs v1:
- TLS/TCP connection pre-warmed at T-15s (idle conns die in minutes, handshake costs ~3 RTT)
- RTT calibrated on the WARM connection right before the shot (not hours early)
- NTP time = median offset of 4 samples
- Account Error / <30d guard: do not waste a shot during cooldown
- Triple tap arrivals: ~ +40 / +100 / +160 ms after 00:00:00.000 GMT+8

Env: DRY=1 -> print plan only. WARM_WINDOW=seconds (default 15).
"""
import os, sys, time, json, statistics, ntplib, requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from micommunity import get_headers, STATE_URL, APPLY_URL, state

DRY = os.environ.get("DRY") == "1"
TEST_TARGET_S = float(os.environ["TEST_TARGET_S"]) if os.environ.get("TEST_TARGET_S") else None
WARM_AT = float(os.environ.get("WARM_WINDOW", "15"))  # seconds before midnight
BEIJING = timezone(timedelta(hours=8))
NTP_HOSTS = ("ntp1.aliyun.com", "ntp.aliyun.com", "pool.ntp.org", "time.cloudflare.com")

def ntp_offsets(n=4):
    offs, refs = [], []
    for _ in range(n):
        for h in NTP_HOSTS:
            try:
                c = ntplib.NTPClient()
                m = time.monotonic()
                r = c.request(h, version=3, timeout=4)
                # offset of local clock vs server: (t1-t0+t2-t3)/2 style -> use r.offset
                offs.append(r.offset)
                refs.append((r.dest_time + r.offset, time.monotonic()))  # corrected abs time
                break
            except Exception:
                continue
        time.sleep(0.2)
    if not offs:
        raise SystemExit("all NTP servers unreachable")
    med = statistics.median(offs)
    # pick the reference pair whose offset is closest to median
    best = min(zip(offs, refs), key=lambda x: abs(x[0] - med))
    return best[1]  # (true_time_at_mono, mono)

print("[1/5] auth + state", flush=True)
headers = get_headers(silent=True)
s = requests.Session()
r = s.get(STATE_URL, headers=headers, timeout=15)
st = state(r)
print("state:", st.get("code"), st.get("message"), flush=True)
if st.get("code") == 1:
    print("ALREADY GRANTED - exiting", flush=True)
    sys.exit(0)
if st.get("code") == 3:
    print("ACCOUNT ERROR cooldown - DO NOT fire today, exiting", flush=True)
    sys.exit(0)

true0, mono0 = ntp_offsets()
now_bj = datetime.fromtimestamp(true0, timezone.utc).astimezone(BEIJING)
if TEST_TARGET_S:
    target_ts = true0 + TEST_TARGET_S
    target = datetime.fromtimestamp(target_ts, BEIJING)
else:
    target = (now_bj + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_ts = target.timestamp()
print(f"[2/5] now(BJ)={now_bj.strftime('%H:%M:%S')} target={target.strftime('%m-%d %H:%M:%S')} GMT+8", flush=True)

def true_now():
    return true0 + (time.monotonic() - mono0)

def wait_until(ts, resync_margin=120):
    global true0, mono0
    resynced = False
    while True:
        d = ts - true_now()
        if d <= 0:
            return
        # re-sync only in a comfortable band; NEVER in the final 20s (a sync costs 1-2s)
        if not resynced and 20 < d < resync_margin:
            true0, mono0 = ntp_offsets()
            resynced = True
            print("      (NTP re-sync)", flush=True)
            continue
        if d > 5:
            time.sleep(min(d - 5, 30))
        elif d > 0.5:
            time.sleep(0.02)

ARRIVAL = 0.04

if DRY:
    # rough estimate for the plan print only
    t0 = time.monotonic()
    try:
        s.get(STATE_URL, headers=headers, timeout=5)
        est_rtt = time.monotonic() - t0
    except Exception:
        est_rtt = 0.2
    print(f"[DRY] est_rtt(1st, cold)={est_rtt*1000:.0f}ms; warm-conn oneway would be measured at T-{WARM_AT:.0f}s")
    print(f"[DRY] taps arrive ~ +40 / +100 / +160 ms after 00:00:00.000 GMT+8")
    sys.exit(0)

plan = {"arrival": ARRIVAL}

print(f"[3/5] sleeping until T-{WARM_AT:.0f}s", flush=True)
wait_until(target_ts - WARM_AT)

# --- warm phase: establish TLS + measure real request latency on THIS connection ---
def get_min_rtt(n=3):
    lat = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            s.get(STATE_URL, headers=headers, timeout=5)
        except Exception:
            pass
        lat.append(time.monotonic() - t0)
        time.sleep(0.25)
    return min(lat)

warm_rtt = get_min_rtt()
oneway = warm_rtt / 2.0
send_off = ARRIVAL - oneway
if send_off < -0.30:
    send_off = -0.30
print(f"[4/5] warm_rtt={warm_rtt*1000:.0f}ms oneway~{oneway*1000:.0f}ms -> send at {send_off*1000:+.0f}ms", flush=True)

if DRY:
    print(f"[DRY] taps would arrive ~ +{int((send_off+oneway)*1000)} / +{int((send_off+oneway)*1000)+60} / +{int((send_off+oneway)*1000)+120} ms")
    sys.exit(0)

# keep-alive touch at T-4s
wait_until(target_ts - 4)
s.get(STATE_URL, headers=headers, timeout=5)

# precise arm
wait_until(target_ts + send_off)

def tap(is_retry):
    t = time.monotonic()
    try:
        if TEST_TARGET_S:
            rr = s.get(STATE_URL, headers=headers, timeout=10)
            return f"lat={(time.monotonic()-t)*1000:.0f}ms http={rr.status_code} {rr.text[:120]}"
        rr = s.post(APPLY_URL, headers=headers, json={"is_retry": bool(is_retry)}, timeout=10)
        return f"lat={(time.monotonic()-t)*1000:.0f}ms http={rr.status_code} {rr.text[:300]}"
    except Exception as e:
        return f"lat={(time.monotonic()-t)*1000:.0f}ms ERR={e}"

if TEST_TARGET_S:
    # skip the 90s post-shot wait in synthetic tests
    def _noop(*a): pass

print("[5/5] FIRE", flush=True)
print("TAP1", tap(False), flush=True)
time.sleep(0.06)
print("TAP2", tap(True), flush=True)
time.sleep(0.06)
print("TAP3", tap(True), flush=True)

# post-shot state peek (2 min later catches most silent approvals early)
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
