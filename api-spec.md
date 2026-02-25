# unipi-daemon REST API 仕様書

- **バージョン**: 1.0.0
- **ベースURL**: `http://<RPi-IP>:8080`（デフォルトポート。`config.yaml` の `rest_api.port` で変更可）
- **ソース**: `/home/yasu/unipi-agri-ha/services/unipi-daemon/rest_api.py`

---

## 認証

**X-API-Key** ヘッダーを使用する。

```
X-API-Key: <api_key>
```

- `config.yaml` の `rest_api.api_key` で設定する
- 空文字（デフォルト）の場合は認証スキップ（開発用）
- 本番環境では必ず設定すること

**認証エラー時のレスポンス**

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json

{"detail": "Invalid API key"}
```

---

## エンドポイント一覧

| メソッド | パス | 概要 |
|---------|------|------|
| POST | `/api/relay/{ch}` | リレー制御（ON/OFF + 自動OFFタイマー） |
| GET  | `/api/sensors`    | センサーデータ取得（MQTTキャッシュ経由） |
| GET  | `/api/status`     | デーモン状態取得（uptime, ロックアウト, リレー状態） |
| POST | `/api/emergency/clear` | 緊急ロックアウト解除 |

---

## 1. POST /api/relay/{ch} — リレー制御

リレーチャンネルをON/OFFする。MQTT経由で `MqttRelayBridge` に非同期で転送される。

### パスパラメータ

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `ch` | integer | ✓ | リレーチャンネル番号（1〜8） |

### リクエストヘッダー

| ヘッダー | 必須 | 説明 |
|---------|------|------|
| `X-API-Key` | 条件付き | `api_key` 設定時に必須 |
| `Content-Type` | ✓ | `application/json` |

### リクエストボディ（JSON）

```json
{
  "value": 1,
  "duration_sec": 30.0,
  "reason": "灌水開始"
}
```

| フィールド | 型 | 必須 | デフォルト | 説明 |
|-----------|-----|------|-----------|------|
| `value` | integer | ✓ | — | `1` = ON、`0` = OFF |
| `duration_sec` | float | — | `0.0` | 自動OFFまでの秒数（`0.0` = タイマーなし）。`0.0` 以上の値を指定する |
| `reason` | string | — | `""` | 制御理由（ログ記録用） |

### MQTT publish 先

```
agriha/{house_id}/relay/{ch}/set
```

publishされるペイロード（JSON）:
```json
{"value": 1, "duration_sec": 30.0, "reason": "灌水開始"}
```

### レスポンス

#### 202 Accepted — 正常（コマンドをMQTTキューに投入）

```json
{
  "ch": 3,
  "value": 1,
  "queued": true
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `ch` | integer | 制御したリレーチャンネル番号 |
| `value` | integer | 設定値（0 or 1） |
| `queued` | boolean | 常に `true` |

#### 423 Locked — ロックアウト中（緊急スイッチON後300秒間）

リレー操作が拒否される。

```json
{
  "error": "locked_out",
  "message": "緊急スイッチによりロックアウト中",
  "remaining_sec": 247.3
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `error` | string | `"locked_out"` 固定 |
| `message` | string | エラーメッセージ（日本語） |
| `remaining_sec` | float | ロックアウト残り秒数 |

#### 503 Service Unavailable — MQTTブローカー未接続

```json
{
  "error": "mqtt_unavailable",
  "message": "MQTT ブローカー未接続"
}
```

#### 403 Forbidden — API Key 不正

```json
{"detail": "Invalid API key"}
```

### HTTPステータスコードまとめ

| コード | 意味 |
|--------|------|
| 202 | コマンドをMQTTキューに投入（非同期）|
| 403 | API Key 不正 |
| 422 | バリデーションエラー（`ch` が1-8範囲外、`value` が0/1以外等） |
| 423 | 緊急スイッチによりロックアウト中 |
| 503 | MQTTブローカー未接続 |

---

## 2. GET /api/sensors — センサーデータ取得

MQTTキャッシュに蓄積された最新センサーデータを返す。

MQTT subscribeしているトピック:
- `agriha/{house_id}/sensor/#` — ハウス固有センサーデータ
- `agriha/farm/weather/misol` — 農場気象データ（Misol WH65LP）
- `agriha/{house_id}/relay/state` — リレー状態
- `agriha/{house_id}/ccm/#` — UECS-CCM 内気象/アクチュエータデータ

### リクエストヘッダー

| ヘッダー | 必須 | 説明 |
|---------|------|------|
| `X-API-Key` | 条件付き | `api_key` 設定時に必須 |

### クエリパラメータ

なし

### レスポンス

#### 200 OK

```json
{
  "sensors": {
    "agriha/h01/sensor/temperature": {"value": 22.5, "unit": "°C"},
    "agriha/farm/weather/misol": {"WWindSpeed": 1.2, "WRainfall": 0},
    "agriha/h01/ccm/InAirTemp": {"value": 23.1}
  },
  "updated_at": 1740441600.0,
  "age_sec": 3.5
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `sensors` | object | MQTTトピックをキー、受信JSONペイロードを値とする辞書。キャッシュが空の場合は空オブジェクト `{}` |
| `updated_at` | float | キャッシュの最終更新時刻（UNIXタイムスタンプ）。データなしの場合は `0.0` |
| `age_sec` | float \| null | キャッシュの経過秒数。`updated_at` が `0.0`（データなし）の場合は `null` |

> **注意**: `sensors` の各値はMQTTで受信したJSONをそのまま返す。スキーマはトピックごとに異なる。

#### 403 Forbidden — API Key 不正

```json
{"detail": "Invalid API key"}
```

### HTTPステータスコードまとめ

| コード | 意味 |
|--------|------|
| 200 | 正常（キャッシュが空でも200を返す） |
| 403 | API Key 不正 |

---

## 3. GET /api/status — デーモン状態取得

デーモンのランタイム状態を返す。ロックアウト確認やリレー状態の読み取りに使用する。

### リクエストヘッダー

| ヘッダー | 必須 | 説明 |
|---------|------|------|
| `X-API-Key` | 条件付き | `api_key` 設定時に必須 |

### クエリパラメータ

なし

### レスポンス

#### 200 OK

```json
{
  "house_id": "h01",
  "uptime_sec": 3600,
  "locked_out": false,
  "lockout_remaining_sec": 0.0,
  "relay_state": {
    "ch1": false,
    "ch2": true,
    "ch3": false,
    "ch4": false,
    "ch5": false,
    "ch6": false,
    "ch7": false,
    "ch8": false
  },
  "ts": 1740441600.123
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `house_id` | string | ハウスID（`config.yaml` の `daemon.house_id`） |
| `uptime_sec` | integer | デーモン起動からの経過秒数（`time.monotonic()` ベース） |
| `locked_out` | boolean | `true` = 緊急ロックアウト中（リレー操作API は 423 を返す） |
| `lockout_remaining_sec` | float | ロックアウト残り秒数。非ロックアウト時は `0.0` |
| `relay_state` | object \| null | 各リレーチャンネルの現在状態（`true`=ON, `false`=OFF）。I2C読み取り失敗時は `null` |
| `ts` | float | レスポンス生成時刻（UNIXタイムスタンプ） |

`relay_state` の詳細:

| キー | 型 | 説明 |
|------|-----|------|
| `ch1` 〜 `ch8` | boolean | 対応するリレーチャンネルのON/OFF状態 |

#### 403 Forbidden — API Key 不正

```json
{"detail": "Invalid API key"}
```

### HTTPステータスコードまとめ

| コード | 意味 |
|--------|------|
| 200 | 正常（`relay_state` が `null` でも200を返す） |
| 403 | API Key 不正 |

---

## 4. POST /api/emergency/clear — 緊急ロックアウト解除

物理スイッチ（DI07-DI14）によって発動した緊急ロックアウトを手動で解除する。

通常はロックアウト発動から300秒後に自動解除される。本エンドポイントは強制的に即時解除する。

### リクエストヘッダー

| ヘッダー | 必須 | 説明 |
|---------|------|------|
| `X-API-Key` | 条件付き | `api_key` 設定時に必須 |

### リクエストボディ

なし

### レスポンス

#### 200 OK

```json
{
  "cleared": true,
  "was_locked_out": true
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `cleared` | boolean | 常に `true` |
| `was_locked_out` | boolean | 解除前にロックアウト状態だったか（`false` = ロックアウトしていなかった状態でのAPI呼び出し） |

#### 403 Forbidden — API Key 不正

```json
{"detail": "Invalid API key"}
```

### HTTPステータスコードまとめ

| コード | 意味 |
|--------|------|
| 200 | 正常（ロックアウト中でなくても200を返す） |
| 403 | API Key 不正 |

---

## 緊急ロックアウト機構

物理スイッチ（UniPi 1.1 DI07〜DI14）がONになると以下が発生する:

1. `CommandGate` が I2C で直接リレーを制御（MQTTを経由しない高速安全制御）
2. ロックアウトが300秒間発動
3. ロックアウト中は `POST /api/relay/{ch}` が **423** を返す
4. 300秒経過で自動解除、または `POST /api/emergency/clear` で即時解除

```
物理スイッチON
  → CommandGate.handle_gpio_event()
  → I2C直接リレー制御 + ロックアウト開始（300秒）
  → POST /api/relay/{ch} → 423 Locked
  → 300秒後 or POST /api/emergency/clear → ロックアウト解除
  → POST /api/relay/{ch} → 202 Accepted
```

---

## 設定例（config.yaml）

```yaml
rest_api:
  host: 0.0.0.0      # バインドアドレス（0.0.0.0 = 全インターフェース）
  port: 8080          # ポート番号
  api_key: ""         # 空文字 = 認証スキップ（本番では必ず設定すること）
```

---

## curlサンプル

```bash
# リレー3をON（30秒後に自動OFF）
curl -X POST http://10.10.0.10:8080/api/relay/3 \
  -H "Content-Type: application/json" \
  -d '{"value": 1, "duration_sec": 30.0, "reason": "灌水開始"}'

# センサーデータ取得
curl http://10.10.0.10:8080/api/sensors

# デーモン状態確認
curl http://10.10.0.10:8080/api/status

# 緊急ロックアウト解除
curl -X POST http://10.10.0.10:8080/api/emergency/clear

# APIキー認証あり
curl -H "X-API-Key: mysecretkey" http://10.10.0.10:8080/api/status
```
