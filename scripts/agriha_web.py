#!/usr/bin/env python3
# agriha 中央 Web UI (pi4) — 依存ゼロ。stdlib http.server + mosquitto_sub スナップショット。
#   GET /             : ダッシュボード（全ハウス sensor/setpoint/window/relay + farm weather）
#   GET /schedule     : setpoint スケジュール編集
#   POST /api/schedule: agriha_schedule.json 保存（scheduler が mtime で自動再読込）
# 設計: Arsprout-RESTAPI/setpoint-schedule-design.md / mqtt-topics.md

import json, subprocess, time, html, os, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8080
BROKER = "localhost"
SCHED_FILE = "/home/pi/agriha_schedule.json"
DB_FILE = "/home/pi/agriha_history.db"   # agriha_logger.py が書く時系列DB
PALETTE = ["#6cf", "#ffd24d", "#7fd", "#f88", "#c9f", "#8f8", "#fb7", "#9cf"]
RANGES = [("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800), ("30d", 2592000)]

# ---------- MQTT snapshot ----------
def snapshot():
    # `timeout 2 mosquitto_sub`: retained は購読直後に一括配信される。2秒で打ち切り、
    # stdout を回収（mosquitto_sub の -W は -C 併用前提なので coreutils timeout を使う）。
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
        if scope == "farm":
            farm.setdefault(cat, {})[name] = payload
        else:
            houses.setdefault(scope, {}).setdefault(cat, {})[name] = payload
    return houses, farm

def age(ts):
    try:
        return f" <span class=age>({int(time.time())-int(ts)}s)</span>"
    except Exception:
        return ""

def fmt(payload):
    j = pj(payload)
    if isinstance(j, dict) and "value" in j:
        v, u = j.get("value"), j.get("unit", "")
        return f"<b>{html.escape(str(v))}</b> {html.escape(u)}{age(j.get('ts'))}"
    return html.escape(str(payload))

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
header{background:#1a1d24;padding:10px 16px;font-size:18px;border-bottom:1px solid #333}
header a{color:#6cf;margin-left:14px;font-size:14px;text-decoration:none}
.wrap{padding:14px;display:flex;flex-wrap:wrap;gap:14px}
.card{background:#1a1d24;border:1px solid #2c313c;border-radius:8px;padding:12px;min-width:260px}
.card h2{margin:0 0 8px;font-size:15px;color:#9cf;border-bottom:1px solid #2c313c;padding-bottom:6px}
table{border-collapse:collapse;width:100%}td{padding:2px 6px;font-size:13px}
td.k{color:#9aa;white-space:nowrap}td.v{text-align:right}
.age{color:#667;font-size:11px}
.sp{color:#ffd24d}.win{color:#7fd}.rel{font-family:monospace}
.on{color:#5f5}.off{color:#555}
.note{color:#778;font-size:12px;padding:0 16px}
textarea{width:100%;height:160px;background:#0c0e12;color:#cfe;border:1px solid #333;
  border-radius:6px;font-family:monospace;font-size:14px;padding:8px}
button{background:#2563eb;color:#fff;border:0;padding:8px 18px;border-radius:6px;cursor:pointer;font-size:14px}
.msg{margin-left:12px}
.controls{padding:10px 12px;background:#1a1d24;border:1px solid #2c313c;border-radius:8px;margin:0 0 12px}
.chk{display:inline-block;margin:2px 12px 2px 0;font-size:12px;color:#ccd;white-space:nowrap}
.rg{margin-right:12px;font-size:13px;color:#cde}
svg{max-width:100%;margin-bottom:12px}
"""

def kv_table(d, cls=""):
    rows = "".join(
        f"<tr><td class=k>{html.escape(k)}</td><td class='v {cls}'>{fmt(v)}</td></tr>"
        for k, v in sorted(d.items()))
    return f"<table>{rows}</table>"

def render_window(d):
    rows = []
    for wid, payload in sorted(d.items()):
        j = pj(payload) or {}
        pct, tgt = j.get("pct"), j.get("target")
        mv = "▶" if j.get("moving") else "■"
        src = j.get("src", "")
        rows.append(f"<tr><td class=k>win{html.escape(wid)}</td>"
                    f"<td class='v win'>{pct}%→{tgt}% {mv} <span class=age>{html.escape(str(src))}</span></td></tr>")
    return f"<table>{''.join(rows)}</table>"

def render_relay(d):
    j = pj(d.get("state", "")) or {}
    cells = []
    for i in range(1, 9):
        on = j.get(f"ch{i}")
        cells.append(f"<span class={'on' if on else 'off'}>{i}{'●' if on else '○'}</span>")
    return "<div class=rel>" + " ".join(cells) + "</div>"

def page_dashboard():
    data = snapshot()
    houses, farm = organize(data)
    cards = []

    # farm weather
    if farm.get("weather"):
        cards.append(f"<div class=card><h2>farm / weather</h2>{kv_table(farm['weather'])}</div>")

    for house in sorted(houses, key=lambda x: int(x) if x.isdigit() else 999):
        h = houses[house]
        parts = [f"<div class=card><h2>house {html.escape(house)}</h2>"]
        if "setpoint" in h:
            parts.append("<b class=sp>setpoint</b>" + kv_table(h["setpoint"], "sp"))
        if "sensor" in h:
            parts.append("sensor" + kv_table(h["sensor"]))
        if "window" in h:
            parts.append("window" + render_window(h["window"]))
        if "relay" in h:
            parts.append("relay " + render_relay(h["relay"]))
        if "sys" in h:
            parts.append("sys" + kv_table(h["sys"]))
        parts.append("</div>")
        cards.append("".join(parts))

    if not cards:
        cards.append("<div class=card>no agriha data (broker/bridge 稼働中？)</div>")

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=10><title>agriha</title><style>{CSS}</style></head><body>
<header>🌱 agriha <span class=age>{time.strftime('%H:%M:%S')} · 10s refresh</span>
<a href=/>dashboard</a><a href=/schedule>schedule</a><a href=/history>history</a></header>
<div class=wrap>{''.join(cards)}</div>
<p class=note>retained snapshot via mosquitto_sub. window: 現在%→目標% ▶/■ source。</p>
</body></html>"""

# ---------- schedule editor ----------
def load_sched():
    try:
        with open(SCHED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def sched_to_text(sched):
    lines = []
    for house in sorted(sched, key=lambda x: int(x) if x.isdigit() else 999):
        toks = ", ".join(f"{t}={v}" for t, v in sched[house])
        lines.append(f"{house}: {toks}")
    return "\n".join(lines)

def text_to_sched(text):
    sched = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        house, _, rest = line.partition(":")
        house = house.strip()
        periods = []
        for tok in rest.split(","):
            tok = tok.strip()
            if not tok:
                continue
            t, _, temp = tok.partition("=")
            t = t.strip()
            hh, _, mm = t.partition(":")
            if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError(f"bad time '{t}'")
            periods.append([f"{int(hh):02d}:{int(mm):02d}", float(temp)])
        if house and periods:
            sched[house] = periods
    if not sched:
        raise ValueError("empty schedule")
    return sched

def page_schedule(msg=""):
    txt = html.escape(sched_to_text(load_sched()))
    return f"""<!doctype html><html><head><meta charset=utf-8><title>agriha schedule</title>
<style>{CSS}</style></head><body>
<header>🌱 agriha — setpoint schedule<a href=/>dashboard</a><a href=/schedule>schedule</a><a href=/history>history</a></header>
<div class=wrap><div class=card style=min-width:520px>
<h2>時間帯別 目標室温（℃）</h2>
<p class=note>1行=1ハウス。書式: <code>house: HH:MM=temp, HH:MM=temp, ...</code>（例 <code>2: 06:00=22, 10:00=26, 20:00=20</code>）。
保存で即反映（scheduler が自動再読込）。setpoint=換気開始温度（室温&gt;setpointで開き始め）。</p>
<form method=post action=/api/schedule>
<textarea name=sched>{txt}</textarea><br><br>
<button type=submit>保存</button><span class=msg>{html.escape(msg)}</span>
</form></div></div></body></html>"""

# ---------- history (SQLite + サーバ側SVG) ----------
def db_ro():
    # WAL の reader。ロガーが書込み中でも読める。読み取り専用で開く。
    return sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=3)

def list_series():
    try:
        db = db_ro()
        rows = db.execute("SELECT key, unit FROM series ORDER BY key").fetchall()
        db.close()
        return rows
    except Exception:
        return []

def fetch_series(key, frm, to, maxpts=600):
    """[(ts,value)...], unit を返す。raw(30日)＋agg(5分平均)を結合し maxpts へ間引き。"""
    try:
        db = db_ro()
        r = db.execute("SELECT id, unit FROM series WHERE key=?", (key,)).fetchone()
        if not r:
            db.close(); return [], ""
        sid, unit = r
        rows = db.execute(
            "SELECT ts,value FROM samples WHERE sid=? AND ts>=? AND ts<=? "
            "UNION ALL SELECT ts,value FROM samples_agg WHERE sid=? AND ts>=? AND ts<=? "
            "ORDER BY ts", (sid, frm, to, sid, frm, to)).fetchall()
        db.close()
    except Exception:
        return [], ""
    if len(rows) > maxpts:               # 時間バケット平均でダウンサンプル
        bw = max(1.0, (to - frm) / maxpts)
        buckets = {}
        for ts, v in rows:
            b = int((ts - frm) // bw)
            acc = buckets.get(b)
            if acc:
                acc[0] += v; acc[1] += 1
            else:
                buckets[b] = [v, 1, ts]
        rows = [(buckets[b][2], buckets[b][0] / buckets[b][1]) for b in sorted(buckets)]
    return rows, unit

def svg_chart(group, unit, frm, to, w=900, h=300):
    """group=[(key,[(ts,v)...])...] を同一単位で重ね描き。"""
    ml, mr, mt, mb = 56, 150, 18, 28
    iw, ih = w - ml - mr, h - mt - mb
    vals = [v for _, pts in group for _, v in pts]
    if not vals:
        return f"<svg width={w} height={h}></svg>"
    vmin, vmax = min(vals), max(vals)
    if vmin == vmax:
        vmin -= 1; vmax += 1
    pad = (vmax - vmin) * 0.08
    vmin -= pad; vmax += pad
    span = max(1, to - frm)
    def X(ts): return ml + iw * (ts - frm) / span
    def Y(v):  return mt + ih * (1 - (v - vmin) / (vmax - vmin))
    p = [f"<svg width={w} height={h} style='background:#0c0e12;border:1px solid #2c313c;border-radius:6px'>"]
    for i in range(5):                    # y グリッド＋目盛
        v = vmin + (vmax - vmin) * i / 4; y = Y(v)
        p.append(f"<line x1={ml} y1={y:.1f} x2={ml+iw} y2={y:.1f} stroke='#222'/>")
        p.append(f"<text x={ml-6} y={y+4:.1f} fill='#778' font-size=11 text-anchor=end>{v:.1f}</text>")
    long_range = (to - frm) > 2 * 86400
    for i in range(5):                    # x 時刻目盛
        ts = frm + (to - frm) * i / 4; x = X(ts)
        lbl = time.strftime('%m/%d %H:%M' if long_range else '%H:%M', time.localtime(ts))
        p.append(f"<line x1={x:.1f} y1={mt} x2={x:.1f} y2={mt+ih} stroke='#1a1d24'/>")
        p.append(f"<text x={x:.1f} y={h-8} fill='#778' font-size=11 text-anchor=middle>{lbl}</text>")
    p.append(f"<text x={ml} y=13 fill='#9cf' font-size=12>{html.escape(unit or '(no unit)')}</text>")
    for idx, (key, pts) in enumerate(group):
        col = PALETTE[idx % len(PALETTE)]
        if pts:
            d = " ".join(f"{X(ts):.1f},{Y(v):.1f}" for ts, v in pts)
            p.append(f"<polyline fill=none stroke='{col}' stroke-width=1.5 points='{d}'/>")
        ly = mt + 14 + idx * 16
        short = key.split('/', 2)[-1] if key.count('/') >= 2 else key
        p.append(f"<rect x={ml+iw+10} y={ly-9} width=10 height=10 fill='{col}'/>")
        p.append(f"<text x={ml+iw+24} y={ly} fill='#ccd' font-size=11>{html.escape(short)}</text>")
    p.append("</svg>")
    return "".join(p)

def page_history(qs):
    sel = qs.get("s", [])
    rng = qs.get("range", ["24h"])[0]
    secs = dict(RANGES).get(rng, 86400)
    to = int(time.time()); frm = to - secs

    boxes = []
    for key, unit in list_series():
        chk = "checked" if key in sel else ""
        boxes.append(f"<label class=chk><input type=checkbox name=s value='{html.escape(key)}' {chk}>"
                     f" {html.escape(key)} <span class=age>{html.escape(unit or '')}</span></label>")
    if not boxes:
        boxes.append("<span class=note>系列なし（agriha-logger 稼働中？ DB未生成？）</span>")
    rads = "".join(f"<label class=rg><input type=radio name=range value={r} {'checked' if r == rng else ''}>{r}</label>"
                   for r, _ in RANGES)

    charts = []
    if sel:
        byunit = {}
        for key in sel:
            pts, unit = fetch_series(key, frm, to)
            byunit.setdefault(unit, []).append((key, pts))
        for unit, group in byunit.items():
            charts.append(svg_chart(group, unit, frm, to))
    else:
        charts.append("<p class=note>系列を選んで「表示」。同一単位は重ね描き。</p>")

    return f"""<!doctype html><html><head><meta charset=utf-8><title>agriha history</title>
<style>{CSS}</style></head><body>
<header>🌱 agriha — history<a href=/>dashboard</a><a href=/schedule>schedule</a><a href=/history>history</a></header>
<div class=wrap style=display:block>
<form method=get action=/history>
<div class=controls>{rads}<button type=submit style=margin-left:12px>表示</button></div>
<div class=controls>{''.join(boxes)}</div>
</form>
{''.join(charts)}
<p class=note>raw 30日＋5分平均(集約)。最大600点に間引き。</p>
</div></body></html>"""

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/schedule"):
            self._send(page_schedule())
        elif self.path.startswith("/history"):
            self._send(page_history(parse_qs(urlparse(self.path).query)))
        elif self.path == "/" or self.path.startswith("/index"):
            # ダッシュボードは :8000 (agriha_dashboard.py) へ移設。8080 は設定/履歴専用。
            host = self.headers.get("Host", "pi4.local:8080")
            base = host.rsplit(":", 1)[0]
            self.send_response(302)
            self.send_header("Location", f"http://{base}:8000/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send("not found", 404, "text/plain")

    def do_POST(self):
        if self.path == "/api/schedule":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8", "replace")
            text = parse_qs(body).get("sched", [""])[0]
            try:
                sched = text_to_sched(text)
                with open(SCHED_FILE, "w") as f:
                    json.dump(sched, f, ensure_ascii=False, indent=2)
                self._send(page_schedule(f"✓ 保存しました ({len(sched)} houses)"))
            except Exception as e:
                self._send(page_schedule(f"✗ エラー: {e}"))
        else:
            self._send("not found", 404, "text/plain")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print(f"[web] http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
