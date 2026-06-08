# agriha 制御ロジック実装設計（ArSprout 機能移植）

`arsprout-logic-spec.md`（解析仕様）を agriha（中央 Python ブレーン, pi4, model-B）へ実装する際の設計方針。
**最初から Web UI 編集前提**で、config 駆動・MQTT ネイティブ・観測可能に作る。

---

## 0. 設計原則（全 Logic 共通）
1. **config 駆動 / UI 編集前提**: 各 Logic の設定は JSON ファイル（`/home/pi/agriha_<logic>.json`）。
   `mtime 自動リロード`（再起動不要、既存 scheduler と同方式）。スキーマは**平坦で規則的**に保ち、
   後で agriha_web の汎用フォームがそのまま編集できる形にする。
2. **MQTT ネイティブ**: 入力＝`agriha/#` を subscribe（センサー・他制御の状態）、出力＝`agriha/{house}/relay|window/.../set`。
   依存ゼロ（mosquitto_pub/sub を subprocess）。
3. **観測可能**: 各 Logic は「いま何を・なぜ」を publish（採用 source / 条件名 / 値）。UI と history に出す。
4. **アクチュエータ排他所有**（後述アービトレーションの核）。

---

## 1. アービトレーション設計 ★ 最重要論点の決着

### 問題（ユーザー指摘）
「温度制御と CO2/湿度制御で、どちらのルールが優先されるのか曖昧」

### ArSprout 実データが示す答え＝「優先順位の数値」ではなく**構造**で解く
1. **アクチュエータ排他所有**: ArSprout では各アクチュエータは**ちょうど 1 つの Logic の `LinkedActuator`**。
   - 気温制御 = 窓・換気扇・暖房・冷房を所有
   - CO2制御 = CO2施用機を所有 / 飽差制御 = 加湿機を所有 / カーテン制御 = カーテン / 灌水 = 灌水弁
   - **→ 2 つの Logic が同じリレーを奪い合うことが原理的に起きない**＝「どっちが勝つ」問題が消える。
2. **二次制御は『換気状態を条件に取り込んで自動的に控える』**（ソフト協調）:
   ArSprout の CO2 条件名は **晴天濃度(換気大/換気小/無換気)** ＝ **CO2 目標を換気状態で変える**。
   窓が開く（換気中）と CO2 施用は自動で絞る/止まる（換気中の施用は無駄なので）。飽差(加湿)も同様。
   → 温度制御が一次（環境の主）、CO2/湿度は**その結果(換気状態)を見て従属的に動く**。明示の veto ではなく入力協調。
3. **安全(SAFETY)だけが全体上書き**: 強風・降雨は温度制御の「開けたい」を無視して全閉。これが唯一の真の上書き。

### agriha 実装ルール（確定方針案）
- **`owner` レジストリ**: 各 actuator/relay は config で 1 controller に紐付け。framework が二重所有を検出・拒否。
- **共有状態バス**: 各 controller は自分の出力状態を publish 済（window pct / relay state）。
  二次 controller（CO2/湿度）は**換気状態（窓平均%・換気扇）を入力条件に使う**（例: 窓>20% で施用デューティ減 or 停止）。
- **SAFETY ラッチ**: 風速・雨を中央で評価し、影響する controller（窓・カーテン）に最優先で安全状態を強制。
- **数値の全体優先順位は持たない**（所有が排他なので不要）。安全だけが別格。

> まとめ: 温度=主、CO2/湿度=従（換気状態に追従）、安全=全体上書き。
> 「とりあえず ON」ではなく「換気中は控える」を条件に明文化する。

---

## 2. スケジュール設計（時間制御の汎用化）

ArSprout は全 Logic 共通で **`StartType ∈ {SUNRISE, SUNSET, FIXED_TIME}` + Hour/Min + `Delay`(±分)**。
ユーザー案「crontab に日の出/日没変数を入れて UI 編集」と狙いは同じ。

### 方針: 常駐 Python が JSON スケジュールを毎 tick 評価（crontab は使わない）
- 理由: 日の出/日没相対＋オフセットは crontab で表しにくい／daemon は既に常駐し自前評価が柔軟／UI 編集は JSON が素直
  （crontab 再生成・権限が不要）。＝ユーザー案の意図（汎用・UI 編集可）を、より素直な形で実現。
- **日の出/日の入り計算**: lat/lon（NodeConfig=35/135）から純 Python で算出（NOAA 略算, 依存ゼロ）。日付ごとに更新。
- **period（時間帯）モデル**（全 Logic 共通の部品）:
  ```json
  { "anchor": "sunrise|sunset|fixed", "time": "06:00", "offset_min": -30, ... }
  ```
  `anchor=fixed`→`time` をそのまま、`sunrise/sunset`→当日の太陽時刻＋`offset_min`。これを絶対時刻に解決し
  「今どの period か」を判定。既存 `setpoint_scheduler.py`(固定HH:MMのみ) をこの period モデルに一般化する。

---

## 3. モジュール構成 と 実装順

### 共通基盤（先に作る）
- `agriha_suntime.py` — lat/lon/date → 日の出・日の入り（依存ゼロ）。
- `agriha_control.py`（フレームワーク）— config ロード＋mtime リロード / period 解決 / MQTT 読み書き /
  actuator 所有レジストリ / SAFETY ラッチ / 状態 publish。各 controller はこの上に薄く乗る。

### controller 実装順（提案）
1. **SWT_RULE 汎用ルールエンジン** — CO2・飽差(湿度)・任意 on/off を一本化。再利用価値最大・
   アクチュエータ排他なので安全に追加でき、上記「換気中は控える」協調をここで実装＝論点を実コードで決着。
2. **STD_ATMP（気温制御・中央版）** — 時間帯×目標温度＋比例＋多段風雨セーフティ＋風向ゲート。
   現状 ccm_rp ノード側にも簡易版あり→中央/ノードの分担を確定（中央が目標%まで出すか、setpoint だけ出すか）。
3. **STD_CRTN（カーテン）** — 保温(温度)＋遮光(日射)。
4. **STD_IRRI（灌水）** — 時刻スロット＋日射積算。※灌水/流量センサーは agri-drain/flow PoE ノードへ移行中。

> 各段階で UI 編集スキーマも同時に決める（後付け UI で破綻させない）。

## 4. 関連
- 解析仕様: `arsprout-logic-spec.md`
- トピック規約: `mqtt-topics.md` / setpoint: `setpoint-schedule-design.md`
- ノード側実装: `ccm_rp2350_relay/docs/relay-actuation-design.md`（intent 調停・break-before-make）
