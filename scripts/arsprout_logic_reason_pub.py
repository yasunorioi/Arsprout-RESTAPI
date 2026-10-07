#!/usr/bin/env python3
# ArSprout(.81) の側窓判断理由 → agriha/{house}/window/reason/{winid}
#   agri-display(警告帯/理由表示) 用。.81 の気温制御(STD_ATMP) logic の status を 10 秒ごとに
#   読んで、窓ごとの「目標開度・制約(CONSTRAINT)・時間帯(CND_NO)・効いてる警報」を MQTT に出す。
#
#   ★ 読み取り専用 ★  叩くのは GET /api/logic と GET /api/component だけ。
#   /api/component/actuator/operate 等の副作用 GET は絶対に呼ばない(.81 には一切書き込まない)。
#
# 認証: Basic admin:(空パス)。publish は misol_weather_pub と同じく mosquitto_pub を subprocess。
# 方針(handoff 2026-10-07 §2): まず生値をそのまま出して記録し、値が出そろってから日本語化する。
#   未知の CONSTRAINT 値は REASONS_LOG に追記していく。
#
# logic→house の対応(handoff §2): 17=気温制御2(house2 窓32東/33西)、18=気温制御3(house3 窓64東/65西)。
#   窓 id は status の CTRL-<id> キーから動的に拾う(将来の増減に強く)。

import os, sys, time, json, base64, subprocess, urllib.request, urllib.error

ARSPROUT   = os.environ.get("ARSPROUT_URL", "http://192.168.1.81")
ARS_USER   = os.environ.get("ARSPROUT_USER", "admin")
ARS_PASS   = os.environ.get("ARSPROUT_PASS", "")        # 空パス
BROKER     = os.environ.get("MQTT_HOST", "localhost")
POLL_SEC   = int(os.environ.get("POLL_SEC", "10"))      # /api/logic の周期
COMP_SEC   = int(os.environ.get("COMP_SEC", "300"))     # /api/component(警報設定)の再取得周期
HTTP_TO    = 8
REASONS_LOG = os.environ.get("REASONS_LOG", "/home/yasu/shadow-report/logic_reason_seen.log")

LOGIC_MAP  = {17: "2", 18: "3"}                          # logic id -> house_id
WIN_SIDE   = {"32": "east", "33": "west", "64": "east", "65": "west"}

_AUTH = "Basic " + base64.b64encode(f"{ARS_USER}:{ARS_PASS}".encode()).decode()
_seen = set()


def log(msg):
    print(f"[logic-reason] {msg}", flush=True)


def get_json(path):
    """GET <ARSPROUT><path> を JSON で。読み取り専用。副作用パスはここに渡さないこと。"""
    assert path in ("/api/logic", "/api/component"), f"read-only guard: {path}"
    req = urllib.request.Request(ARSPROUT + path,
                                 headers={"Authorization": _AUTH, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TO) as r:
        return json.loads(r.read().decode())


def pub(topic, payload):
    subprocess.run(["mosquitto_pub", "-h", BROKER, "-q", "1", "-r",
                    "-t", topic, "-m", json.dumps(payload, ensure_ascii=False)],
                   timeout=10, check=False)


def record_seen(key, sample):
    """未知の CONSTRAINT 値を一度だけログに残す(日本語化の材料集め)。"""
    if key in _seen:
        return
    _seen.add(key)
    try:
        with open(REASONS_LOG, "a") as f:
            f.write(f"{int(time.time())}\t{key}\t{json.dumps(sample, ensure_ascii=False)}\n")
    except OSError as e:
        log(f"seen-log write failed: {e}")
    log(f"new CONSTRAINT seen: {key}")


def alert_cfg(cfg, no):
    """ActionConstraintNo=<no> のとき効いてる警報の id と評価条件(生値)を config から引く。"""
    aid = cfg.get(f"AlertId-{no}")
    if aid is None:
        return None
    return {
        "alert_id": _int(aid),
        "pos_min": _int(cfg.get(f"AlertPositionMin-{no}")),
        "pos_max": _int(cfg.get(f"AlertPositionMax-{no}")),
        "target": cfg.get(f"AlertTargetId-{no}"),
    }


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return v


def alert_eval(components, alert_id):
    """ALT_RULE component の評価条件(しきい値など)を生で返す。名前フィールドは .81 に無いので
    RuleEvalId/Value をそのまま載せ、日本語名は表示側/後工程で付ける(handoff §2)。"""
    c = next((x for x in components if x.get("id") == alert_id
              and x.get("componentType") == "ALT_RULE"), None)
    if not c:
        return None
    cf = c.get("config", {})
    return {k: cf.get(k) for k in ("RuleEvalId-1", "RuleEvalValue-1", "RuleEvalType-1",
                                   "HoldTime", "Color", "Icon") if k in cf}


def handle(logic, house, components, now):
    st = logic.get("status") or {}
    cfg = logic.get("config") or {}
    cnd = _int(st.get("CND_NO"))
    acn = st.get("ActionConstraintNo")                   # logic 単位の「今効いてる制約番号」
    win_ids = sorted(k.split("-", 1)[1] for k in st if k.startswith("CTRL-"))
    for win in win_ids:
        constraint = st.get(f"CONSTRAINT-{win}") or "none"
        rec = {
            "house": house, "window": win, "side": WIN_SIDE.get(win),
            "target_pct": _int(st.get(f"CTRL-{win}")),
            "step": _int(st.get(f"STEP-{win}")),
            "constraint": constraint,
            "cnd_no": cnd,
            "ts": now,
        }
        if "Alert" in constraint and acn is not None:
            ac = alert_cfg(cfg, acn)
            if ac:
                rec["alert"] = ac
                ev = alert_eval(components, ac["alert_id"])
                if ev:
                    rec["alert"]["eval"] = ev
        record_seen(constraint, rec)
        pub(f"agriha/{house}/window/reason/{win}", rec)


def main():
    log(f"start: {ARSPROUT} -> mqtt {BROKER}  logic={list(LOGIC_MAP)}  poll={POLL_SEC}s (read-only)")
    components, comp_at = [], 0.0
    while True:
        t0 = time.time()
        try:
            if t0 - comp_at > COMP_SEC or not components:
                components = get_json("/api/component")
                comp_at = t0
            logics = get_json("/api/logic")
            now = int(t0)
            for lg in logics:
                house = LOGIC_MAP.get(lg.get("id"))
                if house:
                    handle(lg, house, components, now)
            pub("agriha/farm/sys/logic_reason/online", {"ts": now, "ok": True})
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"poll error: {e}")                      # 失敗時は publish せず次周期(凍った値を流さない)
        dt = time.time() - t0
        time.sleep(max(1.0, POLL_SEC - dt))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
