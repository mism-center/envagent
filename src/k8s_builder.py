"""L0 (in-cluster): turn an EnvSpec into a pushed, digest-addressed image via
Kaniko, for clusters whose admission policy blocks BuildKit entirely.

BuildKit needs one of two things to run: privileged mode, or (in rootless
mode) an `Unconfined` seccomp profile plus privilege escalation for its
uid-mapping helper (`newuidmap`). Many clusters' admission policy denies both
outright -- Kaniko builds a Dockerfile in pure userspace instead: no
privileged mode, no custom seccomp, no uid-mapping helper, so it runs under
restrictive policy that blocks BuildKit entirely.

Same `Builder` protocol and `BuildResult` shape as `LocalBuildKitBuilder`
(see builder.py) -- driver.py does not need to change to pick this backend,
only `make_builder()`'s selection of which class to construct.

Kaniko has no gRPC control connection the way buildkitd does, so the two
things `LocalBuildKitBuilder` gets for free -- "send the build request" and
"read back structured per-vertex progress" -- have to be done by shelling out
to `kubectl` instead: a Pod is the connection, its logs are the progress
stream. The one wrinkle that follows from that: kubectl can only stage files
into, or read files out of, a *running* container, and Kaniko's own container
exits the moment the build ends -- so a small `busybox` init container runs
as a native sidecar (`restartPolicy: Always`, k8s 1.28+) for the pod's entire
lifetime, used once before the build (stage the context in) and once after
(read the digest file out), with Kaniko itself as the sole regular container
in between.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from builder import BuildResult, map_vertex_to_step
from envspec import EnvSpec, render
from errors import InfraError

_UNREACHABLE = re.compile(
    r"Unable to connect to the server|connection refused|no such host|"
    r"context deadline exceeded|dial tcp.*timeout|TLS handshake timeout",
    re.IGNORECASE)

# Kaniko logs the literal rendered instruction on its own INFO line (color
# ANSI codes and a [seconds] timestamp first, no "Step N/M" the way classic
# `docker build` prints -- confirmed against a real build, not assumed):
#   \x1b[36mINFO\x1b[0m[0014] RUN --mount=type=cache,...     pip install ...
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_KANIKO_LOG_LINE = re.compile(r"^INFO\[\d+\]\s+(.*)$")

_WORKSPACE = "/workspace"
_STAGE_IN = "stage-in"
_STAGE_OUT = "stage-out"
_MAIN = "kaniko"


class K8sBuilder:
    """kubectl -> a Kaniko Pod -> a registry. No buildkitd, no local daemon."""

    def __init__(self, job_id: str, namespace: str, registry: str,
                 kaniko_image: str = "gcr.io/kaniko-project/executor:v1.23.2",
                 helper_image: str = "busybox:1.36",
                 docker_config_secret: str = "envbuild-registry-auth",
                 insecure_registry: bool = False, flat_registry: bool = False,
                 kubeconfig: str | None = None, prune_on_cleanup: bool = True,
                 workdir: str | Path = "/tmp/envbuild"):
        self.job_id = job_id
        self.namespace = namespace
        self.registry = registry.rstrip("/")
        self.kaniko_image = kaniko_image
        self.helper_image = helper_image
        self.docker_config_secret = docker_config_secret
        self.insecure = insecure_registry
        self.flat_registry = flat_registry
        self.kubeconfig = kubeconfig
        self.prune_on_cleanup = prune_on_cleanup
        self.workdir = Path(workdir) / job_id
        self.attempt = 0

    # -- kubectl plumbing --------------------------------------------------
    def _kubectl(self, args: list[str], timeout: int = 60, check: bool = False,
                 input_text: str | None = None):
        base = ["kubectl"]
        if self.kubeconfig:
            base += ["--kubeconfig", self.kubeconfig]
        try:
            return subprocess.run(base + args, capture_output=True, text=True,
                                  timeout=timeout, check=check, input=input_text)
        except FileNotFoundError as exc:
            raise InfraError("kubectl is not on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise InfraError(f"kubectl {' '.join(args[:2])} timed out after {timeout}s") from exc

    def check_builder(self) -> str:
        """Preflight: can we even talk to the cluster and act in this namespace."""
        out = self._kubectl(["auth", "can-i", "create", "pods", "-n", self.namespace])
        if out.returncode != 0 or out.stdout.strip() != "yes":
            raise InfraError(
                f"cannot create pods in namespace {self.namespace!r}: "
                f"{(out.stderr or out.stdout).strip()[:300]}")
        return "ok"

    # -- naming, same shape as LocalBuildKitBuilder -------------------------
    def _image_repo(self) -> str:
        if self.flat_registry:
            return f"{self.registry}/envbuild"
        return f"{self.registry}/envbuild/{self.job_id}"

    def _image_name(self) -> str:
        tag = f"{self.job_id}-a{self.attempt}" if self.flat_registry else f"a{self.attempt}"
        return f"{self._image_repo()}:{tag}"

    def _pod_name(self) -> str:
        # k8s object names: lowercase alnum + '-', <=63 chars. job_id is
        # already that shape; the attempt/random suffix just needs trimming.
        base = f"envbuild-{self.job_id}-a{self.attempt}".lower()
        return base[:55] + "-" + uuid.uuid4().hex[:6]

    # -- registry credentials ------------------------------------------------
    def ensure_registry_secret(self, dockerconfigjson_path: str | Path) -> None:
        """Idempotent: (re)create the Secret Kaniko reads its push credentials
        from. Safe to call every attempt -- `kubectl apply` no-ops when nothing
        changed."""
        out = self._kubectl([
            "create", "secret", "generic", self.docker_config_secret,
            "-n", self.namespace,
            f"--from-file=.dockerconfigjson={dockerconfigjson_path}",
            "--type=kubernetes.io/dockerconfigjson",
            "--dry-run=client", "-o", "yaml",
        ])
        if out.returncode != 0:
            raise InfraError(f"could not render registry secret: {out.stderr.strip()[:300]}")
        applied = self._kubectl(["apply", "-f", "-"], input_text=out.stdout)
        if applied.returncode != 0:
            raise InfraError(f"could not apply registry secret: {applied.stderr.strip()[:300]}")

    # -- pod manifest --------------------------------------------------------
    def _pod_manifest(self, pod_name: str, name: str) -> dict:
        insecure_args = []
        if self.insecure:
            insecure_args = ["--insecure", "--insecure-pull", "--skip-tls-verify",
                              "--skip-tls-verify-pull"]
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.namespace,
                        "labels": {"app": "envbuild-kaniko", "job-id": self.job_id}},
            "spec": {
                "restartPolicy": "Never",
                "initContainers": [
                    {
                        # Classic (non-sidecar) init container: k8s guarantees
                        # this COMPLETES before Kaniko starts, which is the
                        # actual guarantee needed here -- a sidecar only has to
                        # be *running*, which races Kaniko's own start instead
                        # of blocking it.
                        "name": _STAGE_IN,
                        "image": self.helper_image,
                        "command": ["sh", "-c",
                                    f"until [ -f {_WORKSPACE}/.ready ]; do sleep 1; done"],
                        "volumeMounts": [{"name": "workspace", "mountPath": _WORKSPACE}],
                    },
                    {
                        # Native sidecar (restartPolicy: Always, k8s 1.28+):
                        # stays running for the pod's whole life, which is what
                        # lets `kubectl exec` read the digest file back out
                        # after Kaniko's own container has already terminated.
                        "name": _STAGE_OUT,
                        "image": self.helper_image,
                        "restartPolicy": "Always",
                        "command": ["sh", "-c",
                                    f"until [ -f {_WORKSPACE}/.done ]; do sleep 1; done"],
                        "volumeMounts": [{"name": "workspace", "mountPath": _WORKSPACE}],
                    },
                ],
                "containers": [{
                    "name": _MAIN,
                    "image": self.kaniko_image,
                    "args": [
                        f"--dockerfile={_WORKSPACE}/dockerfile/Dockerfile",
                        f"--context=dir://{_WORKSPACE}/context",
                        f"--destination={name}",
                        f"--digest-file={_WORKSPACE}/digest.txt",
                        "--cache=false",  # Phase 0: correctness first, cache export is a later win
                        *insecure_args,
                    ],
                    "volumeMounts": [
                        {"name": "workspace", "mountPath": _WORKSPACE},
                        {"name": "docker-config", "mountPath": "/kaniko/.docker"},
                    ],
                }],
                "volumes": [
                    {"name": "workspace", "emptyDir": {}},
                    {"name": "docker-config", "secret": {
                        "secretName": self.docker_config_secret,
                        "items": [{"key": ".dockerconfigjson", "path": "config.json"}],
                    }},
                ],
            },
        }

    def _wait_for(self, pod_name: str, jsonpath: str, want_nonempty: bool,
                  deadline: float) -> str:
        while time.monotonic() < deadline:
            out = self._kubectl(["get", "pod", pod_name, "-n", self.namespace,
                                 "-o", f"jsonpath={jsonpath}"])
            val = (out.stdout or "").strip()
            if (val != "") == want_nonempty:
                return val
            time.sleep(2)
        raise InfraError(f"timed out waiting for pod {pod_name} ({jsonpath})")

    # -- Builder protocol ------------------------------------------------
    def build(self, spec: EnvSpec, context_tar: bytes, timeout_s: int) -> BuildResult:
        self.attempt += 1
        rendered = render(spec)
        run_dir = self.workdir / f"a{self.attempt}"
        ctx_dir, df_dir = run_dir / "context", run_dir / "df"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        df_dir.mkdir(parents=True, exist_ok=True)
        (df_dir / "Dockerfile").write_text(rendered.text)
        if spec.install_mode == "installed" and context_tar:
            (run_dir / "context.tar").write_bytes(context_tar)
            subprocess.run(["tar", "-xf", str(run_dir / "context.tar"), "-C", str(ctx_dir)],
                           check=True, capture_output=True)

        name = self._image_name()
        pod_name = self._pod_name()
        started = time.monotonic()
        deadline = started + timeout_s
        manifest = self._pod_manifest(pod_name, name)

        applied = self._kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        if applied.returncode != 0:
            raise InfraError(f"could not create build pod: {applied.stderr.strip()[:300]}")

        try:
            return self._run_build(pod_name, name, rendered, deadline, started)
        finally:
            self._kubectl(["delete", "pod", pod_name, "-n", self.namespace,
                          "--wait=false", "--ignore-not-found"], timeout=30)

    def _run_build(self, pod_name, name, rendered, deadline, started) -> BuildResult:
        run_dir = self.workdir / f"a{self.attempt}"
        ctx_dir, df_dir = run_dir / "context", run_dir / "df"

        # 1. Wait for the staging container to be running, then stage inputs.
        self._wait_for(pod_name,
                       "{.status.initContainerStatuses[0].state.running}",
                       want_nonempty=True, deadline=deadline)
        for src, dst in ((ctx_dir, f"{_WORKSPACE}/context"), (df_dir, f"{_WORKSPACE}/dockerfile")):
            # Trailing "/." -- without it, kubectl cp nests the source dir's
            # own basename one level deeper (e.g. .../dockerfile/df/Dockerfile
            # instead of .../dockerfile/Dockerfile).
            cp = self._kubectl(["cp", f"{src}/.", f"{self.namespace}/{pod_name}:{dst}",
                               "-c", _STAGE_IN], timeout=120)
            if cp.returncode != 0:
                raise InfraError(f"failed to stage {src} into build pod: {cp.stderr.strip()[:300]}")
        ready = self._kubectl(["exec", pod_name, "-n", self.namespace, "-c", _STAGE_IN,
                              "--", "touch", f"{_WORKSPACE}/.ready"], timeout=30)
        if ready.returncode != 0:
            raise InfraError(f"failed to release build pod: {ready.stderr.strip()[:300]}")

        # 2. Wait for Kaniko itself (the one regular container) to terminate.
        try:
            term = self._wait_for(pod_name,
                                  "{.status.containerStatuses[0].state.terminated.exitCode}",
                                  want_nonempty=True, deadline=deadline)
        except InfraError:
            logs = self._kubectl(["logs", pod_name, "-n", self.namespace, "-c", _MAIN],
                                 timeout=30).stdout or ""
            return BuildResult(ok=False, stderr=f"ENVBUILD_TIMEOUT: build exceeded {timeout_s}s\n"
                               + logs[-4000:], duration_s=time.monotonic() - started,
                               dockerfile=rendered.text)

        logs_out = self._kubectl(["logs", pod_name, "-n", self.namespace, "-c", _MAIN], timeout=30)
        logs = logs_out.stdout or ""
        duration = time.monotonic() - started
        exit_code = int(term)

        if exit_code == 0:
            digest = None
            cat = self._kubectl(["exec", pod_name, "-n", self.namespace, "-c", _STAGE_OUT,
                                "--", "cat", f"{_WORKSPACE}/digest.txt"], timeout=30)
            if cat.returncode == 0 and cat.stdout.strip():
                digest = cat.stdout.strip()
            self._kubectl(["exec", pod_name, "-n", self.namespace, "-c", _STAGE_OUT,
                          "--", "touch", f"{_WORKSPACE}/.done"], timeout=30)
            ref = f"{self._image_repo()}@{digest}" if digest else name
            return BuildResult(ok=True, image_ref=ref, image_digest=digest,
                               image_bytes=None, stderr="", duration_s=duration,
                               cache_hits=0, vertices_total=0, dockerfile=rendered.text)

        self._kubectl(["exec", pod_name, "-n", self.namespace, "-c", _STAGE_OUT,
                      "--", "touch", f"{_WORKSPACE}/.done"], timeout=30)
        if _UNREACHABLE.search(logs) or _UNREACHABLE.search(logs_out.stderr or ""):
            raise InfraError(f"lost the cluster mid-build: {(logs_out.stderr or logs)[:300]}")

        step = _match_kaniko_step(logs, rendered.steps)
        return BuildResult(
            ok=False,
            failed_step_index=step.index if step else None,
            failed_step_kind=step.kind if step else None,
            failed_step_field=step.field if step else None,
            stderr=logs[-20000:], duration_s=duration,
            cache_hits=0, vertices_total=0, dockerfile=rendered.text,
        )

    def cleanup(self, job_id: str) -> None:
        """Reclaim local staging dirs; the pod is already deleted per-attempt
        in `build()`'s `finally`, so there's no cluster-side state to prune."""
        import shutil
        shutil.rmtree(self.workdir.parent / job_id, ignore_errors=True)


_INSTRUCTION = re.compile(
    r"^(FROM|RUN|COPY|ADD|WORKDIR|ENV|CMD|ENTRYPOINT|USER|ARG|LABEL|EXPOSE|"
    r"VOLUME|STOPSIGNAL|SHELL|ONBUILD|HEALTHCHECK)\b")


def _match_kaniko_step(logs: str, steps):
    """Find the last rendered instruction Kaniko logged before the failure and
    map it back to the EnvSpec field that produced it. Kaniko echoes the exact
    rendered line on its own INFO line rather than a "Step N/M" header, so
    buildctl's own vertex matcher (plain longest-common-prefix scoring, no
    ordinal needed) works directly against that text."""
    last_text = None
    for raw in logs.splitlines():
        m = _KANIKO_LOG_LINE.match(_ANSI.sub("", raw).strip())
        if m and _INSTRUCTION.match(m.group(1)):
            last_text = m.group(1)
    if not last_text:
        return None
    return map_vertex_to_step(last_text, steps)
