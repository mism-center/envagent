import json, pathlib, os
import requests            # never declared anywhere
pathlib.Path(os.environ.get("ENVBUILD_OUTPUTS", "/outputs"), "r.json").write_text(
    json.dumps({"ua": requests.utils.default_user_agent()}))
