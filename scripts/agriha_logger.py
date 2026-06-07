#!/usr/bin/env python3
# agriha 中央ロガー (pi4) — 依存ゼロ。mosquitto_sub 常駐 + SQLite 蓄積。
#   `mosquitto_sub -t agriha/# -v` を subprocess で読み、数値系列を SQLite に時系列保存。
#   ArSprout CrawlerLog 相当。agriha_web.py の /history が読んでSVGグラフ描画。
# 設計: Arsprout-RESTAPI/mqtt-topics.md（{value,unit,ts} / window / relay / sys）。
#
# SD摩耗対策:
#   - WAL + synchronous=NORMAL、INSERT はメモリにバッファし COMMIT は数十秒に1回（fsync削減）。
#   - 保持: 生データ 30日 → それ以前は 5分平均に集約(samples_agg)し raw を削除。集約は2年で削除。
#
# 系列(series)= topic（+サブキー）。例:
#   agriha/2/sensor/InAirTemp        … {value,unit,ts} の value
#   agriha/2/window/1#pct, #target   … window ペイロードの数値サブキー
#   agriha/2/relay/state#ch1..#ch8   … bool を 0/1 で
# 数値でない値(src 等)と ts/uptime キーは記録しない。

import sqlite3, subprocess, time, json, math, sys, os

BROKER, MQTT_PORT   = "localhost", 1883
DB_FILE             = "/home/pi/agriha_history.db"
TOPIC               = "agriha/#"

COMMIT_INTERVAL     = 30          # sec: バッファをまとめて COMMIT する間隔
RETENTION_INTERVAL  = 3600        # sec: 集約/掃除を走らせる間隔
RAW_RETENTION_DAYS  = 30          # 生データ保持日数
AGG_BUCKET_SEC      = 300         # 集約バケット = 5分平均
AGG_RETENTION_DAYS  = 730         # 集約データ保持日数（約2年）
EXCLUDE_KEYS        = {"ts", "uptime"}   # 系列にしないキー（タイムスタンプ/単調増加）

_series_cache = {}   # key -> sid


def db_init():
    db = sqlite3.connect(DB_FILE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS series(
            id INTEGER PRIMARY KEY, key TEXT UNIQUE, unit TEXT);
        CREATE TABLE IF NOT EXISTS samples(
            sid INTEGER, ts INTEGER, value REAL);
        CREATE INDEX IF NOT EXISTS ix_samples ON samples(sid, ts);
        CREATE TABLE IF NOT EXISTS samples_agg(
            sid INTEGER, ts INTEGER, value REAL, UNIQUE(sid, ts));
        CREATE INDEX IF NOT EXISTS ix_agg ON samples_agg(sid, ts);
    """)
    db.commit()
    for sid, key in db.execute("SELECT id, key FROM series"):
        _series_cache[key] = sid
    return db


def series_id(db, key, unit):
    sid = _series_cache.get(key)
    if sid is not None:
        return sid
    cur = db.execute("INSERT OR IGNORE INTO series(key, unit) VALUES(?,?)", (key, unit))
    if cur.lastrowid:
        sid = cur.lastrowid
    else:
        sid = db.execute("SELECT id FROM series WHERE key=?", (key,)).fetchone()[0]
    _series_cache[key] = sid
    return sid


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def flatten(payload):
    """payload(JSON文字列) -> {サブキー: (value, unit)}。サブキー "" は topic 直下の値。"""
    try:
        j = json.loads(payload)
    except Exception:
        return {}
    out = {}
    if isinstance(j, bool):
        out[""] = (1.0 if j else 0.0, "")
    elif is_num(j):
        out[""] = (float(j), "")
    elif isinstance(j, dict):
        if is_num(j.get("value")):                 # {value,unit,ts} 形式
            out[""] = (float(j["value"]), str(j.get("unit", "")))
        else:                                       # window/relay/sys 等の数値サブキー
            for k, v in j.items():
                if k in EXCLUDE_KEYS:
                    continue
                if isinstance(v, bool):
                    out[k] = (1.0 if v else 0.0, "")
                elif is_num(v):
                    out[k] = (float(v), "")
    return out


def retention(db):
    now = int(time.time())
    raw_cut = now - RAW_RETENTION_DAYS * 86400
    # 30日より古い raw を 5分平均に集約 → agg へ（再実行に強いよう OR IGNORE）
    db.execute(f"""
        INSERT OR IGNORE INTO samples_agg(sid, ts, value)
        SELECT sid, (ts/{AGG_BUCKET_SEC})*{AGG_BUCKET_SEC} AS bucket, AVG(value)
        FROM samples WHERE ts < ?
        GROUP BY sid, bucket
    """, (raw_cut,))
    db.execute("DELETE FROM samples WHERE ts < ?", (raw_cut,))
    db.execute("DELETE FROM samples_agg WHERE ts < ?", (now - AGG_RETENTION_DAYS * 86400,))
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # WAL を切り詰めて肥大防止
    print(f"[logger] retention done (raw<{raw_cut} aggregated)", flush=True)


def run():
    db = db_init()
    buf = []
    last_commit = last_retention = time.time()
    print(f"[logger] DB={DB_FILE} topic={TOPIC}", flush=True)

    while True:   # mosquitto_sub が落ちても外側で再起動
        proc = subprocess.Popen(
            ["mosquitto_sub", "-h", BROKER, "-p", str(MQTT_PORT), "-t", TOPIC, "-v"],
            stdout=subprocess.PIPE, text=True, bufsize=1)
        print("[logger] mosquitto_sub started", flush=True)
        try:
            for line in proc.stdout:
                now = int(time.time())
                topic, _, payload = line.rstrip("\n").partition(" ")
                if not topic or not payload:
                    continue
                for suf, (val, unit) in flatten(payload).items():
                    key = topic + ("#" + suf if suf else "")
                    sid = series_id(db, key, unit)
                    buf.append((sid, now, val))

                t = time.time()
                if buf and (t - last_commit) >= COMMIT_INTERVAL:
                    db.executemany("INSERT INTO samples(sid, ts, value) VALUES(?,?,?)", buf)
                    db.commit()
                    buf.clear()
                    last_commit = t
                if (t - last_retention) >= RETENTION_INTERVAL:
                    retention(db)
                    last_retention = t
        except Exception as e:
            print(f"[logger] reader error: {e}", flush=True)
        finally:
            if buf:
                db.executemany("INSERT INTO samples(sid, ts, value) VALUES(?,?,?)", buf)
                db.commit()
                buf.clear()
            try:
                proc.kill()
            except Exception:
                pass
        print("[logger] mosquitto_sub ended; respawn in 3s", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
