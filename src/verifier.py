"""L1-L3: run things against a built image, with the model's code mounted.

The other half of the seam. Separate from `Builder` on purpose -- verification
executes untrusted-ish model code and is where isolation gets hardened later,
while building does not.

One pod per rung, and the pod is the isolation boundary:

  * it carries **no ServiceAccount token** (`automountServiceAccountToken: false`),
    so model code that goes looking for cluster credentials finds none;
  * it is labelled for a **deny-all NetworkPolicy**, which is how `--network none`
    survives the move off docker. A model that only "runs" with live network
    access has not been verified, and that claim has to keep meaning the same
    thing in the corpus before and after this change;
  * the kubelet enforces the timeout via `activeDeadlineSeconds`, so a wedged
    container cannot outlive its rung.

Code and results move on PVCs, never through the API: the model source is mounted
read-only from the models claim, outputs land on the work claim, and this process
reads them off its own mount. Nothing here needs `pods/exec`.
"""

from __future__ import annotations

import io
import shlex
import tarfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol

import k8s
import registry
from envspec import MountContract
from errors import InfraError

_MAIN = "verify"

# The label the deny-all NetworkPolicy in deploy/ selects on. A pod no policy
# selects gets unrestricted traffic, so the *absence* of this label is what
# "network on" means -- see `run(network=True)`.
_DENY_NET_LABEL = ("envbuild.io/network", "deny")


@dataclass
class RunResult:
    ok: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class Verifier(Protocol):
    """Seam: swap K8sVerifier for the infra team's own runner.

    `code_ref` is a path on a volume the verification pod can mount, not a docker
    volume name. `outputs_listing` is declared because it is what `ladder.run_l3`
    actually calls -- this protocol used to declare only `fetch_outputs`, which no
    rung has ever called.
    """

    def run(self, image_ref: str, code_ref: str, cmd: list[str], mount: MountContract,
            timeout_s: int, network: bool = False, env: dict | None = None) -> RunResult: ...
    def outputs_listing(self) -> list[str]: ...
    def fetch_outputs(self, job_id: str) -> bytes: ...
    def cleanup(self, job_id: str) -> None: ...


