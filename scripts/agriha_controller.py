#!/usr/bin/env python3
# agriha 制御コントローラ (pi4) — 依存ゼロ。中央ブレーンの制御ロジック実行 daemon。
#   mosquitto_sub agriha/# を常駐購読して状態キャッシュ → 各 Logic を毎秒評価 →
#   コマンド publish（relay/window set）＋状態 publish。
# 設計: arsprout-logic-design.md（排他所有＋換気協調＋安全上書き / 常駐JSON評価）。
#   spec: arsprout-logic-spec.md
#
# 実装済: SWT_RULE（汎用ルールエンジン＝CO2/飽差(湿度)/任意 on/off のデューティ制御）。
#   STD_ATMP/STD_CRTN/STD_IRRI は今後追加（同フレームワーク上）。
#
# 安全機構:
#   - dry_run=true の Logic は実アクチュエータを叩かず agriha/{house}/logic/{id}/cmd に意図のみ publish。
#     （配線前の検証・UIプレビュー用。既定 true）
#   - アクチュエータ排他所有: 同一 (house,actuator) を実駆動する Logic が複数あれば衝突検出し後勝ちを dry_run 化。
#   - 換気協調は SWT_RULE の条件に窓開度を入れて表現（例: window#pct LE 20 の時だけ施用）。

import json, os, time, subprocess, threading, datetime
import agriha_suntime as sun

BROKER, PORT = "localhost", 1883
CONF_FILE = "/home/pi/agriha_logic.json"
STATE_REFRESH = 30          # 変化なしでも state/cmd を再送する間隔(sec)
BASE_TICK = 1.0             # 評価周期(sec)。デューティ計時のため細かく回す

DEFAULT_CONF = {
    "site": {"lat": 35.0, "lon": 135.0, "tz": 9},
    "logics": []
}

# ---------- MQTT ----------
class MqttCache:
    """mosquitto_sub agriha/# を常駐させ topic->payload を保持。"""
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                p = subprocess.Popen(
                    ["mosquitto_sub", "-h", BROKER, "-p", str(PORT), "-t", "agriha/#", "-v"],
                    stdout=subprocess.PIPE, text=True, bufsize=1)
                for line in p.stdout:
                    topic, _, payload = line.rstrip("\n").partition(" ")
                    if topic:
                        with self.lock:
                            self.data[topic] = payload
            except Exception as e:
                print(f"[ctrl] sub error: {e}", flush=True)
            time.sleep(3)

    def get(self, topic):
        with self.lock:
            return self.data.get(topic)

def mqtt_pub(topic, payload, retain=False):
    args = ["mosquitto_pub", "-h", BROKER, "-p", str(PORT), "-t", topic, "-m", payload]
    if retain:
        args.append("-r")
    try:
        subprocess.run(args, timeout=5, check=False)
    except Exception as e:
        print(f"[ctrl] pub error {topic}: {e}", flush=True)

# ---------- value access ----------
def to_num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return None

def get_value(cache, ref):
    """ref = "topic" or "topic#subkey"。{value,..}形式は value を、その他は subkey or 生数値。"""
    topic, _, sub = ref.partition("#")
    raw = cache.get(topic)
    if raw is None:
        return None
    try:
        j = json.loads(raw)
    except Exception:
        return None
    if sub:
        return to_num(j.get(sub)) if isinstance(j, dict) else None
    if isinstance(j, dict):
        return to_num(j.get("value")) if "value" in j else None
    return to_num(j)

OPS = {
    "GE": lambda a, b: a >= b, "LE": lambda a, b: a <= b,
    "GT": lambda a, b: a > b,  "LT": lambda a, b: a < b,
    "EQ": lambda a, b: a == b, "NE": lambda a, b: a != b,
}

# ---------- schedule ----------
def anchor_min(spec, sr, ss):
    """spec={anchor,offset|time} -> 現地分。anchor=fixed は time(HH:MM)、sunrise/sunset は当日±offset。"""
    a = (spec or {}).get("anchor", "fixed")
    off = float((spec or {}).get("offset", 0))
    if a == "sunrise":
        return None if sr is None else sr + off
    if a == "sunset":
        return None if ss is None else ss + off
    t = (spec or {}).get("time", "00:00")
    try:
        hh, mm = t.split(":")
        return int(hh) * 60 + int(mm) + off
    except Exception:
        return off

def in_window(win, now_min, sr, ss):
    if not win:
        return True
    s = anchor_min(win.get("start"), sr, ss)
    e = anchor_min(win.get("end"), sr, ss)
    if s is None or e is None:
        return False
    if s <= e:
        return s <= now_min <= e
    return now_min >= s or now_min <= e   # 日跨ぎ

