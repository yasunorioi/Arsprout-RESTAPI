#!/usr/bin/env python3
# agriha CCM -> MQTT ブリッジ（一方向・ArSprout 専用）
# UECS-CCM (UDP マルチキャスト 224.0.0.1:16520) を受信し、agriha MQTT 体系へ republish。
# ArSprout 廃止までの移行期に、ArSprout 系 CCM センサ(.71気象/.70本棟/.80新棟)を
# agriha トピックで供給し、ccm_rp 等の MQTT ノードへ橋渡しする。
# agri-* 自作ノードは 2026-06-07 以降 agriha MQTT を直接 publish する（CCM 撤去済）
# ため、ここでは扱わない。
#
# 設計参照: arsprout-analysis/skills/uecs-mqtt-bridge-generator.md（自作Node-RED版の後継）、
#           Arsprout-RESTAPI/mqtt-topics.md（§0 命名規約・§2.5 farm weather）。
# 依存: なし（broker 同梱の mosquitto_pub を subprocess 呼び出し。paho/pip 不要）。
#
# 現地マッピング: room=1 固定 + region で場所区別。下表は要現地調整。

import socket, struct, re, time, json, subprocess

MCAST, PORT = "224.0.0.1", 16520
BROKER, MQTT_PORT = "localhost", 1883

# (room, region) -> (scope, category)。scope=house_id(int) または "farm"。
SCOPE_MAP = {
    # (1,13) 別棟 .27 agri-env は撤去済み: v0.4.0 から agriha/2/sensor/* を直接 publish
    (1, 41): ("farm", "weather"),  # 屋外気象 .71 → farm 共有
    (1, 11): (1, "sensor"),        # 本棟旧 .70 → house 1
    (1, 12): (3, "sensor"),        # 新棟 .80 → house 3
    # アクチュエータ/制御(region 61/62)は当面ブリッジしない（必要なら追加）
}

# type -> unit（agriha §0.4 の {value,unit,ts}）。無い型は ""。
UNIT = {
    "InAirTemp": "C", "InAirHumid": "%", "InAirCO2": "ppm", "InAirPressure": "hPa",
    "InAirAbsHumid": "g m-3", "InAirDP": "C", "InAirHD": "kPa", "InRadiation": "kW m-2",
    "IntgRadiation": "MJ m-2", "Pulse": "", "SoilTemp": "C", "SoilWC": "%", "SoilEC": "dS m-1",
    "WAirTemp": "C", "WAirHumid": "%", "WWindSpeed": "m s-1", "WWindDir16": "16dir",
    "WRainfallAmt": "mm", "WRadiation": "kW m-2", "WRadInteg": "MJ m-2",
}

DATA_RE = re.compile(
    r'<DATA\s+type="([^"]*)"'
    r'(?:[^>]*?\broom="([^"]*)")?'
    r'(?:[^>]*?\bregion="([^"]*)")?'
    r'(?:[^>]*?\border="([^"]*)")?'
    r'[^>]*>([^<]*)</DATA>', re.I)

# センサー値でない型は除外（cnd=ノード生存通知 等）
SKIP_TYPES = {"cnd"}

def strip_suffix(t):
    for s in (".cMC", ".mC", ".MC"):
        if t.endswith(s):
            return t[:-len(s)]
    return t

def publish(topic, payload):
    # broker 同梱 mosquitto_pub を使用（依存追加なし）。QoS1・retain。
    subprocess.run(["mosquitto_pub", "-h", BROKER, "-p", str(MQTT_PORT),
                    "-t", topic, "-m", payload, "-q", "1", "-r"],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("", PORT))
mreq = struct.pack("4sl", socket.inet_aton(MCAST), socket.INADDR_ANY)
try:
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
except OSError as e:
    print(f"[bridge] join warn: {e} (all-hosts は bind のみで届くことが多い)", flush=True)

print(f"[bridge] CCM {MCAST}:{PORT} -> MQTT {BROKER}:{MQTT_PORT} (via mosquitto_pub)", flush=True)
seen = {}
while True:
    data, addr = sock.recvfrom(8192)
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        continue
    for m in DATA_RE.finditer(text):
        typ, room, region, order, val = m.groups()
        typ = strip_suffix(typ)
        if typ in SKIP_TYPES:
            continue
        try:
            key = (int(room or 0), int(region or 0))
        except ValueError:
            continue
        if key not in SCOPE_MAP:
            continue
        scope, cat = SCOPE_MAP[key]
        topic = f"agriha/{scope}/{cat}/{typ}"
        try:
            value = float(val)
        except ValueError:
            value = val.strip()
        publish(topic, json.dumps({"value": value, "unit": UNIT.get(typ, ""), "ts": int(time.time())}))
        if seen.get(topic) != value:           # 値変化のみログ
            seen[topic] = value
            print(f"  {addr[0]:<15} {topic} = {value}", flush=True)