class K8sVerifier:
    """Runs verification pods in the cluster. No docker socket anywhere."""

    def __init__(self, job_id: str, client, work_pvc: str, work_mount: str,
                 models_pvc: str = "", models_mount: str = "", code_ref: str = "",
                 docker_config_secret: str = "envbuild-registry-auth",
                 memory: str | None = None):
        self.job_id = job_id
        self.client = client
        self.work_pvc = work_pvc
        self.work_mount = Path(work_mount)
        self.models_pvc = models_pvc
        self.models_mount = models_mount
        # The model source, as this process sees it. The models claim is mounted
        # at the same path inside the pod, so nothing has to be rewritten between
        # the two sides.
        self.code_ref = code_ref
        self.docker_config_secret = docker_config_secret
        self.memory = memory
        self._pods: list[str] = []

    # -- preflight -------------------------------------------------------
    def check_cluster(self) -> str:
        if not self.client.can_i("get", "pods/log"):
            raise InfraError(
                f"not allowed to read pod logs in namespace "
                f"{self.client.namespace!r}; verification could not report "
                f"anything it ran. Apply deploy/envbuild.yaml.")
        return "ok"

    def image_size(self, ref: str) -> int | None:
        """Bytes, read off the registry manifest before anything executes.

        Compressed layer sizes, so this reads a little under what
        `docker image inspect` reported for the same image. It feeds exactly one
        decision -- refusing to run above `max_image_bytes` -- and that ceiling
        has headroom.
        """
        return registry.image_size(ref)

    # -- paths on the work claim ------------------------------------------
    def _job_sub(self, leaf: str) -> str:
        return f"{self.job_id}/{leaf}"

    def _job_dir(self, leaf: str) -> Path:
        path = self.work_mount / self._job_sub(leaf)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _assert_job(self, job_id: str) -> None:
        """Guard the namespace: reading one job's outputs under another job's id
        would report the wrong results as success."""
        if job_id != self.job_id:
            raise RuntimeError(f"verifier is bound to job {self.job_id}, got {job_id}")

    def _models_sub(self, code_ref: str) -> str:
        """An absolute path under the models mount -> the subPath in the claim."""
        try:
            return str(Path(code_ref).relative_to(self.models_mount))
        except ValueError as exc:
            raise InfraError(
                f"model source {code_ref} is not under the models mount "
                f"{self.models_mount!r}. Set ENVBUILD_MODELS_MOUNT to the path "
                f"the models claim is mounted at.") from exc

    # -- pod manifest ------------------------------------------------------
    def _pod_manifest(self, pod_name: str, image_ref: str, code_ref: str,
                      cmd: list[str], mount: MountContract, timeout_s: int,
                      network: bool, env: dict | None) -> dict:
        labels = {"app": "envbuild-verify", "job-id": self.job_id}
        if not network:
            labels[_DENY_NET_LABEL[0]] = _DENY_NET_LABEL[1]

        self._job_dir("inputs")
        self._job_dir("outputs")
        mounts = [
            {"name": "work", "mountPath": mount.input_path,
             "subPath": self._job_sub("inputs")},
            {"name": "work", "mountPath": mount.output_path,
             "subPath": self._job_sub("outputs")},
        ]
        volumes = [{"name": "work",
                    "persistentVolumeClaim": {"claimName": self.work_pvc}}]
        if code_ref and self.models_pvc:
            # L1 runs the image alone: no code mount, which is the entire point
            # of that rung. L2/L3 mount the source read-only.
            mounts.append({"name": "models", "mountPath": mount.code_path,
                           "subPath": self._models_sub(code_ref), "readOnly": True})
            volumes.append({"name": "models", "persistentVolumeClaim": {
                "claimName": self.models_pvc, "readOnly": True}})

        container = {
            "name": _MAIN,
            "image": image_ref,
            "command": list(cmd),
            "workingDir": mount.workdir,
            "volumeMounts": mounts,
            "env": [{"name": k, "value": str(v)} for k, v in (env or {}).items()],
            # NOT runAsNonRoot: rocker and micromamba images legitimately run as
            # root and write into their own library paths. Forcing a uid here
            # would fail most of the corpus with an error about the sandbox
            # rather than about the model. Drop what can be dropped instead.
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        }
        if self.memory:
            container["resources"] = {"limits": {"memory": self.memory}}

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.client.namespace,
                         "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "activeDeadlineSeconds": timeout_s,
                # Model code must never be handed a cluster credential.
                "automountServiceAccountToken": False,
                "imagePullSecrets": [{"name": self.docker_config_secret}],
                "containers": [container],
                "volumes": volumes,
            },
        }

    # -- Verifier protocol -------------------------------------------------
    def run(self, image_ref: str, code_ref: str, cmd: list[str], mount: MountContract,
            timeout_s: int, network: bool = False, env: dict | None = None) -> RunResult:
        """One verification pod. Network off by default.

        Kubernetes merges a container's stdout and stderr into one log stream --
        the API offers no way to separate them. The merged text goes in `stderr`,
        because that is what the classifier reads and what lands in the attempt
        record; `stdout` stays empty rather than carrying a duplicate copy of the
        same 20 KB.
        """
        pod_name = k8s.object_name("envbuild-v", self.job_id)
        started = time.monotonic()
        self.client.create_pod(self._pod_manifest(
            pod_name, image_ref, code_ref, cmd, mount, timeout_s, network, env))
        self._pods.append(pod_name)
        try:
            # Watch slightly past the kubelet's own deadline, so the pod's
            # DeadlineExceeded is what we observe rather than our own impatience.
            exit_code, note = self.client.wait_terminated(
                pod_name, _MAIN, deadline=started + timeout_s + 30)
            logs = self.client.pod_log(pod_name, _MAIN)
            duration = time.monotonic() - started
            if exit_code is None:
                timed_out = "DeadlineExceeded" in note or "ENVBUILD_TIMEOUT" in note
                return RunResult(False, 124 if timed_out else 1, "",
                                 f"{note}\n{logs[-4000:]}".strip(), duration,
                                 timed_out=timed_out)
            return RunResult(exit_code == 0, exit_code, "", logs[-20000:], duration)
        finally:
            self.client.delete_pod(pod_name)

    def outputs_listing(self) -> list[str]:
        """What the run actually wrote, relative to `output_path`.

        Relative, so a record reads the same whatever the mount contract happens
        to be -- an absolute `/outputs/r.json` in a job whose contract says
        `/results` is confusing in a row nobody can re-run.
        """
        root = self.work_mount / self._job_sub("outputs")
        if not root.is_dir():
            return []
        return sorted(str(p.relative_to(root)) for p in root.rglob("*"))[:100]

    def fetch_outputs(self, job_id: str) -> bytes:
        """Tar of everything the run wrote, for whoever collects results."""
        self._assert_job(job_id)
        root = self.work_mount / self._job_sub("outputs")
        if not root.is_dir():
            return b""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(root, arcname=".")
        return buf.getvalue()

    def cleanup(self, job_id: str) -> None:
        """Delete any verification pod this job created. Scratch on the work
        claim is removed by the builder's cleanup, which owns that directory."""
        self._assert_job(job_id)
        for pod_name in self._pods:
            self.client.delete_pod(pod_name)
        self._pods.clear()


def quote_cmd(cmd) -> list[str]:
    """Accept a command as a string or a list; always hand the pod a list."""
    if isinstance(cmd, str):
        return shlex.split(cmd)
    return [str(c) for c in cmd]
