#!/usr/bin/env python3
# ccm_uptime_logger.py — ccm_rp2350_relay 系ノードの uptime を定期取得して
# agriha MQTT へ publish する。reboot/ブラウンアウト監視用。
#
# 各ノードの HTTP /api/state から uptime(秒) を取り、
#   agriha/<scope>/sys/<node>/uptime  = {"value":<秒>,"unit":"s","ts":<epoch>}  (retain)
# を publish。pi4 の agriha_logger が history DB に系列として記録するので、
# /history でグラフ化できる。uptime が落ちる(sawtooth)＝reboot 発生。
# 依存ゼロ（stdlib urllib + broker 同梱 mosquitto_pub を subprocess 呼出）。
#
# モーター突入のブラウンアウトで基板がリブートすると uptime が 0 付近に戻るので、
# このグラフで「窓駆動のたびに落ちてるか／頻度」が見える。電源対策(電源分離/NTC/
# バルクコン)の要否判断に使う。
import json, time, subprocess, urllib.request

# 監視対象（node名 -> IP）。増やす時はここに追記。
NODES = {
    "uecs-ccm-02": "192.168.1.86",
    "uecs-ccm-03": "192.168.1.224",
}
SCOPE    = "h01"        # topic: agriha/<SCOPE>/sys/<node>/uptime
INTERVAL = 30           # 秒
BROKER   = "localhost"
MQTT_PORT = 1883


def pub(topic, payload):
    subprocess.run(["mosquitto_pub", "-h", BROKER, "-p", str(MQTT_PORT),
                    "-t", topic, "-m", payload, "-q", "1", "-r"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


print(f"[ccm-uptime] polling {list(NODES)} every {INTERVAL}s", flush=True)
last = {}
while True:
    for node, ip in NODES.items():
        ts = int(time.time())
        topic = f"agriha/{SCOPE}/sys/{node}/uptime"
        try:
            d = json.load(urllib.request.urlopen(f"http://{ip}/api/state", timeout=5))
            up = int(d.get("uptime", -1))
        except Exception:
            up = None
        if up is None or up < 0:
            # 到達不能 = 落ちてる/再起動中。null を publish（系列に欠損として残る）
            pub(topic, json.dumps({"value": None, "unit": "s", "ts": ts}))
            print(f"  {node} unreachable", flush=True)
            last.pop(node, None)
            continue
        pub(topic, json.dumps({"value": up, "unit": "s", "ts": ts}))
        prev = last.get(node)
        if prev is not None and up < prev:
            print(f"  [REBOOT] {node} uptime {prev}s -> {up}s "
                  f"(ブラウンアウト/再起動の可能性)", flush=True)
        last[node] = up
    time.sleep(INTERVAL)
