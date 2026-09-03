import json, os, pathlib
from mypkg import simulate      # only importable with /model/src on the path
pathlib.Path(os.environ.get("ENVBUILD_OUTPUTS", "/outputs"), "r.json").write_text(
    json.dumps({"result": simulate()}))