# ---------- controller ----------
class Controller:
    def __init__(self):
        self.cache = MqttCache()
        self.conf = DEFAULT_CONF
        self.mtime = None
        self.state = {}        # logic_id -> {cond,out,since,last_out,last_pub}
        self.sun_day = None
        self.sun = (None, None)

    def load(self):
        try:
            m = os.path.getmtime(CONF_FILE)
            if m != self.mtime:
                with open(CONF_FILE) as f:
                    self.conf = json.load(f)
                self.mtime = m
                self._check_ownership()
                names = [l.get("id") for l in self.conf.get("logics", [])]
                print(f"[ctrl] loaded {CONF_FILE}: logics={names}", flush=True)
        except FileNotFoundError:
            if self.mtime is not None or not self.conf["logics"]:
                self.conf = DEFAULT_CONF
        except Exception as e:
            print(f"[ctrl] conf error: {e} (keep previous)", flush=True)
        return self.conf

    def _check_ownership(self):
        owned = {}
        for lg in self.conf.get("logics", []):
            if not lg.get("enabled") or lg.get("dry_run", True):
                continue
            act = lg.get("actuator") or {}
            key = (str(lg.get("house")), act.get("kind"), act.get("ch"), act.get("wid"))
            if key in owned:
                print(f"[ctrl] OWNERSHIP CONFLICT {key}: {lg['id']} vs {owned[key]} → {lg['id']} を dry_run 化", flush=True)
                lg["dry_run"] = True
            else:
                owned[key] = lg["id"]

    def sun_today(self):
        d = datetime.date.today()
        if d != self.sun_day:
            site = self.conf.get("site", {})
            self.sun = sun.sun_times(site.get("lat", 35.0), site.get("lon", 135.0), d, site.get("tz", 9))
            self.sun_day = d
            print(f"[ctrl] {d} sunrise={sun.hhmm(self.sun[0])} sunset={sun.hhmm(self.sun[1])}", flush=True)
        return self.sun

    def eval_swt_rule(self, lg, now_min, sr, ss):
        """最優先でマッチした条件名と action を返す。マッチ無しは (None, off)。"""
        for c in lg.get("conditions", []):
            if not in_window(c.get("window"), now_min, sr, ss):
                continue
            rules = c.get("rules", [])
            results = []
            for r in rules:
                v = get_value(self.cache, r.get("in", ""))
                op = OPS.get(r.get("op", "GE"))
                results.append(v is not None and op is not None and op(v, float(r.get("val", 0))))
            match = c.get("match", "AND")
            ok = (all(results) if match == "AND" else any(results)) if rules else True
            if ok:
                return c.get("name", "?"), c.get("action", {"type": "off"})
        return None, {"type": "off"}

    def duty_output(self, lid, cond, action, now):
        """timer_repeat のデューティ計時。out(0/1) を返す。"""
        st = self.state.setdefault(lid, {"cond": None, "out": 0, "since": now, "last_out": None, "last_pub": 0})
        atype = action.get("type", "off")
        if cond != st["cond"]:                       # 条件が変わった → リセット
            st["cond"] = cond
            st["since"] = now
            st["out"] = 1 if atype == "timer_repeat" else 0
        if atype == "off" or cond is None:
            st["out"] = 0
        elif atype == "on":
            st["out"] = 1
        elif atype == "timer_repeat":
            on = float(action.get("on", 0)); off = float(action.get("off", 0))
            if off <= 0:
                st["out"] = 1
            elif on <= 0:
                st["out"] = 0
            else:
                el = now - st["since"]
                if st["out"] and el >= on:
                    st["out"] = 0; st["since"] = now
                elif (not st["out"]) and el >= off:
                    st["out"] = 1; st["since"] = now
        return st["out"]

    def publish(self, lg, cond, out, now):
        lid = lg["id"]; house = str(lg.get("house"))
        st = self.state[lid]
        changed = (out != st["last_out"]) or (cond != st.get("last_cond"))
        due = (now - st["last_pub"]) >= STATE_REFRESH
        if not (changed or due):
            return
        st["last_out"] = out; st["last_cond"] = cond; st["last_pub"] = now
        # 状態（観測用・retain）
        mqtt_pub(f"agriha/{house}/logic/{lid}/state",
                 json.dumps({"cond": cond, "out": out, "dry_run": bool(lg.get("dry_run", True)),
                             "ts": int(now)}, ensure_ascii=False), retain=True)
        # コマンド
        if lg.get("dry_run", True):
            mqtt_pub(f"agriha/{house}/logic/{lid}/cmd", json.dumps({"value": out, "ts": int(now)}))
        else:
            act = lg.get("actuator") or {}
            if act.get("kind") == "relay":
                mqtt_pub(f"agriha/{house}/relay/{act.get('ch')}/set", json.dumps({"value": out}))
            elif act.get("kind") == "window":
                mqtt_pub(f"agriha/{house}/window/{act.get('wid')}/set", json.dumps({"value": out}))

    def tick(self):
        self.load()
        sr, ss = self.sun_today()
        nowt = datetime.datetime.now()
        now_min = nowt.hour * 60 + nowt.minute + nowt.second / 60.0
        now = time.time()
        for lg in self.conf.get("logics", []):
            if not lg.get("enabled"):
                continue
            if lg.get("type") == "SWT_RULE":
                cond, action = self.eval_swt_rule(lg, now_min, sr, ss)
                out = self.duty_output(lg["id"], cond, action, now)
                self.publish(lg, cond, out, now)
            # 今後: STD_ATMP / STD_CRTN / STD_IRRI をここに追加

    def run(self):
        print(f"[ctrl] start. conf={CONF_FILE}", flush=True)
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"[ctrl] tick error: {e}", flush=True)
            time.sleep(BASE_TICK)

if __name__ == "__main__":
    Controller().run()
