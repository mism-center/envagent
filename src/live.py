"""A live scratch container: search here, verify with a clean build.

Every hypothesis used to cost a whole Kaniko build plus a ladder of fresh
pods, and the budget allowed five. `LiveSession` keeps ONE container of the
spec's base image alive for the job and plays both `Builder` and `Verifier`
against it:

* `build(spec, ...)` runs the rendered steps as shell, skipping any step
  whose text already succeeded in this container. A repair that adds an apt
  package re-runs one `apt-get`, not the image.
* `run(...)` runs a ladder probe or the L3 command in the same container, so
  `ladder.climb` and the classifier work unchanged.

Nothing found here is a result. The container is not reproducible (removed
packages linger, the network is on at L3, steps ran in whatever order the
search took), so a live pass only earns a clean `envbuild attempt`: one
Kaniko build of the final spec and the real, network-denied ladder. The
verdict rests on that attempt alone.

No `pods/exec`: commands reach the container as files on the work claim and
results come back the same way (CLAUDE.md invariant). The pod has no service
account token, no Secret and no registry credential; the model source is
mounted read-only. Its network is open, exactly as a Kaniko build's is --
it installs packages -- which is why it must never hold anything worth
stealing.

ponytail: one container, no snapshots. A change of base image, install mode
or code path starts a new one and replays every step. Layer snapshots
(commit the container per step) if replay time ever dominates.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import time
from pathlib import Path

import k8s
import logs as logtools
from builder import BUILD_RESOURCES, BuildResult
from envspec import EnvSpec, MountContract, render
from errors import InfraError
from verifier import RunResult

_MAIN = "live"
_X = "/envbuild-x"          # command/result exchange, on the work claim
_SRC = "/envbuild-src"      # the model source, read-only
_IO = "/envbuild-io"        # inputs/ and outputs/, the same directories the verifier uses

# Runs each /in/N.sh in order, exactly once. `mv` makes the result appear
# atomically: the reader never sees a half-written exit code.
_LOOP = f"""mkdir -p {_X}/in {_X}/out; n=1
while :; do
  if [ -f {_X}/in/$n.sh ]; then
    sh {_X}/in/$n.sh > {_X}/out/$n.log 2>&1; echo $? > {_X}/out/$n.tmp
    mv {_X}/out/$n.tmp {_X}/out/$n.rc; n=$((n+1))
  else sleep 1; fi
