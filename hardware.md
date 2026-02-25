# Arsprout RESTAPI ハードウェア構成リファレンス

ソース: `unipi-agri-ha/services/unipi-daemon/` 配下の実装から生成

## 全体構成図

```
UniPi 1.1 (Raspberry Pi ベース)
│
├── I2C バス (/dev/i2c-1, bus=1)
│   ├── MCP23008  addr=0x20 ──────────── 8ch リレー制御
│   └── DS2482-100 addr=0x18 ─────────── 1-Wire I2Cブリッジ
│       └── DS18B20 (28-00000de13271) ── 温度センサー (1-Wire)
│
├── GPIO (/dev/gpiochip0)
│   └── DI07-DI14 (pull-up, BOTH edge) ─ 物理スイッチ入力
│
└── UART (/dev/ttyUSB0, 9600bps)
    └── Misol WH65LP ─────────────────── RS485 気象センサー

MQTT ブローカー: localhost:1883
REST API: 0.0.0.0:8080 (FastAPI + uvicorn)
```

---

## 1. MCP23008 8ch リレー制御 (I2C)

**実装**: `i2c_relay.py` (`MCP23008Relay` クラス)
**ライブラリ**: `smbus2`

### 接続設定

| 項目 | 値 |
|------|-----|
| I2C バス | 1 (`/dev/i2c-1`) |
| I2C アドレス | 0x20 |

### レジスタマップ

| アドレス | 名称 | 説明 |
|---------|------|------|
| 0x00 | IODIR | I/O 方向設定 (0=出力, 1=入力)。初期化時に 0x00 (全出力) を書き込む |
| 0x09 | GPIO | GPIO ポート状態読み取り |
| 0x0A | OLAT | 出力ラッチ書き込み (現在の出力値を保持するシャドウレジスタとしても使用) |

### チャンネル / ビットマッピング (逆順配線)

配線の都合で **ch番号とbit位置が逆順** になっている。
変換式: `bitmask = 1 << (8 - ch)`

| チャンネル | MCP23008 ピン | ビットマスク |
|-----------|-------------|------------|
| ch1 | GP7 | 0x80 (bit7) |
| ch2 | GP6 | 0x40 (bit6) |
| ch3 | GP5 | 0x20 (bit5) |
| ch4 | GP4 | 0x10 (bit4) |
| ch5 | GP3 | 0x08 (bit3) |
| ch6 | GP2 | 0x04 (bit2) |
| ch7 | GP1 | 0x02 (bit1) |
| ch8 | GP0 | 0x01 (bit0) |

### 制御 API

```python
from i2c_relay import MCP23008Relay

with MCP23008Relay(bus_num=1, addr=0x20) as relay:
    relay.set_relay(1, True)         # ch1 ON
    relay.set_relay(1, False)        # ch1 OFF
    state = relay.get_state()        # 全8ch の OLAT レジスタ値 (int) を返す
    on = relay.get_relay(2)          # ch2 の状態 (True/False)
    relay.set_all(0b10000001)        # ch1, ch8 ON（ビットマスク一括設定）
    relay.all_off()                  # 全チャンネル OFF (set_all(0x00))
```

シャドウレジスタ `_olat` により、毎回 I2C 読み取りをせずに現在の出力値を保持する。

---

## 2. DS18B20 温度センサー (1-Wire)

**実装**: `ds18b20.py` (`DS18B20` クラス)

### 接続方式

```
DS18B20 (温度センサー)
  └── 1-Wire バス
      └── DS2482-100 (I2C-to-1-Wire ブリッジ, I2C addr=0x18)
          └── /dev/i2c-1
```

### 前提条件

- `/boot/config.txt` に `dtoverlay=ds2482` が設定済み
- Linux カーネルモジュール `w1_therm` がロード済み

### デバイス情報

| 項目 | 値 |
|------|-----|
| デフォルトデバイス ID | `28-00000de13271` |
| sysfs パス | `/sys/bus/w1/devices/{device_id}/temperature` |
| 値フォーマット | millidegrees Celsius (例: `24500` → 24.5°C) |
| デバイス検索パターン | `/sys/bus/w1/devices/28-*` |

