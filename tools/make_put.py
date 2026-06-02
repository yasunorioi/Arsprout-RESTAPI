import json, io, sys
src = sys.argv[1]            # ar81_comp.json
target_id = int(sys.argv[2]) # 8 or 51
out = sys.argv[3]            # output body file
j = json.load(io.open(src, encoding="utf-8-sig"))
comp = next(c for c in j if c.get("id") == target_id)
before = (comp["config"].get("DataSource"), comp["config"].get("CcmSide"))
comp["config"]["DataSource"] = "CCM"
comp["config"]["CcmSide"] = "R"
after = (comp["config"]["DataSource"], comp["config"]["CcmSide"])
print("id=%s name=%s  DataSource/CcmSide: %s -> %s  (CcmInfoName=%s region=%s)" % (
    target_id, comp["config"].get("ComponentName"), before, after,
    comp["config"].get("CcmInfoName"), comp["config"].get("CcmRegion")))
# write compact JSON (no BOM) for curl
io.open(out, "w", encoding="utf-8").write(json.dumps(comp, ensure_ascii=False))
