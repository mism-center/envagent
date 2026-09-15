"""L0: turn an EnvSpec into a pushed, digest-addressed image via Kaniko.

This module is one half of the seam the infra team builds against. `Builder` is a
Protocol with exactly two methods; Phase 0 ships `K8sBuilder`, they ship whatever
their own cluster runs, and driver.py never changes.

Build and verify are deliberately *separate* protocols: they are different pods
with different isolation requirements, which is what makes gVisor-on-execution a
drop-in later rather than a rewrite.

Kaniko and not BuildKit: BuildKit needs either privileged mode or (rootless) an
`Unconfined` seccomp profile plus privilege escalation for its uid-mapping helper
(`newuidmap`). Many clusters' admission policy denies both outright. Kaniko builds
a Dockerfile in pure userspace -- no privileged mode, no custom seccomp, no
uid-mapping helper -- so it runs under that policy.

**Bytes move over a mounted volume, never over the API.** The agent writes the
rendered Dockerfile into its own scratch directory on the work PVC; the build pod
mounts the same claim and reads it; Kaniko writes `digest.txt` back to that
directory and the agent reads it off its own mount after the pod exits. That is
why there is no init container, no sidecar, no `.ready`/`.done` handshake and no
`kubectl cp` -- and, more to the point, why the ServiceAccount never needs
`pods/exec`. See k8s.py.

`failed_step_index` is the non-negotiable part. Without it the classifier sees a
wall of text; with it, the repair prompt gets "the apt layer failed, here are the
packages in that layer" and the action space collapses to something tractable.
Kaniko echoes each rendered instruction on its own INFO line, which is what
`match_step` matches against.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol

import k8s
from envspec import EnvSpec, Step, render
from errors import InfraError

# Kaniko logs the literal rendered instruction on its own INFO line (color ANSI
# codes and a [seconds] timestamp first, no "Step N/M" the way classic
# `docker build` prints -- confirmed against a real build, not assumed):
#   \x1b[36mINFO\x1b[0m[0014] RUN pip install ...
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_KANIKO_LOG_LINE = re.compile(r"^INFO\[\d+\]\s+(.*)$")
_INSTRUCTION = re.compile(
    r"^(FROM|RUN|COPY|ADD|WORKDIR|ENV|CMD|ENTRYPOINT|USER|ARG|LABEL|EXPOSE|"
    r"VOLUME|STOPSIGNAL|SHELL|ONBUILD|HEALTHCHECK)\b")
_WS = re.compile(r"[\s\\]+")

# Where the work PVC is mounted *inside the build pod*. The agent sees the same
# bytes at its own mount point plus the job/attempt subPath.
_POD_WORK = "/workspace"
_MAIN = "kaniko"


@dataclass
class BuildResult:
    ok: bool
    image_ref: str | None = None          # pushed ref, digest-addressed
    image_digest: str | None = None
    image_bytes: int | None = None
    failed_step_index: int | None = None  # -> maps back to an EnvSpec field
    failed_step_kind: str | None = None   # "apt" | "pkg" | "cmd" | "copy" | ...
    failed_step_field: str | None = None
    stderr: str = ""
    duration_s: float = 0.0
    # Both stay 0 under Kaniko, which runs with `--cache=false` (Phase 0:
    # correctness first). They are part of the attempt-record contract, so the
    # fields stay even while the builder has nothing to put in them -- a row
    # missing them is not comparable with one that has them.
    cache_hits: int = 0
    vertices_total: int = 0
    dockerfile: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Builder(Protocol):
    """Seam: swap K8sBuilder for the infra team's in-cluster builder.

    `context_dir` is a path on a volume both this process and the build pod can
    read -- not a tar. It used to be `bytes`, streamed in with `kubectl cp`; the
    shared PVC made both the tar and the streaming unnecessary.
    """

    def build(self, spec: EnvSpec, context_dir: str, timeout_s: int) -> BuildResult: ...
    def cleanup(self, job_id: str) -> None: ...


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def match_step(logged_instruction: str, steps: list[Step]) -> Step | None:
    """A rendered instruction Kaniko echoed -> the EnvSpec-tagged step that
    produced it.

    ponytail: longest-common-prefix scoring, not a real parser. Kaniko echoes the
    instruction verbatim (flattened onto one line), so this is exact in practice;
    if a future executor starts truncating the echo, switch to emitting an
    explicit `# envbuild:step=<i>` comment per instruction and match on that.
    """
    body = _norm(logged_instruction)
    best, best_score = None, 0
    for st in steps:
        inst = _norm(st.instruction)
        n = 0
        for a, b in zip(body, inst):
            if a != b:
                break
            n += 1
        if n > best_score:
            best, best_score = st, n
    return best if best_score >= 8 else None


def match_kaniko_step(logs: str, steps: list[Step]) -> Step | None:
    """Last rendered instruction Kaniko logged before the failure, mapped back to
    the EnvSpec field that produced it."""
    last_text = None
    for raw in logs.splitlines():
        m = _KANIKO_LOG_LINE.match(_ANSI.sub("", raw).strip())
        if m and _INSTRUCTION.match(m.group(1)):
            last_text = m.group(1)
    if not last_text:
        return None
    return match_step(last_text, steps)


class K8sBuilder:
    """One Kaniko Pod per attempt. Talks to the API server; passes bytes on a PVC."""

    def __init__(self, job_id: str, client, registry: str,
                 work_pvc: str, work_mount: str,
                 models_pvc: str = "", models_mount: str = "",
                 kaniko_image: str = "gcr.io/kaniko-project/executor:v1.23.2",
                 docker_config_secret: str = "envbuild-registry-auth",
                 insecure_registry: bool = False, flat_registry: bool = False):
        self.job_id = job_id
        self.client = client
        self.registry = registry.rstrip("/")
        self.work_pvc = work_pvc
        self.work_mount = Path(work_mount)
        self.models_pvc = models_pvc
        self.models_mount = models_mount
        self.kaniko_image = kaniko_image
        self.docker_config_secret = docker_config_secret
        self.insecure = insecure_registry
        # Docker Hub (and similar) allow exactly namespace/repository -- no
        # deeper nesting -- unlike registry:2 / GHCR / ECR, which all tolerate an
        # arbitrary path. A flat registry needs job identity folded into the tag
        # instead of the path.
        self.flat_registry = flat_registry
        self.attempt = 0

    # -- preflight -------------------------------------------------------
    def check_builder(self) -> str:
        """Can we create the pods we are about to create? Cheap, and it turns a
        wasted attempt into an immediate, actionable message."""
        if not self.client.can_i("create", "pods"):
            raise InfraError(
                f"not allowed to create pods in namespace "
                f"{self.client.namespace!r}. Apply deploy/rbac.yaml and run as "
                f"that ServiceAccount.")
        return "ok"

    # -- naming ----------------------------------------------------------
    def _image_repo(self) -> str:
        if self.flat_registry:
            return f"{self.registry}/envbuild"
        return f"{self.registry}/envbuild/{self.job_id}"

    def _image_name(self) -> str:
        # Unique per attempt in both layouts, which is what makes it safe to read
        # the digest back by tag if digest.txt is ever missing.
        tag = f"{self.job_id}-a{self.attempt}" if self.flat_registry else f"a{self.attempt}"
        return f"{self._image_repo()}:{tag}"

    def _pod_name(self) -> str:
        return k8s.object_name("envbuild", self.job_id, f"a{self.attempt}")

    # -- scratch ---------------------------------------------------------
    def _sub_path(self) -> str:
        """Where this attempt's scratch lives, relative to the work claim root."""
        return f"{self.job_id}/a{self.attempt}"

    def _scratch(self) -> Path:
        return self.work_mount / self._sub_path()

    # -- pod manifest ----------------------------------------------------
    def _pod_manifest(self, pod_name: str, name: str, context: str,
                      timeout_s: int) -> dict:
        args = [
            f"--dockerfile={_POD_WORK}/Dockerfile",
            f"--context=dir://{context}",
            f"--destination={name}",
            f"--digest-file={_POD_WORK}/digest.txt",
            "--cache=false",   # Phase 0: correctness first, cache export is a later win
        ]
        if self.insecure:
            args += ["--insecure", "--insecure-pull", "--skip-tls-verify",
                     "--skip-tls-verify-pull"]

        mounts = [{"name": "work", "mountPath": _POD_WORK,
                   "subPath": self._sub_path()},
                  {"name": "docker-config", "mountPath": "/kaniko/.docker"}]
        volumes = [
            {"name": "work", "persistentVolumeClaim": {"claimName": self.work_pvc}},
            {"name": "docker-config", "secret": {
                "secretName": self.docker_config_secret,
                "items": [{"key": ".dockerconfigjson", "path": "config.json"}]}},
        ]
        # `installed` mode builds FROM the model source, so the models claim comes
        # along -- read-only, at the same path the agent sees it, so `context` is
        # the same string on both sides.
        if context != f"{_POD_WORK}/context" and self.models_pvc:
            mounts.append({"name": "models", "mountPath": self.models_mount,
                           "readOnly": True})
            volumes.append({"name": "models", "persistentVolumeClaim": {
                "claimName": self.models_pvc, "readOnly": True}})

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.client.namespace,
                         "labels": {"app": "envbuild-kaniko", "job-id": self.job_id}},
            "spec": {
                "restartPolicy": "Never",
                # The kubelet owns the timeout. Nothing here needs the API, so it
                # gets no token to lose.
                "activeDeadlineSeconds": timeout_s,
                "automountServiceAccountToken": False,
                "containers": [{
                    "name": _MAIN,
                    "image": self.kaniko_image,
                    "args": args,
                    "volumeMounts": mounts,
                }],
                "volumes": volumes,
            },
        }

    # -- Builder protocol ------------------------------------------------
    def build(self, spec: EnvSpec, context_dir: str, timeout_s: int) -> BuildResult:
        self.attempt += 1
        rendered = render(spec)
        scratch = self._scratch()
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "Dockerfile").write_text(rendered.text)
        digest_file = scratch / "digest.txt"
        digest_file.unlink(missing_ok=True)      # never read a previous attempt's

        if spec.install_mode == "installed" and context_dir:
            # Kaniko reads the model source in place, read-only, at the same path
            # this process sees it.
            context = str(context_dir)
        else:
            # Mounted mode COPYs nothing, so it gets an empty context on purpose:
            # a 200 MB context that no instruction reads is pure latency.
            (scratch / "context").mkdir(exist_ok=True)
            context = f"{_POD_WORK}/context"

        name = self._image_name()
        pod_name = self._pod_name()
        started = time.monotonic()
        self.client.create_pod(self._pod_manifest(pod_name, name, context, timeout_s))

        try:
            # Watch slightly past the kubelet's own deadline so the pod's
            # DeadlineExceeded is what we observe, not our own impatience.
            exit_code, note = self.client.wait_terminated(
                pod_name, _MAIN, deadline=started + timeout_s + 30)
            logs = self.client.pod_log(pod_name, _MAIN)
            duration = time.monotonic() - started

            if exit_code == 0:
                digest = digest_file.read_text().strip() if digest_file.exists() else None
                ref = f"{self._image_repo()}@{digest}" if digest else name
                # image_bytes is filled by the verifier, which reads it off the
                # registry manifest before deciding whether to run anything.
                return BuildResult(ok=True, image_ref=ref, image_digest=digest,
                                   image_bytes=None, stderr="", duration_s=duration,
                                   dockerfile=rendered.text)

            if exit_code is None:
                # Never ran: a deadline, a pull failure, a scheduling refusal.
                # Real build failures always produce an exit code.
                return BuildResult(ok=False, stderr=f"{note}\n{logs[-4000:]}".strip(),
                                   duration_s=duration, dockerfile=rendered.text)

            step = match_kaniko_step(logs, rendered.steps)
            return BuildResult(
                ok=False,
                failed_step_index=step.index if step else None,
                failed_step_kind=step.kind if step else None,
                failed_step_field=step.field if step else None,
                stderr=logs[-20000:], duration_s=duration,
                dockerfile=rendered.text,
            )
        finally:
            self.client.delete_pod(pod_name)

    def cleanup(self, job_id: str) -> None:
        """Drop this job's scratch. The pod is already deleted per attempt in
        `build()`'s `finally`, so there is no cluster-side state to prune."""
        shutil.rmtree(self.work_mount / job_id, ignore_errors=True)
