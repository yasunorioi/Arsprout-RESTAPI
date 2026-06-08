# ArSprout 制御仕様（解析リバースエンジニアリング）— agriha 移植リファレンス

商用 ArSprout Pi の実機ノード設定 XML（`arsprout-analysis/node_extracted/arsprout-pi-configs-1.13.1/*SwitchBoard-v3*`,
Version 1.13.3）を解析して得た、制御ロジック／コンポーネント／デバイスの仕様。
agriha（中央 Python ブレーン, model-B）へ機能移植する際の一次リファレンス。

> 元データ抽出: `arsprout-analysis` をパースして得た構造。Logic=5, Component=38, Device=4。

---

## 1. データモデル（XML 設定の構造）

`ArsproutPi` 直下に 9 セクション。実体 `X`（id+type）と設定 `XConfig`（id 別 key/value）のペア。

| セクション | 役割 |
|-----------|------|
| `RuntimeConfig` | システム（IP/Cloud/言語/時刻/テーマ） |
| `NodeConfig` | UECS ノード（Room=1, Region=61, Priority=1, UecsId, Watchdog 300s, **Latitude=35/Longitude=135**） |
| `Device` (4) | ハード: `UniPi`(リレー8/DI/AI/AO・PortType), `W1`(1-Wire温度), `LCD1602`, `WeatherSt`(UART気象) |
| `Component` (38) | センサー/演算/アクチュエータ/アラートの「論理点」 |
| `Logic` (5) | 制御ロジック本体 |
| `Dashboard` (6) | 画面（TREND_CHART＋各Logicカード） |

## 2. Component 普遍モデル（全点共通の属性 = UECS ノードの心臓）

- **CCM 対応**: `CcmInfoName`(=UECS型 例 InAirTemp) / `CcmRoom` / `CcmRegion` / `CcmOrder` /
  `CcmCast`(0-2) / **`CcmSide`(R=受信 / S=送信)** / `CcmPriority` / `CcmLevel`(例 A-10S-0=送出間隔) / `CcmUnit`
- **データ束縛**: `DataSource` = **CCM**(ネット受信) or **DEVICE**(ローカルポート読取)。
  `DataPorts` = `{"DataPort":"1.TEMP"}`（=Device id.ポート名で結線。SEN_HD は TempPort/HumidPort）
- **処理**: `DetectionMethod`(MOMENT / SMA移動平均 / DIR) / `DetectionInterval` / `ConversionType=POLYNOMIAL`+`Coeff0-3`(校正)
- **限界/警報**: Min/Max(表示域) / LimitMin/LimitMax+`LimitOverRule`(ROUND) / **Low/High(警報閾)** /
  `ValueType`(NUM / SWT(0-1) / POS(0-100%) / DIR(方位))
- **記録**: `RecordInterval=300`（ArSprout 自身も 5 分ロギング）/ `CloudLinkEnabled`
- **アクチュエータ専用**(ACT_POS_*): `FullOpenTime`/`FullCloseTime`(ストローク秒) /
  **`ReverseWaitTime=3`(反転デッドタイム＝break-before-make)** / `GapCorrection`+`GapCorrectionTime=30`(バックラッシュ補正) /
  Open/CloseOverlapTime
- **演算点**: `CAL_INTEGRATION`(日射積算: StartType SUNRISE / ResetType FIXED_TIME 0時 / Coeff1=0.001) /
  `CAL_TIME_MEAN`(時間窓平均=平均気温, `CalcTargetId`=元点)
- **`ALT_RULE`**: 条件評価(RuleEvalId/Sign/Value)→フラグCCM(例 RadAlert)を `HoldTime` 付きで生成。他Logicの入力に使う

### Component 型一覧（23 種）
センサー: `SEN_TMP`温度 / `SEN_RH`湿度 / `SEN_HD`飽差 / `SEN_CO2` / `SEN_RAD`日射 / `SEN_SPD`風速 /
`SEN_DIR`風向 / `SEN_WC`土壌水分 / `SEN_EC` / `SEN_SWT`スイッチ(降雨等) / `SEN`汎用(照度/雨)
演算: `CAL_INTEGRATION`積算 / `CAL_TIME_MEAN`時間平均 / `ALT_RULE`アラート・補正
アクチュエータ: `ACT_POS_WIN`窓(位置) / `ACT_POS_CTN`カーテン(位置) /
`ACT_SWT_HET`暖房 / `ACT_SWT_COL`冷房 / `ACT_SWT_CO2`CO2施用 / `ACT_SWT_IRR`灌水 /
`ACT_SWT_HUM`加湿 / `ACT_SWT_VFN`換気扇 / `ACT_SWT_CFN`攪拌扇

## 3. 制御ロジック 4 種

