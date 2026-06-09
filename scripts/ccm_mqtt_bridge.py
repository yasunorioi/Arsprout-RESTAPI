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
# --- UECS-CCM 仕様準拠について（既存 ArSprout 環境との混在を壊さないための要点） ---
# CCM のデータ identity は (type, room, region, ORDER) の4要素。order は「同じ
# type/room/region に複数台ある場合の連番」で、これを落とすと別個体が1トピックに
# 潰れて last-writer-wins する。一方 agriha 命名規約(§0.1)は `agriha/<scope>/
# <category>/<type>` ＝「1スコープ1型1トピック」前提。両者を両立させるため:
#   * order=1（既定の主系統）は素のトピック（既存購読 ccm_rp 等との互換維持）
#   * order>=2 は `.../{type}/{order}` を付けて multi-order を保存
#   * 異なる発信元(ip/room/region/order)が同一トピックに書こうとしたら WARN ログ
#     （黙って上書きしない）。
# また region 単独では発信元ハウスを一意に決められない現地癖（例: .80 新棟は
# InAir* を region12、Soil*/Pulse/IntgRadiation を region11 で送る）があるため、
# SENDER_OVERRIDE で発信元IPごとの (room,region)->scope 上書きを用意した。

import socket, struct, re, time, json, subprocess

MCAST, PORT = "224.0.0.1", 16520
BROKER, MQTT_PORT = "localhost", 1883

# (room, region) -> (scope, category)。scope=house_id(int) または "farm"。
# 現地マッピング: room=1 固定 + region で場所区別。下表は要現地調整。
SCOPE_MAP = {
    # (1,13) 別棟 .27 agri-env は撤去済み: v0.4.0 から agriha/2/sensor/* を直接 publish
    (1, 41): ("farm", "weather"),  # 屋外気象 .71 → farm 共有
    (1, 11): (1, "sensor"),        # 本棟旧 .70 → house 1
    (1, 12): (3, "sensor"),        # 新棟 .80 → house 3
    # アクチュエータ/制御(region 61/62)は当面ブリッジしない（必要なら追加）
}

# 発信元IPごとの (room,region)->(scope,category) 上書き（SCOPE_MAP より優先）。
# region 付与の現地癖を吸収する。例: 新棟 .80 は Soil*/Pulse/IntgRadiation を
# region11 で送るが、これは house3(新棟) のデータなので house1(.70) に混ぜない。
SENDER_OVERRIDE = {
    "192.168.1.80": {(1, 11): (3, "sensor")},
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
    r'(?:[^>]*?\bpriority="([^"]*)")?'
    r'[^>]*>([^<]*)</DATA>', re.I)

# センサー値でない型は除外（cnd=ノード生存通知 等）
SKIP_TYPES = {"cnd"}


def strip_suffix(t):
    for s in (".cMC", ".mC", ".MC"):
        if t.endswith(s):
            return t[:-len(s)]
    return t


def resolve_scope(ip, room, region):
    # 発信元IP上書き → 基本 SCOPE_MAP の順で (scope, category) を決める。
    ov = SENDER_OVERRIDE.get(ip)
    if ov and (room, region) in ov:
        return ov[(room, region)]
    return SCOPE_MAP.get((room, region))


def build_topic(scope, cat, typ, order):
    # agriha §0.1 は type 単独（1スコープ1型）。order=1 は素のトピックで既存購読と
    # 互換、order>=2 のみ /{order} を付けて UECS の multi-order を潰さない。
    if order and order != 1:
        return f"agriha/{scope}/{cat}/{typ}/{order}"
    return f"agriha/{scope}/{cat}/{typ}"


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
seen = {}        # topic -> value（値変化のみログ）
owner = {}       # topic -> (ip, room, region, order)（衝突検出）
while True:
    data, addr = sock.recvfrom(8192)
    ip = addr[0]
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        continue
    for m in DATA_RE.finditer(text):
        typ, room, region, order, priority, val = m.groups()
        typ = strip_suffix(typ)
        if typ in SKIP_TYPES:
            continue
        try:
            room_i = int(room or 0)
            region_i = int(region or 0)
            order_i = int(order or 1)   # UECS order は本来必須。欠落時は主系統=1 扱い
        except ValueError:
            continue
        sc = resolve_scope(ip, room_i, region_i)
        if sc is None:
            continue
        scope, cat = sc
        topic = build_topic(scope, cat, typ, order_i)

        # 衝突検出: 同一トピックを別の発信元(ip/room/region/order)が書こうとしたら警告。
        # 黙って last-writer-wins させない（mqtt-topics §0.2 の 1値1トピック原則を守る）。
        src = (ip, room_i, region_i, order_i)
        prev = owner.get(topic)
        if prev is not None and prev != src:
            print(f"[bridge] WARN collision on {topic}: {prev} vs {src} "
                  f"(order/region 取り違えの可能性。SCOPE_MAP/SENDER_OVERRIDE 要確認)",
                  flush=True)
        owner[topic] = src

        try:
            value = float(val)
        except ValueError:
            value = val.strip()
        publish(topic, json.dumps({"value": value, "unit": UNIT.get(typ, ""), "ts": int(time.time())}))
        if seen.get(topic) != value:           # 値変化のみログ
            seen[topic] = value
            pri = f" pri={priority}" if priority else ""
            print(f"  {ip:<15} {topic} = {value}{pri}", flush=True)
