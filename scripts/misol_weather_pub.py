#!/usr/bin/env python3
# Misol WH65LP 屋外気象 → agriha/farm/weather/* (yasu-hp 直結版)
#   .70/.71(ArSprout) 売却で CCM 経由の farm/weather が途絶えたため、
#   WH65LP の RS485(CH340) を yasu-hp に直接挿して読む。依存は pyserial のみ。
#   publish は agriha_logger と同じく mosquitto_pub を subprocess で呼ぶ。
#
# トピックは .71 時代の CCM 由来の名前・単位をそのまま継ぐ(履歴と agriha_logic.json の互換):
#   WAirTemp C / WAirHumid % / WWindSpeed m s-1 / WWindDir16 16dir(0=N 時計回り)
#   WRainfallAmt mm = 直近60分の降水量 / WRainfall 0/1 = 直近 RAIN_ON_SEC(600)秒に転倒あり
# 追加: WWindDir deg(§2.5 正準) と misol(§2.2 多値 raw blob、照度/UV/突風/電池)。
# フレーム仕様は hardware.md §4。
#
# UECS-CCM: .71(ArSprout) が出していた6種のうち WAirTemp を除く5種を room1/region41 で 10 秒ごとに送る
#   (型名・cast は arsprout-backup/20260910/71_ccm.json に合わせる。.81 は WAirHumid/WWindSpeed/
#   WRainfall/WRainfallAmt.cMC を side=R で受けている)。
#   WAirTemp 1/41 は .81 自身も送っていて二重送信になるので外した(2026-10-07)。1 パケット 1 DATA、<IP> 必須、
#   ArSprout は 255.255.255.255 しか聞かないので broadcast と 224.0.0.1 の両方へ送る(AgriCCM.h と同じ)。
#   正常フレームが CCM_STALE 秒途絶えたら送信を止める(凍った値を流し続けない)。

import os, sys, time, json, socket, subprocess, collections, threading
import serial

