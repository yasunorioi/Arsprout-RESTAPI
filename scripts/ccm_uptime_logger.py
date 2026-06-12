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
#
# uptime に加えて relay_state(8ch ビットマスク int, 0=全OFF)も同じ /api/state から
# 取って publish する:
#   agriha/<scope>/sys/<node>/relay_state = {"value":<int>,"unit":"","ts":<epoch>} (retain)
# これで「リセット直前にリレーが通電(駆動)していたか」が history に co-sample で残り、
# 駆動ブラウンアウト(relay_state≠0 で落ちる) か 純電源マージン不足(idle=0 で落ちる) を
# 後から客観判定できる。ノード側 MQTT publish の生死に依存しない(HTTP 経路)。
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
last = {}        # node -> 直近 uptime
last_rs = {}     # node -> 直近 relay_state(駆動判定用)
while True:
    for node, ip in NODES.items():
        ts = int(time.time())
        topic = f"agriha/{SCOPE}/sys/{node}/uptime"
        rs_topic = f"agriha/{SCOPE}/sys/{node}/relay_state"
        try:
            d = json.load(urllib.request.urlopen(f"http://{ip}/api/state", timeout=5))
            up = int(d.get("uptime", -1))
            rs = int(d.get("relay_state", -1))
        except Exception:
            up = None
            rs = None
        if up is None or up < 0:
            # 到達不能 = 落ちてる/再起動中。null を publish（系列に欠損として残る）
            pub(topic, json.dumps({"value": None, "unit": "s", "ts": ts}))
            pub(rs_topic, json.dumps({"value": None, "unit": "", "ts": ts}))
            print(f"  {node} unreachable", flush=True)
            last.pop(node, None)
            last_rs.pop(node, None)
            continue
        pub(topic, json.dumps({"value": up, "unit": "s", "ts": ts}))
        if rs is not None and rs >= 0:
            pub(rs_topic, json.dumps({"value": rs, "unit": "", "ts": ts}))
        prev = last.get(node)
        if prev is not None and up < prev:
            # リセット直前(=今回ドロップ前に最後に観測した relay_state)で駆動していたか
            prev_rs = last_rs.get(node)
            if prev_rs is None or prev_rs < 0:
                drive = "直前リレー不明"
            elif prev_rs == 0:
                drive = "直前リレー=0(アイドル→純電源マージン疑い)"
            else:
                drive = f"直前 relay_state=0x{prev_rs:02X}(駆動中→突入ブラウンアウト疑い)"
            print(f"  [REBOOT] {node} uptime {prev}s -> {up}s "
                  f"(ブラウンアウト/再起動の可能性) {drive}", flush=True)
        last[node] = up
        if rs is not None and rs >= 0:
            last_rs[node] = rs
    time.sleep(INTERVAL)
