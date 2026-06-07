#!/usr/bin/env python3
# agriha ダッシュボード (pi4 :8000) — 依存ゼロ。「見る用」総覧。
#   現在値タイル + ミニスパークライン(直近6h, agriha_logger のSQLite) + 窓位置バー + リレー表示。
#   設定/履歴は 8080 (agriha_web.py)。本サービスは閲覧専用。
# データ源: 現在値=mosquitto_sub retained スナップショット / トレンド=/home/pi/agriha_history.db
# 設計: Arsprout-RESTAPI/mqtt-topics.md

import json, subprocess, time, html, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
BROKER = "localhost"
DB_FILE = "/home/pi/agriha_history.db"
WEB_PORT = 8080            # 設定/履歴UI（nav リンク先）
REFRESH = 10               # sec

# type -> (表示名, 小数桁)
LABELS = {
    "InAirTemp": ("室温", 1), "InAirHumid": ("湿度", 0), "InAirCO2": ("CO2", 0),
    "InRadiation": ("日射", 2), "InAirPressure": ("気圧", 0), "InAirAbsHumid": ("絶対湿度", 1),
    "InAirDP": ("露点", 1), "InAirHD": ("飽差", 1), "IntgRadiation": ("積算日射", 1),
    "SoilTemp": ("地温", 1), "SoilWC": ("土壌水分", 0), "SoilEC": ("EC", 1), "Pulse": ("パルス", 0),
    "WAirTemp": ("外気温", 1), "WAirHumid": ("外湿度", 0), "WWindSpeed": ("風速", 1),
    "WWindDir16": ("風向", 0), "WRainfallAmt": ("雨量", 1), "WRadiation": ("日射", 2),
    "WAirPressure": ("気圧", 0),
}
HOUSE_HEADLINE = ["InAirTemp", "InAirHumid", "InAirCO2", "InRadiation"]
FARM_HEADLINE = ["WAirTemp", "WWindSpeed", "WWindDir16", "WRainfallAmt", "WRadiation"]

# ---------- MQTT snapshot ----------
def snapshot():
    try:
        out = subprocess.run(["timeout", "2", "mosquitto_sub", "-h", BROKER, "-t", "agriha/#", "-v"],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        out = ""
    data = {}
    for line in out.splitlines():
        topic, _, payload = line.partition(" ")
        if topic:
            data[topic] = payload
    return data

def pj(payload):
    try:
        return json.loads(payload)
    except Exception:
        return None

def organize(data):
    houses, farm = {}, {}
    for topic, payload in data.items():
        p = topic.split("/")
        if len(p) < 3:
            continue
        scope, cat = p[1], p[2]
        name = "/".join(p[3:]) if len(p) > 3 else cat
        (farm.setdefault(cat, {}) if scope == "farm"
         else houses.setdefault(scope, {}).setdefault(cat, {}))[name] = payload
    return houses, farm

# ---------- sparkline (SQLite) ----------
def db_ro():
    return sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=3)

def fetch_points(key, frm, to, maxpts=48):
    try:
        db = db_ro()
        r = db.execute("SELECT id FROM series WHERE key=?", (key,)).fetchone()
        if not r:
            db.close(); return []
        rows = db.execute("SELECT ts,value FROM samples WHERE sid=? AND ts>=? AND ts<=? ORDER BY ts",
                          (r[0], frm, to)).fetchall()
        db.close()
    except Exception:
        return []
    if len(rows) > maxpts:
        bw = max(1.0, (to - frm) / maxpts)
        buckets = {}
        for ts, v in rows:
            b = int((ts - frm) // bw)
            acc = buckets.get(b)
            if acc:
                acc[0] += v; acc[1] += 1
            else:
                buckets[b] = [v, 1]
        rows = [(b, buckets[b][0] / buckets[b][1]) for b in sorted(buckets)]
    else:
        rows = [(i, v) for i, (_, v) in enumerate(rows)]
    return rows

def spark(key, hours=6, w=150, h=34):
    to = int(time.time()); frm = to - hours * 3600
    pts = fetch_points(key, frm, to)
    if len(pts) < 2:
        return ""
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs); y0, y1 = min(ys), max(ys)
    if x1 == x0: x1 += 1
    if y1 == y0: y0 -= 1; y1 += 1
    pad = 3
    def X(x): return pad + (w - 2 * pad) * (x - x0) / (x1 - x0)
    def Y(y): return pad + (h - 2 * pad) * (1 - (y - y0) / (y1 - y0))
    d = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts)
    return (f"<svg class=spark width={w} height={h} viewBox='0 0 {w} {h}'>"
            f"<polyline fill=none stroke='#5bd' stroke-width=1.3 points='{d}'/></svg>")

