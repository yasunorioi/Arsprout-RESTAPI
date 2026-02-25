# Arsprout REST API — unipi-daemon ドキュメント

農業施設向け環境制御デーモン **unipi-daemon** の仕様書集。
Raspberry Pi + UniPi 1.1 上で動作し、LLM / LINE Bot からのリレー制御・センサー取得を REST API と MQTT で提供する。

> ソース: `unipi-agri-ha/services/unipi-daemon/`

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────────┐
│                        外部クライアント                              │
│   LLM (Claude Haiku)  /  LINE Bot  /  agriha_control.py (cron)     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HTTP (REST API)
                           │ POST /api/relay/{ch}
                           │ GET  /api/sensors
                           │ GET  /api/status
                           │ POST /api/emergency/clear
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Raspberry Pi (UniPi 1.1)                          │
│                                                                     │
│  ┌──────────────────┐    MQTT publish      ┌───────────────────┐   │
│  │   rest_api       │ ─────────────────── ▶│  MQTT broker      │   │
│  │   (FastAPI :8080)│                      │  (localhost:1883)  │   │
│  └──────────────────┘                      └────────┬──────────┘   │
│                                                     │ MQTT subscribe│
│  ┌──────────────────────────────────────────────────▼──────────┐   │
│  │                      unipi-daemon (asyncio)                  │   │
│  │                                                              │   │
│  │  ┌─────────────┐  ┌────────────┐  ┌──────────┐  ┌────────┐ │   │
│  │  │ sensor_loop │  │ mqtt_loop  │  │gpio_watch│  │ccm_loop│ │   │
│  │  │ (10秒周期)  │  │(relay cmd) │  │(DI07-14) │  │(CCM UDP│ │   │
│  │  └──────┬──────┘  └─────┬──────┘  └────┬─────┘  └───┬────┘ │   │
│  └─────────┼───────────────┼──────────────┼─────────────┼──────┘   │
│            │               │              │             │            │
│            │ I2C           │ I2C          │ I2C(直接)   │ UDP       │
│            ▼               ▼              ▼             ▼            │
│  ┌─────────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
│  │  DS18B20    │  │  MCP23008  │  │  MCP23008│  │ArSprout nodes│  │
│  │  (1-Wire)   │  │  リレー8ch │  │  直接制御│  │(CCM UDP MC)  │  │
│  │  DS2482-100 │  │  (0x20)    │  │  緊急時  │  │224.0.0.1     │  │
│  └─────────────┘  └────────────┘  └──────────┘  │:16520        │  │
│                                                   └──────────────┘  │
│  ┌─────────────────────────────────────┐                           │
│  │  GPIO (/dev/gpiochip0)              │                           │
│  │  DI07-DI14 物理スイッチ (pull-up)   │                           │
│  │  → CommandGate → ロックアウト300秒  │                           │
│  └─────────────────────────────────────┘                           │
│  ┌─────────────────────────────────────┐                           │
│  │  UART (/dev/ttyUSB0, 9600bps)       │                           │
│  │  Misol WH65LP 気象センサー (RS485)  │                           │
│  └─────────────────────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘
```

### 5つのasyncioタスク

| タスク | 役割 | 周期 |
|--------|------|------|
| `sensor_loop` | DS18B20 + Misol WH65LP 読み取り → MQTT publish | 10秒（設定可） |
| `mqtt_loop` | `MqttRelayBridge` でリレー制御コマンド受信 → `_GatedRelay` → I2C | イベント駆動 |
| `gpio_watch` | DI07-DI14 edge event → `CommandGate` 緊急制御 + ロックアウト | イベント駆動 |
| `rest_api` | FastAPI + uvicorn。LINE Bot / LLM 向け REST-MQTT コンバータ | リクエスト駆動 |
| `ccm_loop` | ArSprout UECS-CCM UDP マルチキャスト受信 → MQTT publish | イベント駆動 |

### 緊急オーバーライドの優先順位

```
物理スイッチ (DI07-DI14)
  └─ CommandGate が I2C 直接制御（MQTT/LLM を完全バイパス）
  └─ 300秒ロックアウト
       └─ MQTT 経由の LLM コマンド → ドロップ
       └─ REST API POST /api/relay/{ch} → HTTP 423
```

---

## 仕様書

| ドキュメント | 内容 |
|------------|------|
| [api-spec.md](api-spec.md) | REST API 全4エンドポイント仕様（スキーマ・ステータスコード・認証） |
| [mqtt-topics.md](mqtt-topics.md) | MQTTトピック設計書（publish/subscribe・QoS・ペイロード） |
| [hardware.md](hardware.md) | ハードウェア構成（I2C・GPIO・UART・1-Wire 詳細） |
| [emergency-override.md](emergency-override.md) | 緊急オーバーライド仕様（CommandGate・ロックアウト・状態遷移図） |

---

## 前提環境

### ハードウェア

| 機器 | 説明 |
|------|------|
| Raspberry Pi (3B+ / 4B / Pi Lite) | メインコンピュータ |
| UniPi 1.1 | 拡張ボード（I2C リレー8ch、DI14本、RS485 等） |
| MCP23008 (I2C addr=0x20) | 8chリレー制御IC |
| DS18B20 + DS2482-100 (I2C addr=0x18) | 1-Wire 温度センサー（オプション） |
| Misol WH65LP | RS485 気象センサー（オプション） |

### ソフトウェア

- Python 3.9+
- MQTT ブローカー（Mosquitto 等）: `localhost:1883`

### Pythonライブラリ

| ライブラリ | 用途 | 必須 |
|-----------|------|------|
| `fastapi` | REST API フレームワーク | ✓ |
| `uvicorn` | ASGI サーバー | ✓ |
| `paho-mqtt` | MQTT クライアント | ✓ |
| `pyyaml` | 設定ファイル読み込み | ✓ |
| `smbus2` | I2C（MCP23008 リレー制御） | ✓ |
| `gpiod` (v2) | GPIO edge detection（DI07-DI14）| ✓ |
| `pyserial` | UART（Misol WH65LP 気象センサー） | オプション |

---

## 起動方法

### 設定ファイルの準備

テンプレートをコピーして編集する:

```bash
sudo mkdir -p /etc/agriha
sudo cp services/unipi-daemon/config.yaml /etc/agriha/unipi_daemon.yaml
sudo nano /etc/agriha/unipi_daemon.yaml
```

主要設定項目:

| 設定キー | デフォルト | 説明 |
|---------|-----------|------|
| `daemon.house_id` | `h01` | ハウスID（MQTTトピックプレフィックス） |
| `daemon.sensor_interval_sec` | `10` | センサー読み取り間隔（秒） |
| `mqtt.broker` | `localhost` | MQTTブローカーホスト |
| `mqtt.port` | `1883` | MQTTブローカーポート |
| `rest_api.host` | `0.0.0.0` | REST APIバインドアドレス |
| `rest_api.port` | `8080` | REST APIポート |
| `rest_api.api_key` | `""` | API Key認証（**本番では必ず設定**） |
| `onewire.devices` | `[]` | DS18B20デバイスID一覧（空=自動探索） |
| `uart.weather_port` | `/dev/ttyUSB0` | Misol WH65LPシリアルポート |
| `ccm.enabled` | `true` | UECS-CCM受信の有効/無効 |

### 実行

```bash
cd /home/yasu/unipi-agri-ha/services/unipi-daemon

# 標準起動（設定ファイル: /etc/agriha/unipi_daemon.yaml）
python3 main.py

# 設定ファイルを指定して起動
python3 main.py --config /path/to/config.yaml

# デバッグログ有効
python3 main.py --debug
```

### コマンドライン引数

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--config` | `/etc/agriha/unipi_daemon.yaml` | 設定YAMLのパス |
| `--debug` | `false` | デバッグログ有効化 |

### 停止

```bash
# SIGTERM または SIGINT（Ctrl+C）でgraceful shutdown
kill -TERM <pid>
```

---

## クイックリファレンス

```bash
# リレー3をON（灌水 180秒後に自動OFF）
curl -X POST http://10.10.0.10:8080/api/relay/3 \
  -H "Content-Type: application/json" \
  -d '{"value": 1, "duration_sec": 180, "reason": "灌水"}'

# センサーデータ取得
curl http://10.10.0.10:8080/api/sensors

# デーモン状態・ロックアウト確認
curl http://10.10.0.10:8080/api/status

# 緊急ロックアウト手動解除
curl -X POST http://10.10.0.10:8080/api/emergency/clear
```
