import json, io, sys
d = json.load(io.open(sys.argv[1], encoding="utf-8-sig"))
print("root type:", type(d).__name__)
if isinstance(d, dict):
    print("root keys:", list(d.keys()))
    devs = d.get("value", d)
else:
    devs = d
print("device count:", len(devs))
for x in devs:
    print("### dev id=%s type=%s state=%s dataSet=%s iface=%s ports=%d" % (
        x.get("id"), x.get("deviceType"), x.get("state"),
        x.get("dataSetType"), x.get("interfaceType"), len(x.get("dataPorts", []))))
    for p in x.get("dataPorts", []):
        ac = p.get("assignComponents") or []
        pid = str(p.get("portId", ""))
        if ac or any(k in pid for k in ("WIND", "RAIN", "TEMP", "HUMID")):
            print("    %-12s val=%-22s %-4s comp=%s" % (
                pid, p.get("value"), p.get("unit"), ac))
