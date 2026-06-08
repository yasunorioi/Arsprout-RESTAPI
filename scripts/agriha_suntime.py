#!/usr/bin/env python3
# agriha 日の出/日の入り計算 — 依存ゼロ（math のみ）。
# スケジュールの SUNRISE/SUNSET アンカー解決に使う。lat/lon は ArSprout NodeConfig=35/135。
# 精度は中緯度で±数分（±offset 運用なので十分）。参照: Sunrise equation (NOAA 略算)。

import math, datetime

J2000 = datetime.date(2000, 1, 1).toordinal()

def sun_times(lat, lon, d, tz):
    """(sunrise_min, sunset_min) を「現地真夜中からの分」(float) で返す。極夜/白夜は (None,None)。
    lat,lon=度(東経正), d=datetime.date, tz=UTCオフセット時間(JST=9)。"""
    n = d.toordinal() - J2000
    Jstar = n + 0.0009 - lon / 360.0
    M = (357.5291 + 0.98560028 * Jstar) % 360.0
    Mr = math.radians(M)
    C = 1.9148 * math.sin(Mr) + 0.0200 * math.sin(2 * Mr) + 0.0003 * math.sin(3 * Mr)
    L = (M + C + 282.9372) % 360.0           # 180 + 102.9372
    Lr = math.radians(L)
    Jtransit = 2451545.0 + Jstar + 0.0053 * math.sin(Mr) - 0.0069 * math.sin(2 * Lr)
    dec = math.asin(math.sin(Lr) * math.sin(math.radians(23.4397)))
    latr = math.radians(lat)
    cosH = (math.sin(math.radians(-0.833)) - math.sin(latr) * math.sin(dec)) / (math.cos(latr) * math.cos(dec))
    if cosH >= 1.0:
        return (None, None)                  # 太陽が昇らない
    if cosH <= -1.0:
        return (None, None)                  # 太陽が沈まない
    H = math.degrees(math.acos(cosH)) / 360.0
    def local_min(jd):
        utc_h = ((jd + 0.5) % 1.0) * 24.0
        return ((utc_h + tz) % 24.0) * 60.0
    return (local_min(Jtransit - H), local_min(Jtransit + H))

def hhmm(minutes):
    if minutes is None:
        return "--:--"
    m = int(round(minutes)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"

if __name__ == "__main__":
    today = datetime.date.today()
    sr, ss = sun_times(35.0, 135.0, today, 9)
    print(f"{today} lat35/lon135 JST: sunrise={hhmm(sr)} sunset={hhmm(ss)}")
