#!/usr/bin/env python3
# agriha 中央ブレーン: 時間帯別 目標室温 setpoint スケジューラ
# 各ハウスの schedule から現在の目標室温を算出し agriha/{house}/setpoint/temp に
# publish(retain)。ノード(ccm_rp)はこれを受けて比例＋不感帯で側窓を制御する。
# 依存ゼロ（broker 同梱 mosquitto_pub を subprocess 呼出）。
# 設計: Arsprout-RESTAPI/setpoint-schedule-design.md
#
# schedule は /home/pi/agriha_schedule.json に外出し（agriha_web.py で編集）。
# ファイル mtime 変化で自動リロード（再起動不要）。

import time, json, subprocess, datetime, os

BROKER, MQTT_PORT = "localhost", 1883
PUBLISH_INTERVAL = 30   # sec（retain なので粗くてよい）
RAMP_MIN = 30           # 区間境界の線形補間（分）。0 で段階切替
SCHED_FILE = "/home/pi/agriha_schedule.json"

# 設定ファイルが無い時の既定（house_id文字列 -> [[HH:MM, temp], ...]）
DEFAULT_SCHEDULES = {"2": [["06:00", 22.0], ["10:00", 26.0], ["16:00", 22.0], ["20:00", 20.0]]}

DAY = 24 * 60
_mtime = None
_schedules = {}

def load_schedules():
    global _mtime, _schedules
    try:
        m = os.path.getmtime(SCHED_FILE)
        if m != _mtime:
            with open(SCHED_FILE) as f:
                _schedules = json.load(f)
            _mtime = m
            print(f"[scheduler] loaded {SCHED_FILE}: houses={list(_schedules)}", flush=True)
    except FileNotFoundError:
        if not _schedules:
            _schedules = DEFAULT_SCHEDULES
            print("[scheduler] no config file, using default", flush=True)
    except Exception as e:
        print(f"[scheduler] config error: {e} (keeping previous)", flush=True)
    return _schedules

def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def target_now(sched, now_min):
    pts = sorted((to_min(t), float(v)) for t, v in sched)
    n = len(pts)
    idx = n - 1                       # 現区間 = start<=now の最後（無ければ前日最後＝夜間）
    for i in range(n):
        if pts[i][0] <= now_min:
            idx = i
    cur_temp = pts[idx][1]
    nxt_start, nxt_temp = pts[(idx + 1) % n]
    to_next = (nxt_start - now_min) % DAY
    if 0 < to_next <= RAMP_MIN and nxt_temp != cur_temp:
        frac = 1 - to_next / RAMP_MIN
        return round(cur_temp + (nxt_temp - cur_temp) * frac, 1)
    return float(cur_temp)

def publish(topic, payload):
    subprocess.run(["mosquitto_pub", "-h", BROKER, "-p", str(MQTT_PORT),
                    "-t", topic, "-m", payload, "-q", "1", "-r"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print("[scheduler] start", flush=True)
last = {}
while True:
    scheds = load_schedules()
    now = datetime.datetime.now()
    nm = now.hour * 60 + now.minute
    for house, sched in scheds.items():
        if not sched:
            continue
        try:
            t = target_now(sched, nm)
        except Exception as e:
            print(f"[scheduler] house{house} bad schedule: {e}", flush=True)
            continue
        publish(f"agriha/{house}/setpoint/temp",
                json.dumps({"value": t, "unit": "C", "ts": int(time.time())}))
        if last.get(house) != t:
            last[house] = t
            print(f"  {now:%H:%M} house{house} setpoint={t}C", flush=True)
    time.sleep(PUBLISH_INTERVAL)
