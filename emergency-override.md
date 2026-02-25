# 緊急オーバーライド仕様 (CommandGate パターン)

ソース: `emergency_override.py`, `gpio_watch.py`, `mqtt_relay_bridge.py`, `rest_api.py`, `main.py`

## 概要

物理スイッチ (UniPi DI07-DI14) を ON にすることで、LLM (AI) からのリレー制御コマンドを遮断し、物理的に安全な状態を確保する機能。

- **DirectControl**: 緊急時は MCP23008Relay を直接操作 (MQTT/LLM 経路を完全バイパス)
- **CommandGate**: LLM コマンドの通過可否を `gate()` メソッドで制御
- **ロックアウト期間**: 300 秒 (`LOCKOUT_SECONDS = 300`)

---

## DI → リレーチャンネル マッピング

`emergency_override.py` の `DI_RELAY_MAP`:

| DI 番号 | gpiochip0 | リレー ch | MCP23008 ピン | ビットマスク |
|---------|-----------|---------|-------------|------------|
| DI07 | GPIO11 | ch1 | GP7 | 0x80 |
| DI08 | GPIO7  | ch2 | GP6 | 0x40 |
| DI09 | GPIO8  | ch3 | GP5 | 0x20 |
| DI10 | GPIO9  | ch4 | GP4 | 0x10 |
| DI11 | GPIO25 | ch5 | GP3 | 0x08 |
| DI12 | GPIO10 | ch6 | GP2 | 0x04 |
| DI13 | GPIO31 | ch7 | GP1 | 0x02 |
| DI14 | GPIO30 | ch8 | GP0 | 0x01 |

---

## 緊急割り込みフロー

### スイッチ ON (FALLING_EDGE) の場合

```
物理スイッチ ON (DI07-DI14 いずれか)
  │
  ▼ GPIO FALLING_EDGE 検出
    gpiod v2: loop.add_reader(watcher.fd, _on_readable)
    _on_readable() → request.read_edge_events()
  │
  ▼ GPIOEvent 生成
    GPIOEvent(di_pin=7, gpio_line=11, value=1, timestamp_ns=...)
  │
  ▼ CommandGate.handle_gpio_event(event)
  │
  ├─ [1] MCP23008Relay.set_relay(ch, True)
  │       I2C 直接制御 — MqttRelayBridge を経由しない
  │
  ├─ [2] MQTT publish
  │       topic:   agriha/{house_id}/emergency/override
  │       payload: {"di_pin": 7, "relay_ch": 1, "state": true,
  │                 "timestamp": 1740000000.0, "lockout_sec": 300}
  │       QoS=1, retain=True
  │
  └─ [3] ロックアウト開始
          _lockout_until = time.monotonic() + 300
          → 以降 300秒間、LLM コマンドをドロップ
```

### スイッチ OFF (RISING_EDGE) の場合

```
物理スイッチ OFF
  │
  ▼ GPIO RISING_EDGE 検出 → GPIOEvent(value=0)
  │
  ▼ CommandGate.handle_gpio_event(event)
  │
  ├─ [1] MCP23008Relay.set_relay(ch, False)  ← リレー OFF
  ├─ [2] MQTT publish (state=false, lockout_sec=0)
  └─ [3] ロックアウト更新なし (既存ロックアウトは継続)
```

---

## CommandGate パターン仕様

### ロックアウト状態の管理

```python
class CommandGate:
    _lockout_until: float  # time.monotonic() ベース

    def is_locked_out(self) -> bool:
        return time.monotonic() < self._lockout_until

    def remaining_lockout(self) -> float:
        return max(0.0, self._lockout_until - time.monotonic())

    def clear_lockout(self) -> None:
        self._lockout_until = 0.0
```

- タイムベース: `time.monotonic()` (システム時刻変更の影響を受けない)
- スイッチ OFF では `_lockout_until` を更新しない (ON 時のみ 300 秒延長)

### リレーコマンドの遮断方式

#### 経路 1: MQTT (LLM コマンド) → `_GatedRelay`

`main.py` の `_GatedRelay` アダプタが MQTT コマンドをゲーティングする:

```
MQTT agriha/{house_id}/relay/{ch}/set
  │
  ▼ MqttRelayBridge._on_message()
  │
  ▼ _GatedRelay.set_relay(ch, on)
  │
  ▼ CommandGate.gate(relay.set_relay, ch, on)
      ├─ is_locked_out() = True
      │   → ログ警告: "LLM command rejected by CommandGate (lockout XXs remaining)"
      │   → return False (コマンドドロップ)
      │
      └─ is_locked_out() = False
          → relay.set_relay(ch, on) 実行
          → return True
```

`gate()` メソッドシグネチャ:
```python
def gate(self, command_fn: Callable, *args: Any, **kwargs: Any) -> bool:
    # ロックアウト中: False を返す (command_fn は呼ばない)
    # 通常時: command_fn(*args, **kwargs) を実行して True を返す
```

#### 経路 2: REST API → `CommandGate.is_locked_out()` で直接チェック