### ① STD_ATMP（気温制御）— 最重要・最複雑
- 入力: `TempSensorId` / `WindSpeedSensorId` / `WindDirSensorId` / `RainSensorId`
- 連動アクチュエータ最大8枠: `LinkedActuatorIds=[窓30,31,32,33, 換気扇47, 暖房36,37, 冷房38]`
- **8 時間帯 × 8 アクチュエータのマトリクス制御**:
  - 時間帯(期 1-8): `StartType`(SUNRISE/SUNSET/FIXED_TIME)+Hour/Min+**`StartDelay`(±分)**、End 同様
    → **日の出/日の入り相対スケジュール＋オフセット**
  - 期×枠: **`TargetTemp-期-枠`**（アクチュエータ個別の目標温度＝多段ステージング。
    例 期4: 窓26 / 換気扇28 / 暖房18・14 / 冷房25℃）、`WindowOpenLimit/CloseLimit-期-枠`(開度上下限)、
    `AdjustTemp1/2-期-枠`(補正＝ALT_RULE 連動)
  - **`Sensitivity-期`=0.5**（比例帯℃。目標±この幅で 0→100%）← agriha の「比例+不感帯」と同型
- **風セーフティ多段**: `WindSpeedWarn1=5→WindWarnPos1=30%`, `Warn2=8→Pos2=10%`, `Alert=10→全閉`,
  `WindSpeedHoldTime=5`、+ **風向ゲート**（`WindDirSensorId`+`Direction-期`で風上の窓だけ閉）
- **雨セーフティ**: `RainSensorId`, `RainAlertLimit-期`(例 開度10%上限), `RainHoldTime`
- `ExecuteInterval=10`s

### ② STD_CRTN（カーテン制御）— 2 系統
- **保温カーテン(ThCrtn)**: 温度ベース（`ThCrtnCloseTemp=14`/`OpenTemp=15`+`Sens=0.5`）＋時間帯、
  4段位置(`Position1-4`=0/5/10/100%)、連動34。夜間保温で閉。
- **遮光カーテン(LsCrtn)**: 日射ベース（`LsCrtnCloseRad=1`kW/m²+`Sensitivity=0.05`）＋時間帯
  (日の出+120分〜日の入-120分)、`OvertimeAction=open`、連動35。

### ③ SWT_RULE（汎用ルールエンジン）— CO2制御・飽差制御に使用【最も再利用価値が高い】
優先順位付き「条件 → デューティ動作」エンジン。同じ仕組みで CO2・湿度・任意の on/off を賄う。
- N 条件（優先順 1..N）。各条件 = 複数サブルール（`RuleEvalId-条件-行`=点id, `Sign`=GE/LE,
  `Value`=閾値, `Check`=RAW）を **`RuleEvalType`=AND** 合成 ＋ 時間窓(Start/End Type/Hour/Min/Delay)
- マッチした**最優先条件**の動作: `ActionType=TIMER_REPEAT`(`OnInterval`/`OffInterval` 秒でデューティ) / `OFF` / 位置
  （`Position`/`PositionStep`）、`ActionAbortEnabled`
- 例 **CO2制御(id3)**: CO2(6)×日射(9)×アラート(49,50) → 燃焼CO2(39) を条件別デューティ
  （曇天 On300/Off0, 晴天換気大 On900/Off900 …＝換気状態で濃度方針を変える）
- 例 **飽差制御(id5)**: 飽差(3)×室温(1)×アラート(50) → 加湿細霧(41) をデューティ
  （過乾燥(換気大) On30/Off180 … 適湿 OFF）

### ④ STD_IRRI（灌水制御）
- **時刻スロット10枠**: `StartHour/Min-N` + `OperationTime-N`(秒) + `Enabled-N` + Start/EndDelay
- **日射積算トリガ**: `RadSensorId`, `RadThreshold=1.0`, `RadIrriTime`, `RadStartType=SUNRISE`/`RadEndType=SUNSET`,
  `RadStartHour=7`/`RadEndHour=17`, `WaitInterval=30` ＝光合成連動灌水
- `ExecuteInterval=1`s

## 4. agriha への移植メモ

- agriha の現行 model-B（setpoint→比例+不感帯+風雨セーフティ, ccm_rp）は **STD_ATMP の簡略版**。
  本家は「時間帯×アクチュエータ目標温度マトリクス＋多段風セーフティ＋風向ゲート」まで持つ。
- **SWT_RULE 汎用ルールエンジンを Python で実装**すれば CO2・飽差・他の on/off デューティを一本化できる。
- 窓 `ReverseWaitTime=3`/`GapCorrection` = agriha の break-before-make＋（未実装）バックラッシュ補正に対応。
- 時間制御は全 Logic 共通で **SUNRISE/SUNSET/FIXED_TIME ＋ ±offset(分)**。Lat/Lon は NodeConfig にある
  （35/135）→ 日の出日の入りは中央で計算可能。
- `RecordInterval=300` = agriha-logger と整合（生は更に細かく取得→5分集約）。

## 5. 重要な設計論点（agriha 実装方針）
→ `arsprout-logic-design.md`（アービトレーション/スケジュール/UI 設計）を参照。
