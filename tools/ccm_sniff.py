#!/usr/bin/env python3
# UECS-CCM passive sniffer — listen only, never transmits.
# 224.0.0.1:16520 のマルチキャストを受信し、送信元IPごとに DATA 要素を集計。
# 実働LANを一切いじらない受動診断用。
#
#   python ccm_sniff.py [seconds] [iface_ip ...]
#   例: python ccm_sniff.py 45 192.168.1.247 192.168.1.60

import socket, struct, sys, re, time
from collections import defaultdict

GROUP = "224.0.0.1"
PORT  = 16520

dur    = int(sys.argv[1]) if len(sys.argv) > 1 else 45
ifaces = sys.argv[2:] if len(sys.argv) > 2 else ["192.168.1.247", "192.168.1.60"]

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
except (AttributeError, OSError):
    pass
sock.bind(("", PORT))  # Windows: bind INADDR_ANY then join per-iface

for ip in ifaces:
    try:
        mreq = socket.inet_aton(GROUP) + socket.inet_aton(ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        print(f"[join] {GROUP} via {ip}")
    except OSError as e:
        # 224.0.0.1 は all-hosts。join 失敗してもバインドのみで届くことが多い
        print(f"[join] {ip} failed ({e}) — continuing (all-hosts may still arrive)")

sock.settimeout(1.0)
print(f"[listen] {GROUP}:{PORT} for {dur}s (passive, no TX)\n")

DATA_RE = re.compile(
    r'<DATA\s+type="([^"]*)"'
    r'(?:[^>]*?\broom="([^"]*)")?'
    r'(?:[^>]*?\bregion="([^"]*)")?'
    r'(?:[^>]*?\border="([^"]*)")?'
    r'(?:[^>]*?\bpriority="([^"]*)")?'
    r'[^>]*>([^<]*)</DATA>', re.I)

# senders[ip][(type,room,region,order)] = {value, prio, count, last}
senders = defaultdict(dict)
pkt_count = 0
deadline = time.time() + dur

while time.time() < deadline:
    try:
        data, addr = sock.recvfrom(8192)
    except socket.timeout:
        continue
    pkt_count += 1
    ip = addr[0]
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        text = repr(data)
    for m in DATA_RE.finditer(text):
        typ, room, region, order, prio, val = m.groups()
        key = (typ, room or "", region or "", order or "")
        rec = senders[ip].setdefault(key, {"count": 0})
        rec["value"] = val.strip()
        rec["prio"]  = prio or ""
        rec["count"] += 1
        rec["last"]  = time.strftime("%H:%M:%S")

print(f"\n==== {pkt_count} packets from {len(senders)} sender(s) in {dur}s ====\n")
for ip in sorted(senders):
    print(f"### {ip}")
    print(f"{'type':22} {'room':>4} {'reg':>4} {'ord':>4} {'pri':>4} {'n':>4}  value")
    for (typ, room, region, order) in sorted(senders[ip]):
        r = senders[ip][(typ, room, region, order)]
        print(f"{typ:22} {room:>4} {region:>4} {order:>4} {r.get('prio',''):>4} "
              f"{r['count']:>4}  {r.get('value','')}")
    print()
