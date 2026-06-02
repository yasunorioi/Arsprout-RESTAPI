#!/usr/bin/env python3
# agriha 中央ブレーン: 時間帯別 目標室温 setpoint スケジューラ
# 各ハウスの schedule から現在の目標室温を算出し agriha/{house}/setpoint/temp に
# publish(retain)。ノード(ccm_rp)はこれを受けて比例＋不感帯で側窓を制御する。
# 依存ゼロ（broker 同梱 mosquitto_pub を subprocess 呼出）。
# 設計: Arsprout-RESTAPI/setpoint-schedule-design.md
#
# setpoint 意味: 「その時間帯にノードが保つべき目標室温℃」＝換気を開始する温度。
#   室温 > setpoint で開き始め、setpoint+band(ノード側) で全開。

import time, json, subprocess, datetime

BROKER, MQTT_PORT = "localhost", 1883
PUBLISH_INTERVAL = 30   # sec（retain なので粗くてよい）
RAMP_MIN = 30           # 区間境界の線形補間（分）。0 で段階切替

# house_id -> [(HH:MM, target_temp_C), ...]（時刻昇順）。要現地調整。
# 夜は高め設定で窓を閉め保温、日中は低めで換気しやすく。
SCHEDULES = {
    2: [("06:00", 22.0), ("10:00", 26.0), ("16:00", 22.0), ("20:00", 20.0)],  # 別棟(.27室温 / .224制御)
}

DAY = 24 * 60

def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def target_now(sched, now_min):
    pts = sorted((to_min(t), v) for t, v in sched)
    n = len(pts)
    # 現区間 = start <= now の最後の点（無ければ前日最後＝夜間）
    idx = n - 1
    for i in range(n):
        if pts[i][0] <= now_min:
            idx = i
    cur_temp = pts[idx][1]
    nxt_start, nxt_temp = pts[(idx + 1) % n]
    to_next = (nxt_start - now_min) % DAY
    if 0 < to_next <= RAMP_MIN and nxt_temp != cur_temp:
        frac = 1 - to_next / RAMP_MIN            # 境界に近づくほど 0→1
        return round(cur_temp + (nxt_temp - cur_temp) * frac, 1)
    return float(cur_temp)

def publish(topic, payload):
    subprocess.run(["mosquitto_pub", "-h", BROKER, "-p", str(MQTT_PORT),
                    "-t", topic, "-m", payload, "-q", "1", "-r"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print("[scheduler] start", flush=True)
last = {}
while True:
    now = datetime.datetime.now()
    nm = now.hour * 60 + now.minute
    for house, sched in SCHEDULES.items():
        t = target_now(sched, nm)
        publish(f"agriha/{house}/setpoint/temp",
                json.dumps({"value": t, "unit": "C", "ts": int(time.time())}))
        if last.get(house) != t:
            last[house] = t
            print(f"  {now:%H:%M} house{house} setpoint={t}C", flush=True)
    time.sleep(PUBLISH_INTERVAL)
