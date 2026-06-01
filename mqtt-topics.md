# MQTTトピック設計書 — unipi-daemon (AgriHA)

> 生成元ソース: `services/unipi-daemon/` 配下の各ファイルを直接読んで記述
> 対象コード: `mqtt_relay_bridge.py`, `sensor_loop.py`, `ccm_receiver.py`, `emergency_override.py`, `main.py`, `config.yaml`

---

## 共通設定

| 項目 | デフォルト値 | 設定箇所 |
|------|------------|---------|
| ブローカー | `localhost` | `config.yaml: mqtt.broker` |
| ポート | `1883` | `config.yaml: mqtt.port` |
| ハウスID (`{house_id}`) | `h01` | `config.yaml: daemon.house_id` |
| keepalive | `60` 秒 | `config.yaml: mqtt.keepalive` |

全トピックのプレフィックスは `agriha/{house_id}/` または `agriha/farm/`。

---

## 0. 設計方針・命名規約（canonical spec）

> **方針**: ArSprout 本体は段階的に廃止し、農場全体を **agriha MQTT** に集約する。
> 各ノード（agri-* センサー / ccm_rp 等のリレー・窓ノード）が直接この体系で
> publish / subscribe する。UECS-CCM ブリッジ（§4）は移行期の互換層で、最終的に不要。
> SNMP のような無秩序化を防ぐため、以下の規約を**必ず**守る。

### 0.1 名前空間（スコープ）
```
agriha/<scope>/<category>/<name>
```
| 要素 | 値 | 意味 |
|------|-----|------|
| `<scope>` | `{house_id}`（例 `h01`） | **ハウス局所**データ（そのハウス内のみ意味を持つ） |
| `<scope>` | `farm` | **農場共有(public)**データ（全ハウスが参照しうる：屋外気象・日射・風） |
| `<category>` | `sensor` / `relay` / `actuator` / `weather` / `emergency` / `ccm` / `sys` | 種別 |
| `<name>` | **UECS-CCM 型名**（`InAirTemp` 等） | 下記 0.3 の正準語彙のみ使用 |

- **局所 vs 共有の判定**: 値が「このハウス固有」か「農場全体で共通」か。
  室内環境(InAir*/Soil*)＝局所、屋外気象(W*)・日射＝共有。
- 局所は `agriha/{house_id}/sensor/{type}`、共有は **`agriha/farm/weather/{type}`**（§2.5）。

### 0.2 1値1トピック原則
- **1 つの物理量＝1 トピック**（`agriha/farm/weather/WWindSpeed` 等）。
  Misol のような多値ブロブ（§2.2）は機器 raw として併存可だが、**正準の消費先は分解型**。
- これにより購読側は必要な値だけ subscribe でき、トピックの意味が一意に定まる。

### 0.3 型名（正準語彙）= UECS-CCM 型を採用
独自名を増やさない。新しい量は UECS 型を踏襲（無ければ UECS 命名規則に倣う）。
- 室内: `InAirTemp` `InAirHumid` `InAirCO2` `InAirAbsHumid` `InAirDP` `InAirHD`
- 土壌: `SoilTemp` `SoilEC` `SoilWC`
- 屋外/農場共有: `WAirTemp` `WAirHumid` `WWindSpeed` `WWindDir` `WRainfallAmt` `WRadiation`(日射) `IntgRadiation`
- アクチュエータ: `VenSdWin` `CirHoriFan` `Irri` `LsCrtn` 等（開度% or ON/OFF）

### 0.4 ペイロード規約（統一 JSON）
センサー/計測値は次の統一形を基本とする（機器固有の多フィールド blob は例外）:
```json
{ "value": 23.5, "unit": "C", "ts": 1740000000 }
```
| キー | 型 | 説明 |
|------|-----|------|
| `value` | number/null | 値（無効時 null） |
| `unit` | string | 単位（UECS 準拠：`C` `%` `ppm` `m s-1` `MJ` `W m-2` 等） |
| `ts` | number | UNIX タイムスタンプ（秒） |

### 0.5 QoS / retain ポリシー
| 用途 | QoS | retain | 理由 |
|------|-----|--------|------|
| センサー値・状態（sensor/weather/relay state） | 1 | **true** | 起動直後に最新値を即取得（特に風速等の安全系） |
| 制御コマンド（relay/{ch}/set 等） | 1 | false | コマンドは一過性、retain 厳禁 |
| CCM ブリッジ（§4・移行期） | 0 | true | 既存実装踏襲 |

