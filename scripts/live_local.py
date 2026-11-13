#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Drive src/live.py against a REAL container, with Docker standing in for the
cluster. The offline suite fakes the executor; this proves the parts a fake
cannot: the command loop, the prelude, step reuse, and the ladder running
inside the scratch container. Needs a docker daemon and network. Not part of
the suite, and nothing in the agent uses Docker.

    uv run scripts/live_local.py
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import baseselect  # noqa: E402
import ladder      # noqa: E402
import live        # noqa: E402
from envspec import EnvSpec, MountContract  # noqa: E402


class DockerAsCluster:
    """The three client calls LiveSession makes, mapped onto docker."""
    namespace = "local"

    def __init__(self, work: Path, models: Path):
        self.claims = {"work": work, "models": models}

    def create_pod(self, m):
        c = m["spec"]["containers"][0]
        args = ["docker", "run", "-d", "--name", m["metadata"]["name"]]
        for v in c["volumeMounts"]:
            host = self.claims[v["name"]] / v.get("subPath", "")
            args += ["-v", f"{host}:{v['mountPath']}" + (":ro" if v.get("readOnly") else "")]
        subprocess.run(args + [c["image"], *c["command"]], check=True, capture_output=True)

    def get_pod(self, name):
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                             capture_output=True, text=True).stdout.strip()
        return {"status": {"phase": "Running" if out == "true" else "Failed"}}

    def delete_pod(self, name, grace_seconds=0):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main() -> None:
    d = tempfile.mkdtemp()
    try:
        work, repo = Path(d) / "work", Path(d) / "models" / "toy" / "1.0"
        (repo / "demo").mkdir(parents=True)
        (repo / "toy.py").write_text("import numpy\nVALUE = int(numpy.arange(4).sum())\n")
        (repo / "demo" / "run.py").write_text(
            "import os, yaml, toy\nopen('result.txt', 'w').write(str(toy.VALUE))\n"
            "print('FOO=' + os.environ['FOO'], 'value', toy.VALUE)\n")
        s = live.LiveSession("job-local", DockerAsCluster(work, Path(d) / "models"), {},
                             "work", str(work), "models", str(Path(d) / "models"), str(repo))
        spec = EnvSpec(base_image="python:3.12-slim",
                       base_digest=baseselect.resolve_digest("python:3.12-slim"),
                       pkg_specs=["numpy"], env_vars={"FOO": "bar"},
                       mount=MountContract(workdir="/model/demo", extra_path=("/model",), writable_copy=True))
        entry = ladder.entry_from_command("python demo/run.py")
        try:
            t = time.time(); b = s.build(spec, "", 900)
            assert b.ok, b.stderr[-2000:]
            print(f"round 1: build ok, {b.vertices_total} steps run, {time.time() - t:.0f}s")
            lad = ladder.climb(s.climb_with(spec), b.image_ref, s.code_ref, spec, entry,
                               "python demo/run.py", 120, l3_timeout_s=120)
            # yaml is not installed: the ladder must stop and say so
            assert lad.failed_at in ("L2", "L3") and "yaml" in lad.stderr, (lad.failed_at, lad.stderr[-800:])
            print(f"round 1: ladder stopped at {lad.failed_at} on the missing module, as it should")

            spec.pkg_specs = ["numpy", "pyyaml"]
            t = time.time(); b = s.build(spec, "", 900)
            assert b.ok and b.vertices_total == 1, (b.vertices_total, b.stderr[-800:])
            print(f"round 2: repair re-ran {b.vertices_total} step, reused {b.cache_hits}, {time.time() - t:.0f}s")
            lad = ladder.climb(s.climb_with(spec), b.image_ref, s.code_ref, spec, entry,
                               "python demo/run.py", 120, l3_timeout_s=120)
            assert lad.failed_at is None and lad.reached == "L3", (lad.failed_at, lad.stderr[-1500:])
            assert "result.txt" in " ".join(lad.outputs), lad.outputs
            print(f"round 2: ladder reached {lad.reached}; outputs {lad.outputs}")
            assert not (repo / "demo" / "result.txt").exists(), "the read-only source was written to"

            r = s.run("live", s.code_ref, ["sh", "-c", "sleep 30"], spec.mount, 3)
            assert r.timed_out and s.live["pod"], r        # killed in place; the container survives
            print("timeout: command killed in place, container kept")
        finally:
            s.cleanup("job-local")
    finally:
        # the container wrote as root, so a container removes what it wrote
        subprocess.run(["docker", "run", "--rm", "-v", f"{d}:/d", "busybox", "rm", "-rf", "/d/work", "/d/models"],
                       capture_output=True)
    print("live_local: ok")


if __name__ == "__main__":
    main()
