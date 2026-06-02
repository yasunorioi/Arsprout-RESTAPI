#!/usr/bin/env bash
# agriha MQTT broker (Mosquitto) セットアップ — 冪等。
# 対象: Raspberry Pi OS / Debian。Pi 上で:  sudo bash setup_mqtt_broker.sh
#
# 設計準拠 (Arsprout-RESTAPI/mqtt-topics.md §0):
#   - LAN リスナ 1883（ccm_rp / agri-* / unipi-daemon が接続）
#   - retain 多用のため永続化を有効（再起動で保持メッセージ維持）
#   - 初期は匿名許可（LAN内運用）。認証を絞る場合は末尾の手順参照。
set -euo pipefail

CONF=/etc/mosquitto/conf.d/agriha.conf
PERSIST=/var/lib/mosquitto

echo "[agriha] 1/5 mosquitto を導入..."
if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get が無い。mosquitto を手動導入してください。"; exit 1
fi
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mosquitto mosquitto-clients >/dev/null
echo "    $(mosquitto -h 2>&1 | head -1)"

echo "[agriha] 2/5 設定 $CONF を書き込み..."
sudo mkdir -p "$(dirname "$CONF")"
# 重要: 既定 /etc/mosquitto/mosquitto.conf が既に persistence / persistence_location /
# log_dest を定義している。mosquitto 2.0 はこれらの「重複定義」で起動失敗する
# (Error: Duplicate persistence_location value)。よって conf.d には既定が持たない
# 「追加分」だけを書く。retain の永続化は既定の persistence でそのまま効く。
sudo tee "$CONF" >/dev/null <<'EOF'
# ===== agriha broker (managed by setup_mqtt_broker.sh) — 追加分のみ =====
# LAN リスナ（mosquitto 2.0 は既定で localhost のみなので明示）
listener 1883 0.0.0.0
# 匿名許可（LAN内運用。既定 2.0 は匿名拒否）。本番で絞るなら末尾の手順参照
allow_anonymous true
# 取りこぼし対策・保持保存間隔
autosave_interval 30
max_queued_messages 10000
EOF

echo "[agriha] 3/5 サービス有効化 + 再起動..."
sudo systemctl enable mosquitto >/dev/null 2>&1 || true
sudo systemctl reset-failed mosquitto 2>/dev/null || true   # 連続失敗の start-limit を解除
sudo systemctl restart mosquitto
sleep 1

echo "[agriha] 4/5 リスナ確認 (:1883)..."
if ! sudo ss -tlnp 2>/dev/null | grep -q ':1883'; then
  echo "  !! 1883 が LISTEN していません。状態:"; sudo systemctl status mosquitto --no-pager | tail -n 15; exit 1
fi
sudo ss -tlnp 2>/dev/null | grep ':1883' | sed 's/^/    /'

echo "[agriha] 5/5 retain pub/sub ループバック試験..."
( mosquitto_sub -h localhost -t 'agriha/_selftest' -C 1 -W 5 ) >/tmp/agriha_selftest 2>/dev/null &
SUBPID=$!
sleep 1
mosquitto_pub -h localhost -t 'agriha/_selftest' -m '{"value":1,"unit":"ok"}' -q 1 -r
if wait "$SUBPID" 2>/dev/null && [ -s /tmp/agriha_selftest ]; then
  echo "    OK: $(cat /tmp/agriha_selftest)"
else
  echo "    !! ループバック失敗"; exit 1
fi
mosquitto_pub -h localhost -t 'agriha/_selftest' -r -n || true   # 保持メッセージ消去
rm -f /tmp/agriha_selftest

IP=$(hostname -I | awk '{print $1}')
cat <<EOM

===================================================
 agriha MQTT broker 構築完了
   broker : ${IP}:1883   (mDNS: $(hostname).local)
   匿名   : 許可 (LAN内)。retain/persistence 有効。

 ノード設定:
   ccm_rp WebUI /mqtt → host=${IP} port=1883 house=<整数>
   購読確認: mosquitto_sub -h ${IP} -t 'agriha/#' -v
   試し投入: mosquitto_pub -h ${IP} -t 'agriha/2/setpoint/temp' \
             -m '{"value":22,"unit":"C","ts":0}' -r

 認証を付ける場合(任意):
   sudo mosquitto_passwd -c /etc/mosquitto/passwd agriha
   sudo sed -i 's/^allow_anonymous true/allow_anonymous false/' ${CONF}
   echo 'password_file /etc/mosquitto/passwd' | sudo tee -a ${CONF}
   sudo systemctl restart mosquitto
===================================================
EOM