```
POST /api/relay/{ch}
  │
  ▼ RestApi.set_relay()
  │
  ├─ gate.is_locked_out() = True
  │   → HTTP 423 Locked
  │     {"error": "locked_out",
  │      "message": "緊急スイッチによりロックアウト中",
  │      "remaining_sec": 275.3}
  │
  └─ gate.is_locked_out() = False
      → MQTT publish: agriha/{house_id}/relay/{ch}/set
      → MqttRelayBridge → _GatedRelay → リレー制御
      → HTTP 202 Accepted {"ch": 1, "value": 1, "queued": true}
```

---

## ロックアウト解除手順

### 1. タイムアウト (自動解除)

スイッチ ON から **300 秒後**に自動解除される。
実装: `_lockout_until = time.monotonic() + 300` の時点が過ぎると `is_locked_out()` が False を返す。
明示的なタイマーや強制 OFF 処理はなく、`time.monotonic()` との比較のみで判定する。

### 2. REST API による手動解除

```bash
# ロックアウト解除
curl -X POST http://10.10.0.10:8080/api/emergency/clear

# API キー認証が設定されている場合
curl -X POST http://10.10.0.10:8080/api/emergency/clear \
     -H "X-API-Key: {api_key}"
```

レスポンス (正常):
```json
{"cleared": true, "was_locked_out": true}
```

実装: `gate.clear_lockout()` → `_lockout_until = 0.0`

### 3. ロックアウト状態の確認

```bash
# デーモン状態確認 (ロックアウト残り秒数含む)
curl http://10.10.0.10:8080/api/status
```

レスポンス例:
```json
{
    "house_id": "h01",
    "uptime_sec": 3600,
    "locked_out": true,
    "lockout_remaining_sec": 250.3,
    "relay_state": {"ch1": true, "ch2": false, ...},
    "ts": 1740000000.0
}
```

> **注意**: MQTT によるロックアウト解除はコード上に実装されていない。
> 解除手段はタイムアウト (300秒) と `POST /api/emergency/clear` のみ。

---

## 状態遷移図

```
┌───────────────────────────────────────────────┐
│                   NORMAL                      │
│   is_locked_out() = False                     │
│   LLM コマンド (MQTT): 通過                    │
│   REST POST /api/relay/{ch}: 202 Accepted      │
└──────────────────────┬────────────────────────┘
                       │
                       │ DI スイッチ ON (FALLING_EDGE)
                       │ → set_relay(ch, True) [I2C 直接]
                       │ → MQTT publish emergency/override
                       │ → _lockout_until = now + 300s
                       │
                       ▼
┌───────────────────────────────────────────────┐
│                 LOCKED OUT                    │
│   is_locked_out() = True                      │
│   LLM コマンド (MQTT): ドロップ (gate() = False)│
│   REST POST /api/relay/{ch}: 423 Locked       │
│                                               │
│   ※ スイッチ OFF (RISING_EDGE) が来ても:       │
│     → set_relay(ch, False) [I2C 直接]          │
│     → MQTT publish (state=false)              │
│     → ロックアウトは継続 (_lockout_until 不変)  │
└─────────────┬─────────────────┬───────────────┘
              │                 │
              │ 300 秒経過      │ POST /api/emergency/clear
              │ (自動)          │ → clear_lockout()
              │                 │ → _lockout_until = 0.0
              ▼                 ▼
┌───────────────────────────────────────────────┐
│                   NORMAL                      │
│   is_locked_out() = False                     │
│   LLM コマンド (MQTT): 通過                    │
└───────────────────────────────────────────────┘
```

---

## MQTT トピック一覧 (緊急オーバーライド関連)

| トピック | 方向 | QoS | retain | 説明 |
|---------|------|-----|--------|------|
| `agriha/{house_id}/emergency/override` | Publish | 1 | True | 緊急スイッチイベント通知 (CommandGate → ブローカー) |
| `agriha/{house_id}/relay/{ch}/set` | Subscribe | 1 | False | LLM リレー制御コマンド (ロックアウト中はドロップ) |
| `agriha/{house_id}/relay/state` | Publish | 1 | True | リレー全 ch 状態 (set 後に publish) |

### emergency/override ペイロード例

スイッチ ON 時:
```json
{
    "di_pin": 7,
    "relay_ch": 1,
    "state": true,
    "timestamp": 1740000000.123,
    "lockout_sec": 300
}
```

スイッチ OFF 時:
```json
{
    "di_pin": 7,
    "relay_ch": 1,
    "state": false,
    "timestamp": 1740000060.456,
    "lockout_sec": 0
}
```

---

## デーモン内の役割分担

`main.py` での各クラスの責務:

| クラス | 役割 |
|--------|------|
| `GPIOWatcher` | DI07-DI14 の edge event 検出 (asyncio fd 統合) |
| `CommandGate` | handle_gpio_event() で I2C 直接制御 + ロックアウト管理 |
| `_GatedRelay` | MqttRelayBridge に渡す relay アダプタ。gate() 経由で write をゲーティング |
| `MqttRelayBridge` | MQTT subscribe でリレー制御コマンドを受信 → _GatedRelay 経由で制御 |
| `RestApi` | HTTP エンドポイント。is_locked_out() を直接チェック、clear_lockout() を提供 |