done"""

_MARKER_LINE = re.compile(r"^RUN echo '[^']*' \\\n && ")


def _step_key(instruction: str) -> str:
    """A step's identity without its index marker, so adding a step earlier
    in the Dockerfile does not make every later one look new."""
    return hashlib.sha256(_MARKER_LINE.sub("RUN ", instruction).encode()).hexdigest()[:16]


class LiveSession:
    """`live` is the job's persisted session dict (`state["live"]`); this
    object mutates it and the driver saves it with the rest of the state."""

    def __init__(self, job_id: str, client, live: dict, work_pvc: str, work_mount: str,
                 models_pvc: str, models_mount: str, code_ref: str,
                 lifetime_s: int = 3600, resources: dict | None = None):
        self.job_id, self.client, self.live = job_id, client, live
        self.work_pvc, self.work_mount = work_pvc, Path(work_mount)
        self.models_pvc, self.models_mount = models_pvc, models_mount
        self.code_ref = code_ref
        self.lifetime_s = lifetime_s
        self.resources = resources or BUILD_RESOURCES
        self.attempt = 0                       # Builder protocol; unused

    # -- pod ---------------------------------------------------------------
    def _sub(self, leaf: str) -> str:
        return f"{self.job_id}/{leaf}"

    def _pod_manifest(self, pod_name: str, image: str, gen: int) -> dict:
        try:
            models_sub = Path(self.code_ref).relative_to(self.models_mount).as_posix()
        except ValueError as exc:
            raise InfraError(f"model source {self.code_ref} is not under the models mount "
                             f"{self.models_mount!r}") from exc
        # in/ and out/ are made HERE, by the agent's uid: made by the container
        # (root) they would be unwritable to an agent that is not root.
        for leaf in (f"live{gen}/in", f"live{gen}/out", "inputs", "outputs"):
            (self.work_mount / self._sub(leaf)).mkdir(parents=True, exist_ok=True)
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.client.namespace,
                         "labels": {"app": "envbuild-live", "job-id": self.job_id}},
            "spec": {
                "restartPolicy": "Never",
                # The kubelet ends a session the agent forgot or died holding.
                "activeDeadlineSeconds": self.lifetime_s,
                # It runs repo-controlled install code with the network on:
                # no cluster credential, no Secret, nothing to take.
                "automountServiceAccountToken": False,
                "containers": [{
                    "name": _MAIN, "image": image, "command": ["sh", "-c", _LOOP],
                    "volumeMounts": [
                        {"name": "work", "mountPath": _X, "subPath": self._sub(f"live{gen}")},
                        {"name": "work", "mountPath": f"{_IO}/inputs", "subPath": self._sub("inputs")},
                        {"name": "work", "mountPath": f"{_IO}/outputs", "subPath": self._sub("outputs")},
                        {"name": "models", "mountPath": _SRC, "subPath": models_sub, "readOnly": True},
                    ],
                    "resources": self.resources,
                    # Root with default capabilities: apt-get needs them, as it
                    # does in a Kaniko build. No escalation beyond that.
                    "securityContext": {"allowPrivilegeEscalation": False},
                }],
                "volumes": [
                    {"name": "work", "persistentVolumeClaim": {"claimName": self.work_pvc}},
                    {"name": "models", "persistentVolumeClaim": {"claimName": self.models_pvc,
                                                                 "readOnly": True}},
                ],
            },
        }

    def _ensure(self, spec: EnvSpec) -> None:
        """A running container for this spec's (base, install mode, code path)."""
        key = f"{spec.base_image}@{spec.base_digest}|{spec.install_mode}|{spec.mount.code_path}"
        if self.live.get("pod") and self.live.get("key") == key:
            return
        self.stop()
        gen = int(self.live.get("gen") or 0) + 1
        pod = k8s.object_name("envbuild-l", self.job_id, f"g{gen}")
        self.client.create_pod(self._pod_manifest(pod, f"{spec.base_image}@{spec.base_digest}", gen))
        self.live.update({"pod": pod, "key": key, "gen": gen, "n": 0, "done": []})
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            phase = (self.client.get_pod(pod).get("status") or {}).get("phase")
            if phase == "Running":
                return
            if phase in ("Failed", "Succeeded"):
                break
            time.sleep(2)
        self.stop()
        raise InfraError(f"live pod {pod} did not start (image pull or scheduling)")

    def stop(self) -> None:
        if self.live.get("pod"):
            self.client.delete_pod(self.live["pod"])
        self.live.update({"pod": None, "key": None, "done": []})

    # -- one command ---------------------------------------------------------
    def _exec(self, script: str, timeout_s: int) -> tuple[int | None, str, float]:
        """Run `script` in the container. (exit code or None on timeout, log, seconds)."""
        self.live["n"] = n = int(self.live["n"]) + 1
        x = self.work_mount / self._sub(f"live{self.live['gen']}")
        tmp = x / "in" / f"{n}.tmp"
        tmp.write_text(script)
        tmp.rename(x / "in" / f"{n}.sh")       # atomic: the loop never runs half a script
        rc_file, log_file = x / "out" / f"{n}.rc", x / "out" / f"{n}.log"
        started, checked = time.monotonic(), time.monotonic()
        while time.monotonic() - started < timeout_s + 30:
            if rc_file.exists():
                text = log_file.read_text(errors="replace") if log_file.exists() else ""
                return int(rc_file.read_text().strip() or 1), text, time.monotonic() - started
            if time.monotonic() - checked > 20:
                checked = time.monotonic()
                if (self.client.get_pod(self.live["pod"]).get("status") or {}).get("phase") != "Running":
                    self.stop()
                    raise InfraError("the live pod ended while a command was running "
                                     "(evicted, out of memory, or past its lifetime)")
            time.sleep(1)
        # The command outlived its deadline and cannot be killed from outside
        # without exec: the container goes, and the next round replays.
        text = log_file.read_text(errors="replace") if log_file.exists() else ""
        self.stop()
        return None, text, time.monotonic() - started

    @staticmethod
    def _prelude(spec: EnvSpec, mount: MountContract) -> str:
        """What the image would carry: its ENV, and the run-time mounts as links."""
        lines = [s.instruction.replace("ENV ", "export ", 1)
                 for s in render(spec).steps if s.kind == "env"]
        links = [(f"{_IO}/inputs", mount.input_path), (f"{_IO}/outputs", mount.output_path)]
        if spec.install_mode != "installed":
            links.append((_SRC, mount.code_path))    # mounted mode: the source IS the code path
        for target, path in links:
            lines.append(f"[ -e {shlex.quote(path)} ] || {{ mkdir -p \"$(dirname {shlex.quote(path)})\" "
                         f"&& ln -s {target} {shlex.quote(path)}; }}")
        return "\n".join(lines) + "\n"

    # -- Builder protocol ------------------------------------------------------
    def build(self, spec: EnvSpec, context_dir: str, timeout_s: int) -> BuildResult:
        rendered = render(spec)
        self._ensure(spec)
        started, ran = time.monotonic(), 0
        for step in rendered.steps:
            text = step.instruction
            if step.kind == "copy":
                body = (f"mkdir -p {shlex.quote(spec.mount.code_path)} && "
                        f"cp -a {_SRC}/. {shlex.quote(spec.mount.code_path)}/")
            elif text.startswith("RUN "):
                body = text[len("RUN "):]
            else:
                continue                        # FROM, ENV, WORKDIR, LABEL, ENTRYPOINT: not commands
            key = _step_key(text)
            if key in self.live["done"]:
                continue
            ran += 1
            left = max(30, int(timeout_s - (time.monotonic() - started)))
            rc, log, _ = self._exec(self._prelude(spec, spec.mount) + body + "\n", left)
            if rc != 0:
                out = logtools.decode(log)
                if rc is None:
                    out = f"ENVBUILD_TIMEOUT: live step {step.kind} did not finish in {left}s\n{out}"
                return BuildResult(ok=False, failed_step_index=step.index, failed_step_kind=step.kind,
                                   failed_step_field=step.field, stderr=out[-20000:],
                                   duration_s=time.monotonic() - started, dockerfile=rendered.text,
                                   vertices_total=ran)
            self.live["done"].append(key)
        return BuildResult(ok=True, image_ref="live", stderr="", duration_s=time.monotonic() - started,
                           dockerfile=rendered.text, vertices_total=ran,
                           cache_hits=len(self.live["done"]) - ran)

    # -- Verifier protocol -------------------------------------------------------
    def image_size(self, ref: str) -> int | None:
        return None

    def run(self, image_ref: str, code_ref: str, cmd: list[str], mount: MountContract,
            timeout_s: int, network: bool = False, env: dict | None = None) -> RunResult:
        spec = self._spec
        script = self._prelude(spec, mount)
        script += "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in (env or {}).items())
        script += f"mkdir -p {shlex.quote(mount.workdir)} && cd {shlex.quote(mount.workdir)} || exit 97\n"
        # coreutils `timeout` is in every base we draft; without it the session
        # deadline in _exec still applies, at the cost of the container.
        script += (f"if command -v timeout >/dev/null; then exec timeout -k 10 {int(timeout_s)} "
                   f"{shlex.join(cmd)}; else exec {shlex.join(cmd)}; fi\n")
        rc, log, secs = self._exec(script, timeout_s)
        if rc is None or rc == 124:
            return RunResult(False, 124, "", f"ENVBUILD_TIMEOUT: did not finish in {timeout_s}s\n{log[-4000:]}",
                             secs, timed_out=True)
        return RunResult(rc == 0, rc, "", log[-20000:], secs)

    def climb_with(self, spec: EnvSpec) -> "LiveSession":
        """`Verifier.run` is not handed the spec, and the prelude needs it."""
        self._spec = spec
        return self

    def outputs_listing(self) -> list[str]:
        root = self.work_mount / self._sub("outputs")
        return sorted(str(p.relative_to(root)) for p in root.rglob("*"))[:100] if root.is_dir() else []

    def cleanup(self, job_id: str) -> None:
        self.stop()
