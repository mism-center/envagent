"""Registry reads over plain HTTPS: tag -> digest, and digest -> byte size.

This exists because nothing in the cluster has a docker daemon. `resolve_digest`
used to shell out to `docker buildx imagetools inspect` and `image_size` to
`docker image inspect`; both are gone with the socket, and both questions are
answerable with two GETs against the registry's v2 API.

Read-only on purpose. Pushing is Kaniko's job, from inside the build pod, using
credentials it reads from its own Secret. Nothing here ever writes.

Auth follows the same two steps docker does: hit the endpoint anonymously, and if
it answers 401 with a `Www-Authenticate: Bearer ...` challenge, fetch a token from
the realm it names -- with Basic credentials from the docker config file when we
have them for that host, anonymously when we do not. That single path covers
Docker Hub, GHCR, ECR and a plain registry:2.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Manifest media types we are willing to receive. Order matters to some
# registries: list/index first, so a multi-arch tag resolves to the index digest
# -- which is what `FROM image@sha256:...` must pin, not one architecture's.
_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# Docker Hub is the one registry whose API host differs from the name people
# write. `python:3.11-slim` means `registry-1.docker.io/library/python:3.11-slim`.
_HUB_NAMES = {"docker.io", "index.docker.io", "registry-1.docker.io", ""}
_HUB_API = "registry-1.docker.io"


class RegistryError(RuntimeError):
    """The registry could not answer. Never a statement about the model."""


def parse_ref(image: str) -> tuple[str, str, str]:
    """`python:3.11-slim` -> (`registry-1.docker.io`, `library/python`, `3.11-slim`).

    Returns (api_host, repository, reference) where reference is a tag or a
    `sha256:...` digest. A host is only a host if it looks like one -- a dot, a
    colon, or the literal `localhost`. Without that rule `mismplatform/envbuild`
    parses as host `mismplatform`, which is how you spend an afternoon.
    """
    ref = image
    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = "latest"
    head, _, maybe_tag = ref.rpartition(":")
    if head and "/" not in maybe_tag:
        ref, tag = head, maybe_tag

    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host, repo = parts[0], "/".join(parts[1:])
    else:
        host, repo = "docker.io", ref

    if host in _HUB_NAMES:
        host = _HUB_API
        if "/" not in repo:                 # official images live under library/
            repo = f"library/{repo}"
    return host, repo, (digest or tag)


def _docker_config() -> dict:
    """The docker config file, if one is mounted. Absent is normal: public base
    images need no credentials, and only the pushed image does."""
    base = os.environ.get("DOCKER_CONFIG")
    path = Path(base) / "config.json" if base else Path.home() / ".docker" / "config.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _basic_for(host: str) -> str | None:
    """`Basic <b64>` for this host from the docker config, or None."""
    auths = _docker_config().get("auths") or {}
    keys = [host]
    if host == _HUB_API:
        # Docker writes Hub credentials under any of these, depending on which
        # client version logged in.
        keys += ["docker.io", "index.docker.io", "https://index.docker.io/v1/"]
    for key in keys:
        entry = auths.get(key) or {}
        if entry.get("auth"):
            return "Basic " + entry["auth"]
        if entry.get("username"):
            raw = f"{entry['username']}:{entry.get('password', '')}".encode()
            return "Basic " + base64.b64encode(raw).decode()
    return None


def _open(url: str, headers: dict, timeout: int):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)   # noqa: S310 -- https only, built above


def _bearer(challenge: str, host: str, timeout: int) -> str | None:
    """Turn a `Www-Authenticate: Bearer realm=...,service=...,scope=...` challenge
    into an `Authorization` value, using stored credentials when we have them."""
    if not challenge.lower().startswith("bearer "):
        return None
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = fields.pop("realm", "")
    if not realm:
        return None
    url = realm + ("?" + urllib.parse.urlencode(fields) if fields else "")
    headers = {}
    basic = _basic_for(host)
    if basic:
        headers["Authorization"] = basic
    try:
        with _open(url, headers, timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"could not get a registry token from {realm}: {exc}") from exc
    tok = body.get("token") or body.get("access_token")
    return f"Bearer {tok}" if tok else None


def _get(host: str, path: str, accept: str, timeout: int) -> tuple[bytes, dict]:
    """GET https://host/path, doing the 401-then-token dance once. Returns
    (body, headers)."""
    url = f"https://{host}{path}"
    headers = {"Accept": accept, "User-Agent": "envbuild/0"}
    try:
        with _open(url, headers, timeout) as resp:
            return resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as first:
        if first.code != 401:
            raise RegistryError(f"{url} -> HTTP {first.code} {first.reason}") from first
        auth = _bearer(first.headers.get("Www-Authenticate", ""), host, timeout)
        if not auth:
            raise RegistryError(
                f"{url} needs credentials and none were usable. Mount a docker "
                f"config with an entry for {host}.") from first
        headers["Authorization"] = auth
        try:
            with _open(url, headers, timeout) as resp:
                return resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as second:
            raise RegistryError(f"{url} -> HTTP {second.code} {second.reason}") from second
        except (urllib.error.URLError, OSError) as exc:
            raise RegistryError(f"{url} unreachable: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RegistryError(f"{url} unreachable: {exc}") from exc


def manifest(image: str, timeout_s: int = 120) -> tuple[str, dict]:
    """(digest, parsed manifest) for a tag or a digest reference."""
    host, repo, ref = parse_ref(image)
    body, headers = _get(host, f"/v2/{repo}/manifests/{ref}", _ACCEPT, timeout_s)
    digest = headers.get("Docker-Content-Digest") or headers.get("docker-content-digest") or ""
    if not _DIGEST_RE.fullmatch(digest):
        # Some registries omit the header on a digest reference, because the
        # answer is the thing you already asked with.
        m = _DIGEST_RE.fullmatch(ref)
        digest = m.group(0) if m else ""
    try:
        return digest, json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{image}: manifest was not JSON") from exc


def resolve_digest(image: str, timeout_s: int = 120) -> str:
    """tag -> `sha256:...`. Raises rather than returning a bare tag: an unpinned
    base is worse than a failed job."""
    digest, _ = manifest(image, timeout_s)
    if not digest:
        raise RegistryError(f"could not resolve a digest for {image}")
    return digest


def image_size(image: str, timeout_s: int = 120) -> int | None:
    """Total bytes the image occupies in the registry: config plus every layer.

    Compressed sizes, so this reads a little under `docker image inspect`, which
    reports the unpacked size. It is used for one thing -- refusing to run a
    container above `max_image_bytes` -- and for that, under-reporting is the
    direction that fails safe only if the ceiling has headroom. Set the ceiling
    with that in mind.

    Returns None rather than raising: a missing size must not fail a build that
    otherwise succeeded.
    """
    try:
        _, man = manifest(image, timeout_s)
        if man.get("mediaType") in _INDEX_TYPES or "manifests" in man:
            host, repo, _ = parse_ref(image)
            picked = next(
                (m for m in man.get("manifests", [])
                 if (m.get("platform") or {}).get("os") == "linux"
                 and (m.get("platform") or {}).get("architecture") == "amd64"),
                None)
            if picked is None:
                return None
            body, _hdrs = _get(host, f"/v2/{repo}/manifests/{picked['digest']}",
                               _ACCEPT, timeout_s)
            man = json.loads(body.decode("utf-8", "replace"))
        total = int((man.get("config") or {}).get("size") or 0)
        total += sum(int(layer.get("size") or 0) for layer in man.get("layers") or [])
        return total or None
    except (RegistryError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