# ---------- render ----------
def val_unit(payload):
    j = pj(payload)
    if isinstance(j, dict) and "value" in j:
        return j.get("value"), j.get("unit", ""), j.get("ts")
    return payload, "", None

def fmtnum(v, dec):
    try:
        return f"{float(v):.{dec}f}"
    except Exception:
        return html.escape(str(v))

def tile(scope, cat, t, payload, setpoint=None):
    label, dec = LABELS.get(t, (t, 1))
    v, u, _ = val_unit(payload)
    sp = ""
    if setpoint is not None:
        spv, spu, _ = val_unit(setpoint)
        sp = f"<div class=tsub>SP {fmtnum(spv, 1)}{html.escape(spu)}</div>"
    key = f"agriha/{scope}/{cat}/{t}"
    return (f"<div class=tile><div class=tlabel>{html.escape(label)}</div>"
            f"<div class=tval>{fmtnum(v, dec)}<span class=tunit>{html.escape(u)}</span></div>"
            f"{sp}{spark(key)}</div>")

def render_windows(d):
    out = []
    for wid, payload in sorted(d.items()):
        j = pj(payload) or {}
        pct = j.get("pct", 0) or 0
        tgt = j.get("target", pct) or 0
        mv = "▶" if j.get("moving") else ""
        src = html.escape(str(j.get("src", "")))
        try:
            pctf = max(0, min(100, float(pct))); tgtf = max(0, min(100, float(tgt)))
        except Exception:
            pctf = tgtf = 0
        out.append(
            f"<div class=winrow><span class=winlbl>win{html.escape(str(wid))} {mv}</span>"
            f"<span class=winbar><span class=winfill style='width:{pctf:.0f}%'></span>"
            f"<span class=wintgt style='left:{tgtf:.0f}%'></span></span>"
            f"<span class=winnum>{pctf:.0f}%→{tgtf:.0f}% <span class=age>{src}</span></span></div>")
    return "".join(out)

def render_relays(d):
    j = pj(d.get("state", "")) or {}
    cells = []
    for i in range(1, 9):
        on = j.get(f"ch{i}")
        cells.append(f"<span class={'on' if on else 'off'}>{i}{'●' if on else '○'}</span>")
    return "<div class=relays>" + " ".join(cells) + "</div>"

