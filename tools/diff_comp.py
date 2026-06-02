import json, io, sys
j = json.load(io.open(sys.argv[1], encoding="utf-8-sig"))
targets = ["屋外風速", "屋外相対湿度", "屋外降雨量", "屋外降雨"]
sel = {}
for c in j:
    nm = c.get("config", {}).get("ComponentName")
    if nm in targets:
        sel[nm] = c
# union of config keys
keys = set()
for c in sel.values():
    keys |= set(c["config"].keys())
print("id:        " + "  ".join("%-14s" % (sel[t]["id"] if t in sel else "-") for t in targets))
print("name:      " + "  ".join("%-14s" % t for t in targets))
for k in sorted(keys):
    row = []
    for t in targets:
        v = sel[t]["config"].get(k, "") if t in sel else ""
        row.append("%-14s" % str(v)[:14])
    line = "  ".join(row)
    # only show keys that differ across the present targets
    vals = [sel[t]["config"].get(k, "") for t in targets if t in sel]
    mark = "*" if len(set(map(str, vals))) > 1 else " "
    print("%s %-16s %s" % (mark, k, line))
