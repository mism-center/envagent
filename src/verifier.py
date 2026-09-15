"""L1-L3: run things against a built image, with the model's code mounted.

The other half of the seam. Separate from `Builder` on purpose -- verification
executes untrusted-ish model code and is where isolation gets hardened later,
while building does not.

Two topology constraints drive the shape here:

  * dockerd resolves mount paths on the *host*, not inside the agent container,
    so code is staged into a daemon-managed **named volume** via a tar stream.
    No host paths anywhere -- and it is the same shape as the PVC used in-cluster.
  * the build pod pushes from inside the cluster and this daemon pulls from
    outside it. When those two hostnames differ, the caller sets
    `registry_pull` and the push-side host is rewritten. Digest-addressed, so
    only the host part differs.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Protocol

from envspec import MountContract
from errors import InfraError

# The docker CLI's own words when it cannot reach the daemon. Matching them lets
# a deployment fault be reported as one instead of being classified as a model
# failure and written into the corpus.
_DAEMON_UNREACHABLE = re.compile(
    r"permission denied while trying to connect to the Docker daemon|"
    r"Cannot connect to the Docker daemon|"
    r"Is the docker daemon running|"
    r"dial unix /var/run/docker\.sock", re.IGNORECASE)

_DAEMON_HELP = (
    "the agent cannot reach the docker daemon at /var/run/docker.sock.\n"
    "  The container's user must be in the group that owns the socket:\n"
    "    export DOCKER_GID=$(stat -c %g /var/run/docker.sock)\n"
    "  then relaunch (compose passes it through as `group_add`). `./run.sh`\n"
    "  derives it for you.")


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
    """Seam: swap LocalDockerVerifier for the in-cluster verifier."""

    def stage_code(self, job_id: str, code_tar: bytes) -> str: ...
    def run(self, image_ref: str, code_ref: str, cmd: list[str], mount: MountContract,
            timeout_s: int, network: bool = False, env: dict | None = None) -> RunResult: ...
    def fetch_outputs(self, job_id: str) -> bytes: ...
    def cleanup(self, job_id: str) -> None: ...


def _docker(args: list[str], timeout: int = 120, stdin: bytes | None = None,
            binary: bool = False):
    out = subprocess.run(["docker", *args], input=stdin, capture_output=True,
                         timeout=timeout, check=False, text=not binary)
    if out.returncode != 0:
        err = out.stderr if isinstance(out.stderr, str) else (out.stderr or b"").decode(
            "utf-8", "replace")
        # Raise rather than return: every caller would otherwise have to decide
        # whether "permission denied" was the model's fault. It never is.
        if _DAEMON_UNREACHABLE.search(err):
            raise InfraError(f"docker {args[0]}: {_DAEMON_HELP}")
    return out


class LocalDockerVerifier:
    """Runs verification containers on the host daemon via /var/run/docker.sock."""

    def __init__(self, job_id: str, helper_image: str = "busybox:1.36",
                 registry_pull: str | None = None, memory: str | None = None):
        self.job_id = job_id
        self.helper = helper_image
        self.registry_pull = registry_pull
        self.memory = memory
        self.code_vol = f"envbuild-code-{job_id}"
        self.in_vol = f"envbuild-in-{job_id}"
        self.out_vol = f"envbuild-out-{job_id}"
        self._pulled: set[str] = set()

    # -- helpers ---------------------------------------------------------
    def check_daemon(self) -> str:
        """Preflight. Cheap, and it turns a wasted ten-minute build into an
        immediate, actionable message."""
        out = _docker(["version", "--format", "{{.Server.Version}}"], timeout=60)
        if out.returncode != 0:
            raise InfraError(f"docker version failed: {out.stderr.strip()[:300]}")
        return out.stdout.strip()

    def pull_ref(self, image_ref: str) -> str:
        """Rewrite the push-side registry host to the pull-side one, then pull."""
        ref = image_ref
        if self.registry_pull and "/" in ref:
            ref = self.registry_pull + ref[ref.index("/"):]
        if ref not in self._pulled:
            out = _docker(["pull", ref], timeout=600)
            if out.returncode != 0:
                raise RuntimeError(f"docker pull {ref} failed: {out.stderr[-2000:]}")
            self._pulled.add(ref)
        return ref

    def image_size(self, ref: str) -> int | None:
        """Bytes, measured on the pulled image.

        Not from `buildx imagetools inspect`: that talks HTTPS to the registry
        and a plain-HTTP one makes it fail silently, leaving image_bytes null in
        every record. The verifier pulls anyway, so measure it here.
        """
        out = _docker(["image", "inspect", "--format", "{{.Size}}", ref], timeout=60)
        try:
            return int(out.stdout.strip())
        except (TypeError, ValueError):
            return None

    def _ensure_volumes(self) -> None:
        for vol in (self.code_vol, self.in_vol, self.out_vol):
            _docker(["volume", "create", vol], timeout=60)

    # -- Verifier protocol ----------------------------------------------
    def _assert_job(self, job_id: str) -> None:
        """Guard the volume namespace: staging one job's code into another job's
        volume would verify the wrong source and report it as success."""
        if job_id != self.job_id:
            raise RuntimeError(f"verifier is bound to job {self.job_id}, got {job_id}")

    def stage_code(self, job_id: str, code_tar: bytes) -> str:
        """Unpack the code tar into a named volume; return the volume name."""
        self._assert_job(job_id)
        self._ensure_volumes()
        _docker(["run", "--rm", "-v", f"{self.code_vol}:/stage", self.helper,
                 "sh", "-c", "rm -rf /stage/* /stage/.[!.]* 2>/dev/null || true"], timeout=120)
        out = _docker(["run", "--rm", "-i", "-v", f"{self.code_vol}:/stage", self.helper,
                       "tar", "-xf", "-", "-C", "/stage"], timeout=600,
                      stdin=code_tar, binary=True)
        if out.returncode != 0:
            raise RuntimeError(f"staging code failed: {out.stderr[-2000:]!r}")
        return self.code_vol

    def run(self, image_ref: str, code_ref: str, cmd: list[str], mount: MountContract,
            timeout_s: int, network: bool = False, env: dict | None = None) -> RunResult:
        """One verification container. Network off by default -- a model that only
        'runs' with live network access has not been verified."""
        ref = self.pull_ref(image_ref)
        name = f"envbuild-{self.job_id}-{uuid.uuid4().hex[:8]}"
        args = ["run", "--rm", "--name", name, "--workdir", mount.workdir]
        if not network:
            args += ["--network", "none"]
        if self.memory:
            args += ["--memory", self.memory]
        if code_ref:                      # L1 runs the image alone: no code mount
            args += ["-v", f"{code_ref}:{mount.code_path}"]
        args += ["-v", f"{self.in_vol}:{mount.input_path}",
                 "-v", f"{self.out_vol}:{mount.output_path}"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += [ref, *cmd]

        started = time.monotonic()
        try:
            out = _docker(args, timeout=timeout_s)
            return RunResult(out.returncode == 0, out.returncode,
                             out.stdout[-20000:], out.stderr[-20000:],
                             time.monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            # The CLI died; the container did not. Kill it or the volume stays busy.
            _docker(["rm", "-f", name], timeout=60)
            tail = (exc.stderr or b"") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            if isinstance(tail, bytes):
                tail = tail.decode("utf-8", "replace")
            return RunResult(False, 124, "", f"ENVBUILD_TIMEOUT: exceeded {timeout_s}s\n{tail[-4000:]}",
                             time.monotonic() - started, timed_out=True)

    def outputs_listing(self) -> list[str]:
        """What the run actually wrote, relative to `output_path`.

        The helper mounts the volume at its own path, so the raw `find` output
        would read `/outputs/r.json` even when the contract says `/results` --
        confusing in a record nobody can re-run. Report relative paths.
        """
        out = _docker(["run", "--rm", "-v", f"{self.out_vol}:/mnt/out", self.helper,
                       "sh", "-c", "find /mnt/out -mindepth 1 | head -n 100"], timeout=120)
        return [ln.strip()[len("/mnt/out/"):] for ln in out.stdout.splitlines()
                if ln.strip().startswith("/mnt/out/")]

    def fetch_outputs(self, job_id: str) -> bytes:
        """Tar of everything the run wrote, for whoever collects results."""
        self._assert_job(job_id)
        out = _docker(["run", "--rm", "-v", f"{self.out_vol}:/outputs", self.helper,
                       "tar", "-cf", "-", "-C", "/outputs", "."], timeout=600, binary=True)
        return out.stdout if out.returncode == 0 else b""

    def cleanup(self, job_id: str) -> None:
        """Containers, volumes, pulled images. Called from the driver's `finally`."""
        ids = _docker(["ps", "-aq", "--filter", f"name=envbuild-{job_id}-"], timeout=60).stdout.split()
        if ids:
            _docker(["rm", "-f", *ids], timeout=180)
        for vol in (self.code_vol, self.in_vol, self.out_vol):
            _docker(["volume", "rm", "-f", vol], timeout=120)
        for ref in sorted(self._pulled):
            _docker(["image", "rm", "-f", ref], timeout=180)
        self._pulled.clear()


def quote_cmd(cmd) -> list[str]:
    """Accept a command as a string or a list; always hand docker a list."""
    if isinstance(cmd, str):
        return shlex.split(cmd)
    return [str(c) for c in cmd]
