#!/usr/bin/env python3
"""Precision sniper for Xiaomi BL unlock quota (Beijing midnight window 50-150ms).
Strategy from community research: RTT-calibrated arrival + triple tap.
Env: DRY=1 -> compute and print plan, do not wait/fire.
"""
import os, sys, time, json, ntplib, requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from micommunity import get_headers, STATE_URL, APPLY_URL, state

DRY = os.environ.get("DRY") == "1"
BEIJING = timezone(timedelta(hours=8))
NTP_HOSTS = ("ntp1.aliyun.com", "ntp.aliyun.com", "pool.ntp.org", "time.cloudflare.com")

def ntp_now():
    for h in NTP_HOSTS:
        try:
            return ntplib.NTPClient().request(h, version=3, timeout=5).tx_time
        except Exception:
            continue
    raise SystemExit("all NTP servers unreachable")

print("[1/5] auth + state check", flush=True)
headers = get_headers(silent=True)
s = requests.Session()
r = s.get(STATE_URL, headers=headers, timeout=15)
st = state(r)
print("state:", st.get("code"), st.get("message"), flush=True)
if st.get("code") == 1:
    print("ALREADY GRANTED - nothing to do", flush=True)
    sys.exit(0)

# next Beijing midnight
now_bj = datetime.fromtimestamp(ntp_now(), timezone.utc).astimezone(BEIJING)
target = (now_bj + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
target_ts = target.timestamp()
print(f"[2/5] now(BJ)={now_bj.strftime('%H:%M:%S')} target={target.strftime('%Y-%m-%d %H:%M:%S')} GMT+8", flush=True)

print("[3/5] RTT calibration to sgp-api.buy.mi.com", flush=True)
rtts = []
for _ in range(6):
    t0 = time.monotonic()
    try:
        s.get(STATE_URL, headers=headers, timeout=5)
    except Exception:
        pass
    rtts.append(time.monotonic() - t0)
    time.sleep(0.35)
rtt = min(rtts)
oneway = rtt / 2.0
print(f"rtt_min={rtt*1000:.1f}ms oneway~{oneway*1000:.1f}ms", flush=True)

ARRIVAL = 0.04          # aim to land ~40ms after window opens
send_off = max(ARRIVAL - oneway, -0.30)   # never send earlier than 300ms before midnight

if DRY:
    print(f"[DRY] would send TAP1(is_retry=False) at 00:00:00{send_off:+.3f}s, TAP2 at +90ms, TAP3 at +180ms")
    print(f"[DRY] expected arrival of TAP1: ~{(send_off+oneway)*1000:+.0f}ms after 00:00:00.000")
    sys.exit(0)

print(f"[4/5] arming: send offset {send_off*1000:+.0f}ms rel. midnight", flush=True)

ntp_ref, mono_ref = ntp_now(), time.monotonic()
resynced = False
while True:
    now_ts = ntp_ref + (time.monotonic() - mono_ref)
    d = target_ts + send_off - now_ts
    if d <= 0:
        break
    if not resynced and d < 120:
        ntp_ref, mono_ref = ntp_now(), time.monotonic()
        resynced = True
        print("      (re-synced clock at T-2min)", flush=True)
        continue
    if d > 5:
        time.sleep(min(d - 5, 30))
    elif d > 0.5:
        time.sleep(0.02)

def tap(is_retry):
    t = time.monotonic()
    try:
        rr = s.post(APPLY_URL, headers=headers, json={"is_retry": bool(is_retry)}, timeout=10)
        return f"@{(time.monotonic()-t)*1000:.0f}ms http={rr.status_code} body={rr.text[:300]}"
    except Exception as e:
        return f"@{(time.monotonic()-t)*1000:.0f}ms ERR={e}"

print("[5/5] FIRE", flush=True)
print("TAP1", tap(False), flush=True)
time.sleep(0.06)
print("TAP2", tap(True), flush=True)
time.sleep(0.06)
print("TAP3", tap(True), flush=True)
print("DONE check result.txt later (silent approval possible)", flush=True)