PORT        = os.environ.get("MISOL_PORT", "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
BAUD        = 9600
BROKER      = os.environ.get("MQTT_HOST", "localhost")
PREFIX      = "agriha/farm/weather/"
STALE_SEC   = 120         # この間 正常フレームが無ければポートを開き直す
RAIN_WIN    = 3600        # WRainfallAmt の集計窓
RAIN_ON_SEC = 600         # WRainfall=1 とみなす直近転倒の窓。転倒式は小雨で数分〜数十分おきにしか
                          # 傾かず、60s だと .81 の側窓が雨の合間に開いてしまう(2026-10-07)
FRAME_LEN   = 17

CCM_ENABLE   = os.environ.get("MISOL_CCM", "1") == "1"
CCM_IP       = os.environ.get("CCM_IP", "192.168.1.228")   # <IP> 要素と送信元 IF
CCM_PORT     = 16520
CCM_DESTS    = ("255.255.255.255", "224.0.0.1")
CCM_INTERVAL = 10          # level A-10S-0
CCM_STALE    = 60
CCM_ROOM, CCM_REGION, CCM_ORDER, CCM_PRI = 1, 41, 1, 1
# (CCM 型名, 値を作る関数) — 書式は .71 の実値("64" "2.5" "5" "0" "0.0")に合わせる
CCM_ITEMS = [
    ("WAirHumid",        lambda d: f"{d['humidity_pct']:d}"),
    ("WWindSpeed",       lambda d: None if d["wind_speed_ms"] is None else f"{d['wind_speed_ms']:.1f}"),
    ("WWindDir16",       lambda d: None if d["WWindDir16"] is None else f"{int(d['WWindDir16']):d}"),
    ("WRainfall",        lambda d: f"{int(d['WRainfall']):d}"),
    ("WRainfallAmt.cMC", lambda d: f"{d['WRainfallAmt']:.1f}"),
]


def log(msg):
    print(f"[misol] {msg}", flush=True)


def pub(topic, payload):
    subprocess.run(["mosquitto_pub", "-h", BROKER, "-q", "1", "-r",
                    "-t", PREFIX + topic, "-m", json.dumps(payload)],
                   timeout=10, check=False)


def decode(f):
    """検証済みフレーム(先頭 17 バイト + あれば拡張部) -> dict(無効値は None)。
    電池は bit フラグしか仕様に無い。raw は byte15 や拡張部に電圧が無いかを調べるために残す"""
    wd  = f[2] | ((f[3] & 0x80) << 1)
    tr  = f[4] | ((f[3] & 0x07) << 8)
    ws  = f[6] | ((f[3] & 0x10) << 4)
    uv  = (f[10] << 8) | f[11]
    lux = (f[12] << 16) | (f[13] << 8) | f[14]
    return {
        "wind_dir_deg":  None if wd == 0x1FF else wd,
        "temperature_c": None if tr == 0x7FF else round((tr - 400) / 10.0, 1),
        "humidity_pct":  f[5],
        "wind_speed_ms": None if ws == 0x1FF else round(ws / 8.0 * 1.12, 2),
        "gust_speed_ms": None if f[7] == 0xFF else round(f[7] * 1.12, 2),
        "rain_raw":      (f[8] << 8) | f[9],
        "uv_wm2":        None if uv == 0xFFFF else uv / 10.0,
        "light_lux":     None if lux == 0xFFFFFF else lux / 10.0,
        "battery_low":   bool(f[3] & 0x08),
        "raw":           f.hex(),
    }


def frames(ser):
    """シリアルから検証済み 17 バイトフレームを順に返す(0x24 同期 + チェックサム)"""
    buf = bytearray()
    last_ok = time.time()
    while True:
        buf += ser.read(64)
        while True:
            i = buf.find(0x24)
            if i < 0:
                buf.clear()
                break
            del buf[:i]
            if len(buf) < FRAME_LEN:
                break
            if sum(buf[:16]) & 0xFF == buf[16]:
                if len(buf) < 21:
                    buf += ser.read(21 - len(buf))   # 拡張部(気圧ほか)も raw に残す
                ext = len(buf) >= 21 and buf[FRAME_LEN] != 0x24
                yield bytes(buf[:21 if ext else FRAME_LEN])
                last_ok = time.time()
                del buf[:FRAME_LEN]      # 拡張フレームの残り 4 バイトは次の同期探索で読み飛ばす
            else:
                del buf[:1]
        if time.time() - last_ok > STALE_SEC:
            raise TimeoutError(f"no valid frame for {STALE_SEC}s")


class Rain:
    """累積カウンタ(0.3mm/count, 16bit で巻き戻る)から 60 分降水量と降雨中フラグを出す"""
    def __init__(self):
        self.prev = None
        self.tips = collections.deque()   # (ts, mm)

    def update(self, now, raw):
        if self.prev is not None:
            d = (raw - self.prev) & 0xFFFF
            if 0 < d < 100:              # 異常な跳び(電池交換のリセット等)は捨てる
                self.tips.append((now, d * 0.3))
        self.prev = raw
        while self.tips and self.tips[0][0] < now - RAIN_WIN:
            self.tips.popleft()
        amt = round(float(sum(mm for _, mm in self.tips)), 1)
        raining = 1.0 if self.tips and self.tips[-1][0] >= now - RAIN_ON_SEC else 0.0
        return amt, raining


_latest = {"ts": 0, "d": None}
_lock = threading.Lock()


def ccm_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(CCM_IP))
    sock.bind((CCM_IP, 0))
    log(f"CCM send room{CCM_ROOM}/region{CCM_REGION} every {CCM_INTERVAL}s -> {CCM_DESTS}")
    stale_logged = False
    while True:
        time.sleep(CCM_INTERVAL)
        with _lock:
            ts, d = _latest["ts"], _latest["d"]
        if d is None or time.time() - ts > CCM_STALE:
            if d is not None and not stale_logged:
                log("CCM paused: no fresh frame")
                stale_logged = True
            continue
        stale_logged = False
        for typ, fn in CCM_ITEMS:
            v = fn(d)
            if v is None:
                continue
            xml = (f'<?xml version="1.0"?><UECS ver="1.00-E10">'
                   f'<DATA type="{typ}" room="{CCM_ROOM}" region="{CCM_REGION}" '
                   f'order="{CCM_ORDER}" priority="{CCM_PRI}">{v}</DATA>'
                   f'<IP>{CCM_IP}</IP></UECS>').encode()
            for dst in CCM_DESTS:
                try:
                    sock.sendto(xml, (dst, CCM_PORT))
                except OSError as e:
                    log(f"CCM send {dst}: {e}")


def run():
    rain = Rain()
    if CCM_ENABLE:
        threading.Thread(target=ccm_loop, daemon=True).start()
    while True:
        try:
            with serial.Serial(PORT, BAUD, timeout=2) as ser:
                log(f"open {PORT}")
                n = 0
                for f in frames(ser):
                    now = int(time.time())
                    d = decode(f)
                    amt, raining = rain.update(now, d["rain_raw"])
                    out = {
                        "WAirTemp":     (d["temperature_c"], "C"),
                        "WAirHumid":    (d["humidity_pct"], "%"),
                        "WWindSpeed":   (d["wind_speed_ms"], "m s-1"),
                        "WWindDir":     (d["wind_dir_deg"], "deg"),
                        "WWindDir16":   (None if d["wind_dir_deg"] is None
                                         else float(round(d["wind_dir_deg"] / 22.5) % 16), "16dir"),
                        "WRainfallAmt": (amt, "mm"),
                        "WRainfall":    (raining, ""),
                    }
                    for name, (v, unit) in out.items():
                        if v is not None:
                            pub(name, {"value": v, "unit": unit, "ts": now})
                    pub("misol", {**d, "rain_60min_mm": amt, "ts": now})
                    with _lock:
                        _latest["ts"], _latest["d"] = now, {
                            **d, "WWindDir16": out["WWindDir16"][0],
                            "WRainfall": raining, "WRainfallAmt": amt}
                    n += 1
                    if n == 1 or n % 225 == 0:   # 起動直後と約1時間ごと
                        log(f"frame#{n} {json.dumps(d)}")
        except Exception as e:
            log(f"error: {e}; retry in 10s")
            time.sleep(10)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
