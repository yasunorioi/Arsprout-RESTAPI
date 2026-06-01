# 中央ブレーン setpoint スケジュール設計（agriha）

転用 Unipi 上の **unipi-daemon (Python)** が「各ハウスの目標室温」を時間帯で決め、
`agriha/{house}/setpoint/temp` に publish する設計。ノード（ccm_rp）はこれを受けて
室温を保つ（[[ccm_rp MQTT 制御設計 model B]] / `ccm_rp2350_relay/docs/mqtt-control-design.md`）。

> **役割**: 重い気候判断は中央(Python・編集容易)。ノードは setpoint を保つだけ。
> これは ArSprout の `STD_ATMP`（期間別目標温度マトリクス：`StartHour-1..8` ＋ `TargetTemp-期間-窓`）
> の置き換え＝同等機能を Python の単純スケジュールで持つ。

---

## 1. setpoint の意味
- `setpoint/temp` ＝ **その時間帯にノードが保つべき目標室温℃**。
- ノード側は「室温 > setpoint で開き始め、setpoint+band で全開」（比例＋不感帯）。
  → setpoint ＝ **換気を開始する目標温度**（OGMS の `temp_open`、ArSprout の窓目標温度に相当）。
- band（比例帯）はノード設定。中央は **setpoint（1値）だけ**を送る。

---

## 2. スケジュールモデル

### 2.1 時間帯テーブル（ハウス毎）
ArSprout の期間別目標を踏襲。各ハウスに「開始時刻 → 目標温度」のリスト：

```yaml
houses:
  2:                        # house_id (int)
    schedule:
      - { start: "06:00", temp: 20 }   # 朝（日の出後）
      - { start: "10:00", temp: 25 }   # 日中（高め＝換気多め）
      - { start: "17:00", temp: 20 }   # 夕
      - { start: "21:00", temp: 14 }   # 夜（保温＝閉め気味）
    ramp_min: 30            # 区切りで 30 分かけて滑らかに遷移（段差防止）
    publish_interval_s: 30  # setpoint を 30s ごとに publish（retain）
```
- 現在時刻が属する区間の `temp` を採用（次区間まで）。`ramp_min` で区間境界を線形補間。
- ハウス毎に独立。区間数は可変（ArSprout の8期間に縛られない）。

### 2.2 日の出/日の入り相対（任意・推奨）
ArSprout は緯度経度で日の出を計算（ノード config に Latitude 42.5317/Longitude 141.3616）。
固定時刻に加え、**相対指定**を許す：
```yaml
      - { start: "sunrise+0",  temp: 20 }
      - { start: "sunrise+180m", temp: 25 }
      - { start: "sunset-60m", temp: 18 }
      - { start: "sunset+0",   temp: 14 }
```
- 日の出/日の入りは緯度経度＋日付から計算（`astral` 等）。季節で自動追従。

### 2.3 任意拡張（中央で軽く足せる）
- **気象連動**: 強風/低外気温の日は目標を下げる、晴天(高日射)で換気強化 等（`agriha/farm/weather/*` を購読して setpoint 補正）。
- **季節プロファイル**: 月別にスケジュールを切替。
- **手動上書き**: `agriha/{house}/setpoint/temp/override`（retain）を別途受け、一定時間 or 解除まで優先。
- **CO2/灌水/結露**: これらは setpoint とは別トピック（`relay/{ch}/set` 等）で中央が直接ノードへ指示（§ 別途）。

---

## 3. Publish 仕様
| 項目 | 値 |
|---|---|
| トピック | `agriha/{house}/setpoint/temp` |
| 方向 | brain → broker |
| QoS / retain | 1 / **true**（ノード起動直後に即取得） |
| 周期 | `publish_interval_s`（既定 30s）＋ 値変化時 |
| ペイロード | `{ "value": 22.5, "unit": "C", "ts": 1740000000 }`（agriha §0.4） |

- retain により、ノード再起動・後から接続したノードも最新 setpoint を即取得。
- brain 停止中もノードは最後の setpoint を保持（model B の堅牢性）。

---

## 4. ArSprout STD_ATMP との対応
| ArSprout (STD_ATMP) | 本設計（中央 Python） |
|---|---|
| `StartHour-1..8` | `schedule[].start`（固定時刻 or 日の出相対） |
| `TargetTemp-期間-窓` | `schedule[].temp`（まずハウス1値。窓別は将来 per-slot 化可） |
| `Sensitivity`/比例帯 | **ノード側** band（中央は持たない） |
| `WindSpeed/Rain` 安全 | **ノード側** ローカルセーフティ |
| 期間別 昼夜 | `schedule` の区間で表現（日の出/日の入り相対で自動） |

→ ArSprout のマトリクスの「いつ何℃」だけを Python の素直なスケジュールで持ち、
比例・安全はノードに分離。編集は YAML 1ファイルで完結（ArSprout の UI マトリクス不要）。

---

## 5. 実装スケッチ（unipi-daemon）
`setpoint_scheduler.py`（asyncio タスク、`sensor_loop` 等と並列）:
```python
async def setpoint_loop(mqtt, cfg, geo):
    while True:
        now = local_now()
        for house, h in cfg["houses"].items():
            target = resolve_target(h["schedule"], now, geo, h.get("ramp_min", 0))
            target = apply_weather_bias(target, weather_cache, h)   # 任意
            mqtt.publish(f"agriha/{house}/setpoint/temp",
                         json.dumps({"value": round(target,1), "unit": "C", "ts": int(now.timestamp())}),
                         qos=1, retain=True)
        await asyncio.sleep(min_interval)
```
- `resolve_target`: 区間検索＋ramp 線形補間（日の出相対は `sunrise(geo, date)` で解決）。
- broker 同居（localhost）。設定は `config.yaml`（既存 unipi-daemon と同様）。

---

## 6. 関連
- ノード側受信・制御: `ccm_rp2350_relay/docs/mqtt-control-design.md`（model B）。
- トピック規約: `mqtt-topics.md` §0/§2.5（`setpoint/temp` も §1〜の一覧へ追記推奨）。
- 気象/Unipi HW: `hardware.md`（Misol WH65LP フレーム・DS18B20・MCP23008）。
