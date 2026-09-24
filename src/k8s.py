"""A very small Kubernetes API client: enough to run a pod and read its log.

Five calls, one auth story, no dependency. It replaces shelling out to `kubectl`,
but that is a side effect rather than the point -- the point is that this client
*cannot* exec into a pod, so the ServiceAccount it runs as never needs
`pods/exec`. `pods/exec` together with `pods/create` is arbitrary code execution
in the namespace: create a pod mounting any Secret, exec in, read it. Removing the
need for the verb is the security result; deleting the binary is bookkeeping.

Everything the builder and verifier need is create / get / log / delete, plus one
access review for preflight. Bytes move over a mounted PVC, never over the API.

Auth resolution, first hit wins:

  1. an explicit token + server        -- the escape hatch, and how tests inject
  2. the in-cluster ServiceAccount     -- the target state, zero configuration
  3. a kubeconfig file                 -- transitional, for host-driven runs

A kubeconfig whose user is an `exec` credential plugin (kubelogin, EKS, GKE)
raises rather than half-working: resolving it means running an external binary,
which is exactly the shape we are removing.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from errors import InfraError

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_IN_CLUSTER_API = "https://kubernetes.default.svc"

# A container that will never start on its own. Waiting out the pod deadline to
# discover a typo in an image name wastes a whole attempt of the job's budget.
_FATAL_WAITING = {
    "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError",
}


_NAME_BAD = re.compile(r"[^a-z0-9-]+")
_NAME_RUNS = re.compile(r"-{2,}")


def object_name(*parts: str, limit: int = 63) -> str:
    """Build a valid RFC 1123 label out of arbitrary parts, with a random tail.

    Kubernetes object names are lowercase alphanumerics and dashes, and must
    START AND END on an alphanumeric. Trimming after the truncation is not
    cosmetic: a job id that is a UUID lands on a dash roughly one cut in five,
    and the API server then rejects the whole object with a separate validation
    error for the name and for every label that carries it.

    Anything the caller passes -- a model id with a colon, a path with slashes,
    an id someone typed in caps -- is folded down rather than trusted.
    """
    tail = "-" + uuid.uuid4().hex[:6]
    slug = "-".join(p for p in parts if p).lower()
    slug = _NAME_RUNS.sub("-", _NAME_BAD.sub("-", slug)).strip("-")
    slug = slug[:limit - len(tail)].rstrip("-")
    return (slug or "envbuild") + tail


def _b64(data: str) -> str:
    return base64.b64decode(data).decode("utf-8", "replace")


class Client:
    """One cluster, one namespace, one credential."""

    def __init__(self, api_server: str, namespace: str, token: str | None = None,
                 ca_pem: str | None = None, client_cert_pem: str | None = None,
                 client_key_pem: str | None = None, workdir: str | Path = "/tmp/envbuild"):
        self.api_server = api_server.rstrip("/")
        self.namespace = namespace
        self.token = token
        self.workdir = Path(workdir)
        self._tmp: list[Path] = []

        self._ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem \
            else ssl.create_default_context()
        if client_cert_pem and client_key_pem:
            # load_cert_chain only takes paths, so the key lands on disk. Mode
            # 0600 under the job workdir, removed in close(). Deliberate, and
            # only reachable on the transitional kubeconfig path -- an
            # in-cluster ServiceAccount never gets here.
            self.workdir.mkdir(parents=True, exist_ok=True)
            cert = self.workdir / "client.crt"
            key = self.workdir / "client.key"
            for path, text in ((cert, client_cert_pem), (key, client_key_pem)):
                path.write_text(text)
                path.chmod(0o600)
                self._tmp.append(path)
            self._ctx.load_cert_chain(str(cert), str(key))

    # -- construction ----------------------------------------------------
    @classmethod
    def resolve(cls, namespace: str | None = None, token: str | None = None,
                api_server: str | None = None, kubeconfig: str | None = None,
                workdir: str | Path = "/tmp/envbuild") -> "Client":
        if token and api_server:
            return cls(api_server, namespace or "default", token=token, workdir=workdir)

        if (_SA_DIR / "token").exists():
            ns = namespace
            if not ns:
                try:
                    ns = (_SA_DIR / "namespace").read_text(encoding="utf-8").strip()
                except OSError:
                    ns = "default"
            return cls(api_server or _IN_CLUSTER_API, ns,
                       token=(_SA_DIR / "token").read_text(encoding="utf-8").strip(),
                       ca_pem=(_SA_DIR / "ca.crt").read_text(encoding="utf-8"), workdir=workdir)

        if kubeconfig:
            return cls._from_kubeconfig(kubeconfig, namespace, workdir)

        raise InfraError(
            "no Kubernetes credential found. Expected one of:\n"
            "  - ENVBUILD_K8S_TOKEN + ENVBUILD_K8S_API_SERVER\n"
            f"  - an in-cluster ServiceAccount at {_SA_DIR}\n"
            "  - a kubeconfig at [builder] kubeconfig / ENVBUILD_KANIKO_KUBECONFIG")

    @classmethod
    def _from_kubeconfig(cls, path: str, namespace: str | None,
                         workdir: str | Path) -> "Client":
        # pylint: disable=import-outside-toplevel
        import yaml                       # only the kubeconfig path needs it
        try:
            cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise InfraError(f"cannot read kubeconfig {path}: {exc}") from exc

        want = cfg.get("current-context")
        ctx = next((c["context"] for c in cfg.get("contexts") or []
                    if c.get("name") == want), None)
        if not ctx:
            raise InfraError(f"kubeconfig {path} has no usable current-context")
        cluster = next((c["cluster"] for c in cfg.get("clusters") or []
                        if c.get("name") == ctx.get("cluster")), {})
        user = next((u.get("user") or {} for u in cfg.get("users") or []
                     if u.get("name") == ctx.get("user")), {})

        if "exec" in user or "auth-provider" in user:
            raise InfraError(
                f"kubeconfig {path} authenticates with an external credential "
                "plugin (exec/auth-provider). envbuild does not run helper "
                "binaries. Use ENVBUILD_K8S_TOKEN with ENVBUILD_K8S_API_SERVER, "
                "or run the agent in-cluster with a ServiceAccount.")

        server = cluster.get("server")
        if not server:
            raise InfraError(f"kubeconfig {path} names no server for the current context")
        ca = cluster.get("certificate-authority-data")
        return cls(
            server, namespace or ctx.get("namespace") or "default",
            token=user.get("token"),
            ca_pem=_b64(ca) if ca else None,
            client_cert_pem=(_b64(user["client-certificate-data"])
                             if user.get("client-certificate-data") else None),
            client_key_pem=(_b64(user["client-key-data"])
                            if user.get("client-key-data") else None),
            workdir=workdir)

    def close(self) -> None:
        for path in self._tmp:
            try:
                path.unlink()
            except OSError:
                pass
        self._tmp.clear()

    # -- transport -------------------------------------------------------
    def api(self, method: str, path: str, body: dict | None = None,
            timeout: int = 60, raw: bool = False):
        """The single seam. Every call goes through here, which is also what lets
        the offline suite stub the whole cluster with one assignment."""
        url = f"{self.api_server}{path}"
        headers = {"Accept": "application/json", "User-Agent": "envbuild/0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:  # noqa: S310
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise InfraError(self._status_message(method, path, exc)) from exc
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise InfraError(
                f"cannot reach the Kubernetes API at {self.api_server}: {exc}") from exc
        if raw:
            return payload.decode("utf-8", "replace")
        try:
            return json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _status_message(method: str, path: str, exc: urllib.error.HTTPError) -> str:
        """Unpack the k8s `Status` object. `reason: Forbidden` plus its message
        names the missing RBAC rule; the raw body does not."""
        detail = ""
        try:
            status = json.loads(exc.read().decode("utf-8", "replace"))
            reason = status.get("reason") or ""
            message = status.get("message") or ""
            detail = f" {reason}: {message}".rstrip(": ")
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        return f"{method} {path} -> HTTP {exc.code}{detail}"

    # -- the five operations ---------------------------------------------
    def _pods(self, name: str = "", sub: str = "") -> str:
        base = f"/api/v1/namespaces/{self.namespace}/pods"
        if name:
            base += f"/{name}"
        return base + (f"/{sub}" if sub else "")

    def can_i(self, verb: str, resource: str) -> bool:
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"namespace": self.namespace,
                                                "verb": verb, "resource": resource}}}
        out = self.api("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", body)
        return bool((out.get("status") or {}).get("allowed"))

    def create_pod(self, manifest: dict) -> dict:
        return self.api("POST", self._pods(), manifest)

    def get_pod(self, name: str) -> dict:
        return self.api("GET", self._pods(name))

    def delete_pod(self, name: str, grace_seconds: int = 0) -> None:
        try:
            self.api("DELETE", self._pods(name) + f"?gracePeriodSeconds={grace_seconds}")
        except InfraError:
            pass                    # already gone, or the namespace is being torn down

    def pod_log(self, name: str, container: str, tail_bytes: int = 200_000) -> str:
        try:
            text = self.api("GET", self._pods(name, "log") + f"?container={container}",
                            raw=True)
        except InfraError:
            return ""               # a pod that never started has no log; not an error
        return text[-tail_bytes:]

    # -- waiting ---------------------------------------------------------
    def wait_terminated(self, name: str, container: str, deadline: float,
                        poll_s: float = 2.0) -> tuple[int | None, str]:
        """Block until `container` terminates. Returns (exit_code, note).

        exit_code is None when the pod never got far enough to produce one, and
        `note` then says why -- a deadline, a pull failure, a scheduling refusal.
        The kubelet enforces the real timeout via `activeDeadlineSeconds`; this
        loop just watches, with its own slightly later deadline as a backstop for
        an API server that stops answering.
        """
        while time.monotonic() < deadline:
            pod = self.get_pod(name)
            status = pod.get("status") or {}
            phase = status.get("phase") or ""

            for cs in status.get("containerStatuses") or []:
                if cs.get("name") != container:
                    continue
                state = cs.get("state") or {}
                if "terminated" in state:
                    return int(state["terminated"].get("exitCode") or 0), ""
                waiting = state.get("waiting") or {}
                if waiting.get("reason") in _FATAL_WAITING:
                    return None, (f"{waiting['reason']}: "
                                  f"{waiting.get('message', '')}".strip())

            if phase == "Failed":
                return None, (f"{status.get('reason') or 'Failed'}: "
                              f"{status.get('message', '')}".strip())
            time.sleep(poll_s)

        return None, f"ENVBUILD_TIMEOUT: pod {name} did not terminate before the deadline"


def env_overrides() -> dict:
    """The two env vars the explicit-credential path reads. Kept here so the
    driver's config table and this module cannot drift apart."""
    return {"token": os.environ.get("ENVBUILD_K8S_TOKEN") or None,
            "api_server": os.environ.get("ENVBUILD_K8S_API_SERVER") or None}
