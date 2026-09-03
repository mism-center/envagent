import json, os, pathlib, mypkg
pathlib.Path(os.environ.get("ENVBUILD_OUTPUTS", "/outputs"), "r.json").write_text(
    json.dumps({"answer": mypkg.answer()}))
