import json, pathlib
# Writes to a hardcoded /results, which nothing collects from.
out = pathlib.Path("/results"); out.mkdir(parents=True, exist_ok=True)
(out / "r.json").write_text(json.dumps({"ok": True}))
print("wrote /results/r.json")