def online_badge(sys):
    on = str(pj(sys.get("online", "")) if "online" in sys else "") in ("1", "True", "true")
    up = ""
    st = pj(sys.get("status", "")) if "status" in sys else None
    if isinstance(st, dict) and "uptime" in st:
        up = f" <span class=age>up {st['uptime']}s</span>"
    return (f"<span class={'badge on' if on else 'badge off'}>{'●online' if on else '○offline'}</span>{up}")

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
header{background:#1a1d24;padding:12px 18px;font-size:20px;border-bottom:1px solid #333}
header a{color:#6cf;margin-left:16px;font-size:14px;text-decoration:none}
.wrap{padding:16px;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}
.house{background:#1a1d24;border:1px solid #2c313c;border-radius:10px;padding:14px;min-width:330px;flex:1 1 330px;max-width:520px}
.hhead{font-size:17px;color:#9cf;margin-bottom:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{font-size:12px;padding:1px 8px;border-radius:10px}
.badge.on{background:#15401f;color:#6f6}.badge.off{background:#3a1414;color:#f88}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px}
.tile{background:#0e1117;border:1px solid #232833;border-radius:8px;padding:8px 10px;min-width:120px;flex:1 1 120px}
.tlabel{color:#9aa;font-size:12px}
.tval{font-size:26px;font-weight:600;line-height:1.1;margin:2px 0}
.tunit{font-size:13px;color:#9aa;margin-left:3px}
.tsub{font-size:12px;color:#ffd24d;margin-bottom:2px}
.spark{display:block;margin-top:4px}
.winrow{display:flex;align-items:center;gap:8px;font-size:12px;margin:3px 0}
.winlbl{color:#7fd;min-width:54px}
.winbar{position:relative;flex:1;height:12px;background:#0c0e12;border:1px solid #2c313c;border-radius:6px;overflow:hidden}
.winfill{position:absolute;left:0;top:0;bottom:0;background:#2b6}
.wintgt{position:absolute;top:-2px;width:2px;height:16px;background:#ffd24d}
.winnum{color:#cde;min-width:120px;text-align:right}
.relays{font-family:monospace;margin-top:6px}
.on{color:#5f5}.off{color:#555}
.age{color:#667;font-size:11px}
.sub{margin-top:6px;font-size:12px;color:#9aa}
.sub b{color:#cde}
.note{color:#778;font-size:12px;padding:0 18px}
"""

def section_title(txt):
    return f"<div class=sub style='color:#778;margin-top:8px'>{html.escape(txt)}</div>"

def render_house(scope, h):
    parts = [f"<div class=house><div class=hhead>house {html.escape(scope)} {online_badge(h.get('sys', {}))}</div>"]
    sensors = h.get("sensor", {})
    setp = h.get("setpoint", {})
    # headline tiles
    tiles = []
    for t in HOUSE_HEADLINE:
        if t in sensors:
            sp = setp.get("temp") if t == "InAirTemp" else None
            tiles.append(tile(scope, "sensor", t, sensors[t], sp))
    if tiles:
        parts.append("<div class=tiles>" + "".join(tiles) + "</div>")
    # windows
    if h.get("window"):
        parts.append(render_windows(h["window"]))
    # relays
    if h.get("relay"):
        parts.append(render_relays(h["relay"]))
    # other sensors (compact)
    others = {t: v for t, v in sensors.items() if t not in HOUSE_HEADLINE}
    if others:
        kv = " · ".join(f"{html.escape(LABELS.get(t, (t,1))[0])} <b>{fmtnum(val_unit(v)[0], LABELS.get(t,(t,1))[1])}</b>"
                        for t, v in sorted(others.items()))
        parts.append(f"<div class=sub>{kv}</div>")
    parts.append("</div>")
    return "".join(parts)

def render_farm(farm):
    w = farm.get("weather", {})
    if not w:
        return ""
    parts = ["<div class=house><div class=hhead>🌤 farm / weather</div><div class=tiles>"]
    for t in FARM_HEADLINE:
        if t in w:
            parts.append(tile("farm", "weather", t, w[t]))
    parts.append("</div>")
    others = {t: v for t, v in w.items() if t not in FARM_HEADLINE}
    if others:
        kv = " · ".join(f"{html.escape(LABELS.get(t,(t,1))[0])} <b>{fmtnum(val_unit(v)[0], LABELS.get(t,(t,1))[1])}</b>"
                        for t, v in sorted(others.items()))
        parts.append(f"<div class=sub>{kv}</div>")
    parts.append("</div>")
    return "".join(parts)

def page(host):
    data = snapshot()
    houses, farm = organize(data)
    base = host.rsplit(":", 1)[0] if host else "pi4.local"
    web = f"http://{base}:{WEB_PORT}"
    cards = [render_farm(farm)]
    for scope in sorted(houses, key=lambda x: int(x) if x.isdigit() else 999):
        cards.append(render_house(scope, houses[scope]))
    if not any(cards):
        cards.append("<div class=house>no agriha data（broker/bridge 稼働中？）</div>")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content={REFRESH}><title>agriha dashboard</title><style>{CSS}</style></head><body>
<header>🌱 agriha dashboard <span class=age>{time.strftime('%H:%M:%S')} · {REFRESH}s</span>
<a href="{web}/schedule">schedule</a><a href="{web}/history">history</a></header>
<div class=wrap>{''.join(cards)}</div>
<p class=note>現在値=retainedスナップショット / スパークライン=直近6h。設定・履歴は :{WEB_PORT}。</p>
</body></html>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = page(self.headers.get("Host", "")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers(); self.wfile.write(b"not found")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print(f"[dashboard] http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