### 読み取り API

```python
from ds18b20 import DS18B20

# 特定デバイスを直接指定
sensor = DS18B20(device_id="28-00000de13271")
temp_c = sensor.read_celsius()   # float, 例: 24.5

# 接続されている全 DS18B20 を自動検索
sensors = DS18B20.discover()     # list[DS18B20]
for s in sensors:
    print(s.device_id, s.read_celsius())
```

---

## 3. GPIO デジタル入力 (物理スイッチ DI07-DI14)

**実装**: `gpio_watch.py` (`GPIOWatcher` クラス)
**ライブラリ**: `gpiod v2`

### GPIO ピンマッピング

| DI 番号 | gpiochip0 line offset | 緊急オーバーライド用リレー ch |
|---------|----------------------|--------------------------|
| DI07 | GPIO11 | relay ch1 |
| DI08 | GPIO7  | relay ch2 |
| DI09 | GPIO8  | relay ch3 |
| DI10 | GPIO9  | relay ch4 |
| DI11 | GPIO25 | relay ch5 |
| DI12 | GPIO10 | relay ch6 |
| DI13 | GPIO31 | relay ch7 |
| DI14 | GPIO30 | relay ch8 |

### 電気的特性とイベント変換

- Pull-up 付き配線: 通常 HIGH、スイッチ ON で LOW
- gpiod edge detection 設定: `Edge.BOTH` (立上り・立下り両方)

```
FALLING_EDGE (GPIO HIGH→LOW, スイッチ閉じる) → GPIOEvent.value = 1 (スイッチ ON)
RISING_EDGE  (GPIO LOW→HIGH, スイッチ開く)   → GPIOEvent.value = 0 (スイッチ OFF)
```

### イベント検知方式 (asyncio 統合)

```
gpiod LineRequest.fd (ファイルディスクリプタ)
  └── asyncio loop.add_reader(fd, _on_readable)
      └── _on_readable() が呼ばれる (fd に読み取り可能データあり)
          └── request.read_edge_events() → GPIOEvent リスト
              └── callback(event) を呼び出す
```

consumer 名: `"unipi-daemon-gpio-watch"`

### GPIOEvent データ構造

```python
@dataclass
class GPIOEvent:
    di_pin: int       # DI07-DI14 のピン番号 (7-14)
    gpio_line: int    # gpiochip0 の line offset
    value: int        # 1=スイッチ ON, 0=スイッチ OFF
    timestamp_ns: int # イベントタイムスタンプ (nanoseconds)
```

### 使用 API

```python
from gpio_watch import GPIOWatcher

watcher = GPIOWatcher(
    chip_path="/dev/gpiochip0",
    di_pins=[7, 8, 9, 10, 11, 12, 13, 14],  # None で全 DI07-DI14
    callback=my_callback,                      # GPIOEvent を受け取る関数
)
await watcher.watch()   # asyncio タスクとして実行、CancelledError でシャットダウン
```

---

## 4. Misol WH65LP 気象センサー (RS485/UART)

**実装**: `wh65lp_reader.py`
**ライブラリ**: `pyserial`

### 接続設定

| 項目 | 値 |
|------|-----|
| ポート | `/dev/ttyUSB0` |
| ボーレート | 9600 |
| データビット | 8 |
| パリティ | None |
| ストップビット | 1 |
| 送信方式 | プッシュ型（センサー側から約16秒間隔で自動送信）|

### フレーム構造

| フレーム種別 | バイト長 | 備考 |
|------------|---------|------|
| 基本フレーム | 17 バイト | byte 0 (sync) ～ byte 16 (チェックサム) |
| 拡張フレーム | 21 バイト | 基本フレーム + byte 17-19 (気圧) + byte 20 |

同期バイト: `0x24` (フレーム先頭を示す)
チェックサム: `sum(data[0:16]) & 0xFF == data[16]`

### バイトマップ

