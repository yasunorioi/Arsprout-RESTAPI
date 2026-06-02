#!/usr/bin/env python3
# agriha 中央 Web UI (pi4) — 依存ゼロ。stdlib http.server + mosquitto_sub スナップショット。
#   GET /             : ダッシュボード（全ハウス sensor/setpoint/window/relay + farm weather）
#   GET /schedule     : setpoint スケジュール編集
#   POST /api/schedule: agriha_schedule.json 保存（scheduler が mtime で自動再読込）
# 設計: Arsprout-RESTAPI/setpoint-schedule-design.md / mqtt-topics.md

import json, subprocess, time, html, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PORT = 8080
BROKER = "localhost"
SCHED_FILE = "/home/pi/agriha_schedule.json"

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
<a href=/>dashboard</a><a href=/schedule>schedule</a></header>
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
<header>🌱 agriha — setpoint schedule<a href=/>dashboard</a><a href=/schedule>schedule</a></header>
<div class=wrap><div class=card style=min-width:520px>
<h2>時間帯別 目標室温（℃）</h2>
<p class=note>1行=1ハウス。書式: <code>house: HH:MM=temp, HH:MM=temp, ...</code>（例 <code>2: 06:00=22, 10:00=26, 20:00=20</code>）。
保存で即反映（scheduler が自動再読込）。setpoint=換気開始温度（室温&gt;setpointで開き始め）。</p>
<form method=post action=/api/schedule>
<textarea name=sched>{txt}</textarea><br><br>
<button type=submit>保存</button><span class=msg>{html.escape(msg)}</span>
</form></div></div></body></html>"""

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
        elif self.path == "/" or self.path.startswith("/index"):
            self._send(page_dashboard())
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