---

## 1. リレー制御トピック

### 1.1 リレー状態 (Publish)

```
agriha/{house_id}/relay/state
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 1 |
| retain | **True** |
| パブリッシャー | `MqttRelayBridge` (`mqtt_relay_bridge.py`) |
| タイミング | MQTT接続確立時 / リレー操作後（毎回） |

**ペイロード例:**
```json
{
  "ch1": 0,
  "ch2": 1,
  "ch3": 0,
  "ch4": 0,
  "ch5": 0,
  "ch6": 0,
  "ch7": 0,
  "ch8": 0,
  "ts": 1740000000
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `ch1`〜`ch8` | `int` (0/1) | 各リレーチャンネルの状態 (1=ON, 0=OFF) |
| `ts` | `int` | UNIXタイムスタンプ (秒) |

---

### 1.2 リレー制御コマンド (Subscribe)

```
agriha/{house_id}/relay/+/set
agriha/{house_id}/relay/{ch}/set   （ch = 1〜8）
```

| 項目 | 値 |
|------|-----|
| 方向 | broker → daemon |
| QoS | 1 |
| retain | False（コマンドトピックのため） |
| サブスクライバー | `MqttRelayBridge` (`mqtt_relay_bridge.py`) |

**ペイロード例:**
```json
{
  "value": 1,
  "duration_sec": 180,
  "reason": "灌水 60秒"
}
```

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `value` | `int` (0/1) | ○ | リレー状態 (1=ON, 0=OFF) |
| `duration_sec` | `float` | △ | 自動OFFまでの秒数。`value=1` かつ `>0` の場合のみ有効。省略時 or 0 はタイマーなし |
| `reason` | `str` | × | 操作理由（ログ用）。省略可 |

**チャンネル番号**: `{ch}` は 1〜8 の整数。範囲外は警告ログ出力後スキップ。

**自動OFF挙動**:
- `value=1` かつ `duration_sec > 0` → 指定秒後に自動 OFF しステート publish
- 新コマンド受信時に既存タイマーをキャンセルしてから新タイマー開始

**緊急ロックアウト連携**:
- `CommandGate` がロックアウト中（物理スイッチ ON から 300 秒）は MQTTコマンドをドロップ（リレー操作無効）

---

## 2. センサーデータトピック

センサーループの周期: `config.yaml: daemon.sensor_interval_sec`（デフォルト **10 秒**）

### 2.1 DS18B20 温度センサー (Publish)

```
agriha/{house_id}/sensor/DS18B20
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 1 |
| retain | **True** |
| パブリッシャー | `SensorLoop` (`sensor_loop.py`) |
| センサー | DS18B20 1-Wire 温度センサー（複数台対応） |
| デバイス指定 | `config.yaml: onewire.devices`（空リストなら自動探索） |

**ペイロード例（デバイス1台分）:**
```json
{
  "device_id": "28-00000de13271",
  "temperature_c": 25.625,
  "timestamp": 1740000000.123
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `device_id` | `str` | DS18B20デバイスID（`/sys/bus/w1/devices/` のディレクトリ名） |
| `temperature_c` | `float` | 測定温度 (℃) |
| `timestamp` | `float` | UNIXタイムスタンプ (秒、小数あり) |

> 複数デバイスがある場合、デバイスごとに個別に同トピックへ publish される（デバイスIDで区別）。

---

### 2.2 Misol WH65LP 気象センサー (Publish)

```
agriha/farm/weather/misol
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 1 |
| retain | **True** |
| パブリッシャー | `SensorLoop` (`sensor_loop.py`) |
| センサー | Misol WH65LP（UART RS485接続） |
| シリアルポート | `config.yaml: uart.weather_port`（デフォルト `/dev/ttyUSB0`） |
| ボーレート | `config.yaml: uart.weather_baud`（デフォルト `9600` bps） |

> トピックは `{house_id}` を含まず `agriha/farm/weather/misol` 固定（`sensor_loop.py` L.56 直接定義）。

**ペイロード例:**
```json
{
  "wind_dir_deg": 270,
  "temperature_c": 4.6,
  "humidity_pct": 62,
  "wind_speed_ms": 4.9,
  "gust_speed_ms": 6.72,
  "rainfall_mm": 0.0,
  "uv_wm2": 0.0,
  "light_lux": 12300.5,
  "pressure_hpa": 1013.2,
  "battery_low": false,
  "timestamp": 1740000000.123
}
```

| フィールド | 型 | センチネル(無効値) | 説明 |
|-----------|-----|-----------------|------|
| `wind_dir_deg` | `int` or `null` | `0x1FF`→`null` | 風向 (0〜359 °) |
| `temperature_c` | `float` or `null` | `0x7FF`→`null` | 気温 (℃)、小数1桁 |
| `humidity_pct` | `int` | なし | 相対湿度 (%) |
| `wind_speed_ms` | `float` or `null` | `0x1FF`→`null` | 風速 (m/s)、小数2桁 |
| `gust_speed_ms` | `float` or `null` | `0xFF`→`null` | 突風速度 (m/s)、小数2桁 |
| `rainfall_mm` | `float` | なし | 累積降雨量 (mm)、小数1桁 |
| `uv_wm2` | `float` or `null` | `0xFFFF`→`null` | UV強度 (W/m²)、小数1桁 |
| `light_lux` | `float` or `null` | `0xFFFFFF`→`null` | 照度 (lux)、小数1桁 |
| `pressure_hpa` | `float` or `null` | 基本フレーム時→`null` | 気圧 (hPa)、小数1桁。拡張フレーム(21バイト)時のみ値あり |
| `battery_low` | `bool` | なし | バッテリー低下フラグ (true=低下) |
| `timestamp` | `float` | なし | UNIXタイムスタンプ (秒)。`sensor_loop.py` が `time.time()` で追加 |

> `wh65lp_reader.parse_frame()` の返却10フィールドに `sensor_loop.py` L.129 が `timestamp` を追記して publish（全11フィールド）。
> Misol フレームは基本17バイトと拡張21バイト（気圧付き）の2種類あり、`pressure_hpa` は拡張フレーム時のみ値が入る。
> Misol フレームのシンク待ちタイムアウトは 20 秒（pyserial 未インストール時は無効）。

---

### 2.5 農場共有（public）気象センサー (Publish) — canonical

屋外気象・日射・風など **農場全体で共有**する量の正準置き場（§0.1 の `farm` スコープ、§0.2 の 1値1トピック）。
**機種非依存**：Misol（§2.2 の blob を分解）でも専用風速計でも agri-rain/agri-solar でも、測った者が該当 type トピックへ publish する。

```
agriha/farm/weather/{type}
```

| 項目 | 値 |
|------|-----|
| 方向 | 各センサーノード → broker |
| QoS | 1 |
| retain | **True**（安全系の即時取得のため必須） |
| `{type}` | `WAirTemp` `WAirHumid` `WWindSpeed` `WWindDir` `WRainfallAmt` `WRadiation`(日射) `IntgRadiation` 等（§0.3） |

**トピック例 / ペイロード（§0.4 統一形）:**
```
agriha/farm/weather/WWindSpeed   → {"value": 4.9, "unit": "m s-1", "ts": 1740000000}
agriha/farm/weather/WWindDir     → {"value": 270, "unit": "deg",   "ts": 1740000000}
agriha/farm/weather/WAirTemp     → {"value": 4.6, "unit": "C",     "ts": 1740000000}
agriha/farm/weather/WRainfallAmt → {"value": 0.0, "unit": "mm",    "ts": 1740000000}
agriha/farm/weather/WRadiation   → {"value": 612, "unit": "W m-2", "ts": 1740000000}
```

**消費例（各ハウスの制御ノード = ccm_rp 等）:**
- `agriha/{house_id}/sensor/#`（自ハウス局所）＋ `agriha/farm/weather/#`（農場共有）の両方を subscribe。
- `WWindSpeed` → 強風時に側窓を強制クローズ（安全）、`WRadiation` → 日射連動灌水、等。

> 既存の `agriha/farm/weather/misol`（§2.2 多値 blob）は機器 raw として併存可。
> 正準の消費は本 §2.5 の分解型トピックを使う（publisher 側で分解 publish するか、ブリッジで変換）。

---

### 2.6 目標室温 setpoint (Publish) — 中央ブレーン

中央ブレーン（unipi-daemon）が**時間帯別の目標室温**を各ハウスへ配信する。
リレー/窓ノード（ccm_rp 等）がこれを subscribe し、室温を保つよう側窓を制御する。
スケジュール設計の詳細は `setpoint-schedule-design.md`、ノード側制御は
`ccm_rp2350_relay/docs/mqtt-control-design.md`（model B）。

```
agriha/{house_id}/setpoint/temp
```

| 項目 | 値 |
|------|-----|
| 方向 | brain(daemon) → broker → ノード |
| QoS | 1 |
| retain | **True**（ノード起動/再接続時に即取得、ブレーン停止中も保持） |
| パブリッシャー | `setpoint_scheduler`（unipi-daemon, 予定） |
| タイミング | `publish_interval_s`（既定 30s）＋ 値変化時 |

**ペイロード（§0.4 統一形）:**
```json
{ "value": 22.5, "unit": "C", "ts": 1740000000 }
```
- `value` ＝ その時間帯にノードが保つべき目標室温℃（＝換気開始温度）。
- 比例帯(band)・強風/降雨セーフティは**ノード側**が持つ（中央は setpoint 1値のみ）。
- ArSprout `STD_ATMP`（期間別目標温度）の置き換え。

---

## 3. 緊急制御トピック (Emergency)

### 3.1 緊急オーバーライド通知 (Publish)

```
agriha/{house_id}/emergency/override
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 1 |
| retain | **True** |
| パブリッシャー | `CommandGate` (`emergency_override.py`) |
| トリガー | UniPi 1.1 物理スイッチ（DI07〜DI14）エッジ検出時 |

**ペイロード例:**
```json
{
  "di_pin": 9,
  "relay_ch": 3,
  "state": true,
  "timestamp": 1740000000.456,
  "lockout_sec": 300
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `di_pin` | `int` | 検出した DI ピン番号 (7〜14) |
| `relay_ch` | `int` | 操作されたリレーチャンネル (1〜8) |
| `state` | `bool` | リレー状態 (true=ON, false=OFF) |
| `timestamp` | `float` | UNIXタイムスタンプ (秒) |
| `lockout_sec` | `int` | ロックアウト秒数（スイッチON時=300、OFF時=0） |

**DI → リレーチャンネルマッピング:**

| DI ピン | リレー ch | MCP23008 ビット |
|---------|----------|----------------|
| DI07 | ch1 | GP7 (bit7, 0x80) |
| DI08 | ch2 | GP6 (bit6, 0x40) |
| DI09 | ch3 | GP5 (bit5, 0x20) |
| DI10 | ch4 | GP4 (bit4, 0x10) |
| DI11 | ch5 | GP3 (bit3, 0x08) |
| DI12 | ch6 | GP2 (bit2, 0x04) |
| DI13 | ch7 | GP1 (bit1, 0x02) |
| DI14 | ch8 | GP0 (bit0, 0x01) |

**ロックアウト動作**:
- スイッチ ON → I2C 直接リレー制御（MqttRelayBridge を経由しない） + MQTT publish + **300秒ロックアウト開始**
- スイッチ OFF → I2C 直接リレー制御 + MQTT publish（ロックアウト更新なし）
- ロックアウト中: MQTT 経由の LLM コマンドは `CommandGate.gate()` でドロップ

---

## 4. CCM（UECS-CCM）トピック

ArSprout ノード等が UDP マルチキャスト（`224.0.0.1:16520`）で送信する UECS-CCM データを
`CcmReceiver` (`ccm_receiver.py`) が受信し MQTT に変換してpublish する。

有効/無効: `config.yaml: ccm.enabled`（デフォルト `true`）

### 4.1 センサートピック (Publish)

```
agriha/{house_id}/ccm/sensor/{ccm_type}
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 0 |
| retain | **True** |
| ccm_type 例 | `InAirTemp`, `InAirHumid`, `InAirCO2`, `SoilTemp`, `InRadiation`, `SoilEC`, `SoilWC`, `Pulse`, `InAirHD`, `InAirAbsHumid`, `InAirDP`, `IntgRadiation` |

### 4.2 アクチュエータートピック (Publish)

```
agriha/{house_id}/ccm/actuator/{ccm_type}
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 0 |
| retain | **True** |
| ccm_type 例 | `Irri`, `VenFan`, `CirHoriFan`, `AirHeatBurn`, `AirHeatHP`, `CO2Burn`, `VenRfWin`, `VenSdWin`, `ThCrtn`, `LsCrtn`, `AirCoolHP`, `AirHumFog` |

### 4.3 気象トピック (Publish)

```
agriha/{house_id}/ccm/weather/{ccm_type}
```

| 項目 | 値 |
|------|-----|
| 方向 | daemon → broker |
| QoS | 0 |
| retain | **True** |
| ccm_type 例 | `WAirTemp`, `WAirHumid`, `WWindSpeed`, `WWindDir16`, `WRainfall`, `WRainfallAmt`, `WLUX` |

### 4.4 その他（分類不能）(Publish)

```
agriha/{house_id}/ccm/other/{ccm_type}
```

SENSOR_TYPES / ACTUATOR_TYPES / WEATHER_TYPES いずれにも該当しない ccm_type が来た場合。

### 4.5 CCM 共通ペイロード

全 CCM トピックで共通のペイロード形式:

```json
{
  "ccm_type": "InAirTemp",
  "value": 26.3,
  "room": 1,
  "region": 1,
  "order": 1,
  "priority": 29,
  "level": "S",
  "source_ip": "192.168.1.100",
  "timestamp": "2026-02-25T03:00:00+00:00"
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `ccm_type` | `str` | CCMタイプ名（CCMサフィックス `.mC`/`.cMC`/`.MC` は除去済み） |
| `value` | `float` or `str` | 測定値（数値に変換できない場合は文字列のまま） |
| `room` | `int` | 部屋番号（UECS-CCM `room` 属性） |
| `region` | `int` | 区域番号（UECS-CCM `region` 属性） |
| `order` | `int` | 順番（UECS-CCM `order` 属性） |
| `priority` | `int` | 優先度（UECS-CCM `priority` 属性、デフォルト 29） |
| `level` | `str` | レベル（UECS-CCM `lv` 属性、デフォルト `"S"`） |
| `source_ip` | `str` | 送信元IPアドレス |
| `timestamp` | `str` | ISO 8601 UTC タイムスタンプ（受信時刻） |

**CCM 受信プロトコル**:
- UDP マルチキャスト: `224.0.0.1:16520`
- バッファサイズ: 4096 bytes
- ペイロード形式: UECS XML（`<DATA type="InAirTemp.mC" room="1" ...>26.3</DATA>` 形式）

---

## 5. トピック一覧サマリ

| トピック | 方向 | QoS | retain | ソース |
|---------|------|-----|--------|--------|
| `agriha/{house_id}/relay/state` | daemon→broker | 1 | ✓ | `mqtt_relay_bridge.py` |
| `agriha/{house_id}/relay/{ch}/set` | broker→daemon | 1 | ✗ | `mqtt_relay_bridge.py` |
| `agriha/{house_id}/sensor/DS18B20` | daemon→broker | 1 | ✓ | `sensor_loop.py` |
| `agriha/farm/weather/misol` | daemon→broker | 1 | ✓ | `sensor_loop.py` |
| `agriha/farm/weather/{type}` | sensor→broker | 1 | ✓ | §2.5 canonical |
| `agriha/{house_id}/setpoint/temp` | brain→broker→node | 1 | ✓ | `setpoint_scheduler`（予定） |
| `agriha/{house_id}/emergency/override` | daemon→broker | 1 | ✓ | `emergency_override.py` |
| `agriha/{house_id}/ccm/sensor/{ccm_type}` | daemon→broker | 0 | ✓ | `ccm_receiver.py` |
| `agriha/{house_id}/ccm/actuator/{ccm_type}` | daemon→broker | 0 | ✓ | `ccm_receiver.py` |
| `agriha/{house_id}/ccm/weather/{ccm_type}` | daemon→broker | 0 | ✓ | `ccm_receiver.py` |
| `agriha/{house_id}/ccm/other/{ccm_type}` | daemon→broker | 0 | ✓ | `ccm_receiver.py` |

---

## 6. MQTTクライアントID一覧

`main.py` で生成される MQTT クライアントと、その用途:

| client_id | 担当 | 説明 |
|-----------|------|------|
| `unipi-daemon` (`config.yaml: mqtt.client_id`) | `MqttRelayBridge` | リレー制御コマンドの Subscribe / relay/state の Publish |
| `unipi-daemon-sensor` | `SensorLoop` / `CcmReceiver` | DS18B20, Misol, CCM データの Publish |
| `unipi-daemon-emergency` | `CommandGate` | emergency/override の Publish |