| バイト位置 | フィールド | ビット操作 | 変換式 | 単位 | センチネル (無効) |
|-----------|----------|---------|--------|------|-----------------|
| 0 | Sync | - | 固定 `0x24` | - | - |
| 1 | センサー ID | - | - | - | - |
| 2, 3[bit7] | 風向 | `data[2] \| ((data[3] & 0x80) << 1)` | 9-bit raw → deg | deg | `0x1FF` (511) |
| 4, 3[bits2:0] | 温度 | `data[4] \| ((data[3] & 0x07) << 8)` | `(raw - 400) / 10.0` | °C | `0x7FF` (2047) |
| 5 | 湿度 | - | `data[5]` | % | - |
| 6, 3[bit4] | 風速 | `data[6] \| ((data[3] & 0x10) << 4)` | `(raw / 8.0) * 1.12` | m/s | `0x1FF` (511) |
| 7 | 突風速 | - | `data[7] * 1.12` | m/s | `0xFF` (255) |
| 8-9 | 降雨量 (累積) | - | `((data[8]<<8)\|data[9]) * 0.3` | mm | - |
| 10-11 | UV 強度 | - | `((data[10]<<8)\|data[11]) / 10.0` | W/m² | `0xFFFF` (65535) |
| 12-14 | 照度 | - | `((data[12]<<16)\|(data[13]<<8)\|data[14]) / 10.0` | lux | `0xFFFFFF` (16777215) |
| 3[bit3] | バッテリー低下 | `bool(data[3] & 0x08)` | - | bool | - |
| 16 | チェックサム | - | `sum(data[0:16]) & 0xFF` | - | - |
| 17-19 | 気圧 (拡張) | - | `((data[17]<<16)\|(data[18]<<8)\|data[19]) / 100.0` | hPa | - (拡張フレームのみ) |

センチネル値のフィールドは `None` を返す。

### フレーム受信シーケンス

```
1. シリアルポートを 1 バイトずつ読み込む
2. 0x24 (sync byte) を検出するまで読み捨てる
3. 残り 16 バイトを読み込み、合計 17 バイトを収集
4. チェックサム検証 (失敗時は None を返す)
5. 100ms 以内に追加 4 バイトがあれば 21 バイト拡張フレームとして処理
```

### parse_frame() 戻り値

```python
{
    "wind_dir_deg": int | None,      # 風向 (度)
    "temperature_c": float | None,   # 気温 (°C)
    "humidity_pct": int,             # 湿度 (%)
    "wind_speed_ms": float | None,   # 風速 (m/s)
    "gust_speed_ms": float | None,   # 突風速 (m/s)
    "rainfall_mm": float,            # 降雨量累積 (mm)
    "uv_wm2": float | None,          # UV 強度 (W/m²)
    "light_lux": float | None,       # 照度 (lux)
    "pressure_hpa": float | None,    # 気圧 (hPa, 拡張フレームのみ)
    "battery_low": bool,             # バッテリー低下フラグ
}
```

---

## 設定ファイル (config.yaml)

テンプレート: `services/unipi-daemon/config.yaml`
本番パス: `/etc/agriha/unipi_daemon.yaml`

```yaml
daemon:
  house_id: h01                  # ハウスID (MQTT トピックプレフィックスに使用)
  sensor_interval_sec: 10        # センサー読み取り間隔 (秒)

mqtt:
  broker: localhost
  port: 1883
  client_id: unipi-daemon
  keepalive: 60

i2c:
  bus: 1                         # /dev/i2c-1
  mcp23008_addr: 0x20

gpio:
  chip: /dev/gpiochip0
  di_lines: [7, 8, 9, 10, 11, 12, 13, 14]

onewire:
  devices: []                    # DS18B20 デバイス ID 一覧 (ls /sys/bus/w1/devices/ で確認)

uart:
  weather_port: /dev/ttyUSB0
  weather_baud: 9600

ccm:
  enabled: true
  multicast_addr: "224.0.0.1"   # UECS-CCM マルチキャスト
  multicast_port: 16520

rest_api:
  host: 0.0.0.0
  port: 8080
  api_key: ""                    # 本番では必ず設定すること
```
