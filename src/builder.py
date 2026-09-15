"""L0: turn an EnvSpec into a pushed, digest-addressed image.

This module is one half of the seam the infra team builds against. `Builder` is
a Protocol with exactly two methods; Phase 0 ships `LocalBuildKitBuilder`
(standalone buildkitd + a local registry over gRPC), they ship the in-cluster
one, and driver.py never changes.

Build and verify are deliberately *separate* protocols: they are different pods
with different isolation requirements, which is what makes gVisor-on-execution a
drop-in later rather than a rewrite.

`failed_step_index` is the non-negotiable part. Without it the classifier sees a
wall of text; with it, the repair prompt gets "the apt layer failed, here are the
packages in that layer" and the action space collapses to something tractable.
`buildctl --progress=rawjson` gives per-vertex events natively.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Protocol

from envspec import EnvSpec, Step, render
from errors import InfraError

_BUILDER_UNREACHABLE = re.compile(
    r"connection refused|no such host|context deadline exceeded while dialing|"
    r"transport is closing|failed to dial|Unavailable", re.IGNORECASE)

_VERTEX_NUM = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$", re.S)
_WS = re.compile(r"[\s\\]+")


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
    cache_hits: int = 0
    # Denominator for cache_hits. A fully-cached solve can emit no vertices at
    # all, so `cache_hits: 0` alone is ambiguous -- 0/0 and 0/8 mean opposite
    # things when you read the corpus histogram later.
    vertices_total: int = 0
    dockerfile: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Builder(Protocol):
    """Seam: swap LocalBuildKitBuilder for the in-cluster builder."""

    def build(self, spec: EnvSpec, context_tar: bytes, timeout_s: int) -> BuildResult: ...
    def cleanup(self, job_id: str) -> None: ...


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def map_vertex_to_step(vertex_name: str, steps: list[Step]) -> Step | None:
    """BuildKit vertex name -> the EnvSpec-tagged step that produced it.

    Text match first (exact and stable), the `[N/M]` ordinal as a fallback.

    ponytail: longest-common-prefix scoring, not a real parser. BuildKit vertex
    names are the rendered instruction, so this is exact in practice; if a future
    frontend starts truncating names, switch to emitting an explicit
    `# envbuild:step=<i>` comment per instruction and match on that.
    """
    m = _VERTEX_NUM.match(vertex_name.strip())
    ordinal, body = (int(m.group(1)), m.group(3)) if m else (None, vertex_name)
    body = _norm(body)

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
    if best is not None and best_score >= 8:
        return best
    if ordinal and 1 <= ordinal <= len(steps):
        return steps[ordinal - 1]
    return None


@dataclass
class _Vertex:
    name: str = ""
    cached: bool = False
    error: str = ""
    logs: list[str] = field(default_factory=list)


def parse_rawjson(stream: str) -> tuple[dict[str, _Vertex], list[str]]:
    """`buildctl --progress=rawjson` -> vertices keyed by digest, plus parse errors.

    Each line is a SolveStatus: vertexes carry name/cached/error, logs carry
    base64 payloads tagged with their vertex digest.
    """
    vertices: dict[str, _Vertex] = {}
    bad: list[str] = []
    for line in stream.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            bad.append(line[:120])
            continue
        for v in msg.get("vertexes") or []:
            vx = vertices.setdefault(v.get("digest", ""), _Vertex())
            vx.name = v.get("name") or vx.name
            vx.cached = bool(v.get("cached")) or vx.cached
            if v.get("error"):
                vx.error = v["error"]
        for lg in msg.get("logs") or []:
            vx = vertices.setdefault(lg.get("vertex", ""), _Vertex())
            data = lg.get("data") or ""
            try:
                vx.logs.append(base64.b64decode(data).decode("utf-8", "replace"))
            except (ValueError, TypeError):
                vx.logs.append(str(data))
    return vertices, bad


class LocalBuildKitBuilder:
    """buildctl -> standalone buildkitd -> local registry.

    The embedded/docker builder cannot do registry cache export and does not
    hand back structured progress, and it is not the topology that gets deployed;
    this is.
    """

    def __init__(self, job_id: str, addr: str, registry: str, builder_version: str,
                 insecure_registry: bool = True, prune_on_cleanup: bool = True,
                 flat_registry: bool = False, workdir: str | Path = "/tmp/envbuild"):
        self.job_id = job_id
        self.addr = addr
        self.registry = registry.rstrip("/")
        self.builder_version = builder_version
        self.insecure = insecure_registry
        self.prune_on_cleanup = prune_on_cleanup
        # Docker Hub (and similar) allow exactly namespace/repository -- no
        # deeper nesting -- unlike the local registry:2 / GHCR / ECR, which all
        # tolerate an arbitrary path. A flat registry needs job identity folded
        # into the tag instead of the path.
        self.flat_registry = flat_registry
        self.workdir = Path(workdir) / job_id
        self.attempt = 0

    def check_builder(self) -> str:
        """Preflight against buildkitd. `--addr` is a network endpoint, and the
        base compose file does not publish it -- a host-side run without
        compose.host.yaml fails here, before anything expensive happens."""
        try:
            out = subprocess.run(["buildctl", "--addr", self.addr, "debug", "workers"],
                                 capture_output=True, text=True, timeout=60, check=False)
        except FileNotFoundError as exc:
            raise InfraError(
                "buildctl is not on PATH. Extract it from the pinned builder:\n"
                "    docker run --rm --entrypoint cat moby/buildkit:v0.24.0 "
                "/usr/bin/buildctl > ~/.local/bin/buildctl && chmod +x ~/.local/bin/buildctl"
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfraError(f"buildctl unusable: {exc}") from exc
        if out.returncode != 0:
            raise InfraError(
                f"buildkitd unreachable at {self.addr}: {out.stderr.strip()[:300]}\n"
                "  Start it and publish it for host-side runs:\n"
                "    docker compose -f compose.yaml -f compose.host.yaml up -d buildkitd registry\n"
                "    export ENVBUILD_BUILDKIT_HOST=tcp://127.0.0.1:1234")
        return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "ok"

    # -- internals -------------------------------------------------------
    def _image_repo(self) -> str:
        if self.flat_registry:
            return f"{self.registry}/envbuild"
        return f"{self.registry}/envbuild/{self.job_id}"

    def _image_name(self) -> str:
        tag = f"{self.job_id}-a{self.attempt}" if self.flat_registry else f"a{self.attempt}"
        return f"{self._image_repo()}:{tag}"

    def _cache_ref(self, spec: EnvSpec) -> str:
        # Keyed on the base image, so every model sharing a base shares the cache.
        if self.flat_registry:
            return f"{self.registry}/envbuild-cache:{spec.image_ref_base}"
        return f"{self.registry}/envbuild-cache/{spec.image_ref_base}"

    # -- Builder protocol ------------------------------------------------
    def build(self, spec: EnvSpec, context_tar: bytes, timeout_s: int) -> BuildResult:
        self.attempt += 1
        rendered = render(spec)
        run_dir = self.workdir / f"a{self.attempt}"
        ctx_dir, df_dir = run_dir / "context", run_dir / "df"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        df_dir.mkdir(parents=True, exist_ok=True)
        (df_dir / "Dockerfile").write_text(rendered.text)

        # Mounted mode COPYs nothing, so it gets an empty context on purpose:
        # a 200 MB context that no instruction reads is pure latency.
        if spec.install_mode == "installed" and context_tar:
            (run_dir / "context.tar").write_bytes(context_tar)
            subprocess.run(["tar", "-xf", str(run_dir / "context.tar"), "-C", str(ctx_dir)],
                           check=True, capture_output=True)

        name = self._image_name()
        insecure = ",registry.insecure=true" if self.insecure else ""
        meta = run_dir / "metadata.json"
        cmd = [
            "buildctl", "--addr", self.addr, "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={ctx_dir}",
            "--local", f"dockerfile={df_dir}",
            "--output", f"type=image,name={name},push=true{insecure}",
            "--export-cache", f"type=registry,ref={self._cache_ref(spec)},mode=max{insecure}",
            "--import-cache", f"type=registry,ref={self._cache_ref(spec)}{insecure}",
            "--metadata-file", str(meta),
            "--progress", "rawjson",
        ]

        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
            raw, tail, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            raw = (exc.stdout.decode("utf-8", "replace")
                   if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            tail = f"ENVBUILD_TIMEOUT: build exceeded {timeout_s}s"
            rc = 124
        except OSError as exc:
            raise InfraError(f"buildctl not runnable: {exc}") from exc
        duration = time.monotonic() - started

        if rc != 0 and not timed_out and _BUILDER_UNREACHABLE.search(tail or ""):
            raise InfraError(f"lost buildkitd at {self.addr} mid-build: {tail.strip()[:300]}")

        vertices, _bad = parse_rawjson(raw)
        cache_hits = sum(1 for v in vertices.values() if v.cached)
        vertices_total = len(vertices)

        if rc == 0 and not timed_out:
            digest = None
            if meta.exists():
                try:
                    digest = json.loads(meta.read_text()).get("containerimage.digest")
                except (OSError, json.JSONDecodeError):
                    digest = None
            ref = f"{self._image_repo()}@{digest}" if digest else name
            # image_bytes is filled by the verifier after it pulls: measuring it
            # here would mean a second registry round-trip that plain HTTP breaks.
            return BuildResult(ok=True, image_ref=ref, image_digest=digest,
                               image_bytes=None, stderr="",
                               duration_s=duration, cache_hits=cache_hits,
                               vertices_total=vertices_total, dockerfile=rendered.text)

        # Failure: find the vertex that errored and hand back only its logs.
        failed = next((v for v in vertices.values()
                       if v.error and "context canceled" not in v.error), None)
        step = map_vertex_to_step(failed.name, rendered.steps) if failed else None
        stderr = "".join(failed.logs)[-20000:] if failed else ""
        if failed and failed.error:
            stderr += f"\n[vertex error] {failed.error}"
        if not stderr.strip():
            stderr = tail[-20000:]
        return BuildResult(
            ok=False,
            failed_step_index=step.index if step else None,
            failed_step_kind=step.kind if step else None,
            failed_step_field=step.field if step else None,
            stderr=stderr, duration_s=duration, cache_hits=cache_hits,
            vertices_total=vertices_total, dockerfile=rendered.text,
        )

    def cleanup(self, job_id: str) -> None:
        """Reclaim what this job left behind. Called from the driver's `finally`;
        on a laptop, skipping it fills the disk in an afternoon."""
        shutil.rmtree(self.workdir.parent / job_id, ignore_errors=True)
        if self.prune_on_cleanup:
            subprocess.run(["buildctl", "--addr", self.addr, "prune",
                            "--keep-storage", "20000000000"],
                           capture_output=True, check=False, timeout=300)
