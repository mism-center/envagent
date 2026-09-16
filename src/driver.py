#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""envbuild CLI -- the loop, the budgets, and the teardown.

The agent drives this: it calls `init`, authors an EnvSpec, then alternates
`attempt` and `patch` until a verdict. Everything deterministic lives here;
the model only does the two things a model is actually needed for -- synthesising
the first spec and picking a repair when the rule table has nothing to say.

The loop invariants this enforces (the difference between a loop that converges
and one that thrashes):

  * retain best-so-far -- a repair that lowers the rung rolls back
  * forbid repeated (failure_class, action, arg) triples -- per job, dies with it
  * one action per attempt -- a second `patch` before an `attempt` is refused
  * every exit path writes a verdict -- a job that ends without one is the single
    failure mode that corrupts the dataset
  * teardown in `finally` -- containers, volumes, images, build cache

Usage:
  envbuild init     --repo DIR [--annotation PATH] [--model-id ID]
  envbuild spec     --job-id ID --set-spec FILE
  envbuild attempt  --job-id ID
  envbuild patch    --job-id ID --action ACTION [--arg ARG] [--why TEXT]
  envbuild reverify --job-id ID
  envbuild verdict  --job-id ID --status STATUS [--reason TEXT]
  envbuild inspect  --job-id ID
  envbuild render   (--job-id ID | --spec FILE)
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import difflib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseselect                                    # noqa: E402
import classify as classify_mod                      # noqa: E402
import evidence as evidence_mod                      # noqa: E402
import ladder as ladder_mod                          # noqa: E402
import normalize                                     # noqa: E402
import patch as patch_mod                            # noqa: E402
import record                                        # noqa: E402
from builder import K8sBuilder                      # noqa: E402
from errors import InfraError                        # noqa: E402
from envspec import EnvSpec, MountContract, render   # noqa: E402
import k8s                                           # noqa: E402
from verifier import K8sVerifier                     # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- config
def load_config(path: str | None) -> configparser.ConfigParser:
    """config.ini next to the skill, with env-var overrides for the endpoints
    that differ between the compose stack and the cluster."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "builder": {"registry_push": "docker.io/mismplatform",
                    "insecure_registry": "false",
                    "flat_registry": "true",
                    "namespace": "",
                    "kaniko_image": "gcr.io/kaniko-project/executor:v1.23.2",
                    "docker_config_secret": "envbuild-registry-auth",
                    "kubeconfig": "",
                    "token": "",
                    "api_server": "",
                    "work_pvc": "envbuild-work",
                    "work_mount": "/work",
                    "models_pvc": "irods-pvc",
                    "models_mount": "/models"},
        "verifier": {"memory": ""},
        "budgets": {"max_attempts": "5", "wall_clock_s": "1200",
                    "build_timeout_s": "600", "verify_timeout_s": "180",
                    "max_image_bytes": str(8 * 1024 ** 3),
                    "max_context_bytes": str(256 * 1024 ** 2)},
        "paths": {"outputs": str(SKILL_DIR / "outputs")},
    })
    cfg.read([str(SKILL_DIR / "config.ini")] + ([path] if path else []))
    for env, (sec, key) in {
        "ENVBUILD_REGISTRY_PUSH": ("builder", "registry_push"),
        "ENVBUILD_REGISTRY_INSECURE": ("builder", "insecure_registry"),
        "ENVBUILD_REGISTRY_FLAT": ("builder", "flat_registry"),
        "ENVBUILD_KANIKO_NAMESPACE": ("builder", "namespace"),
        "ENVBUILD_KANIKO_KUBECONFIG": ("builder", "kubeconfig"),
        # Deliberately membership, not truthiness: an empty value is a real
        # setting here -- it is how an in-cluster run says "use the
        # ServiceAccount", overriding whatever config.ini carries.
        "ENVBUILD_K8S_TOKEN": ("builder", "token"),
        "ENVBUILD_K8S_API_SERVER": ("builder", "api_server"),
        "ENVBUILD_WORK_PVC": ("builder", "work_pvc"),
        "ENVBUILD_WORK_MOUNT": ("builder", "work_mount"),
        "ENVBUILD_MODELS_PVC": ("builder", "models_pvc"),
        "ENVBUILD_MODELS_MOUNT": ("builder", "models_mount"),
        "ENVBUILD_OUTPUTS": ("paths", "outputs"),
    }.items():
        if env in os.environ:
            cfg.set(sec, key, os.environ[env])
    return cfg


# ---------------------------------------------------------------- state
def job_dir(cfg, job_id: str) -> Path:
    return Path(cfg.get("paths", "outputs")) / "jobs" / job_id


def load_state(cfg, job_id: str) -> dict:
    p = job_dir(cfg, job_id) / "state.json"
    if not p.exists():
        die(f"no such job: {job_id} (expected {p})")
    return json.loads(p.read_text())


def save_state(cfg, state: dict) -> None:
    d = job_dir(cfg, state["job_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True))


def die(msg: str, code: int = 2, **extra):
    emit({"ok": False, "error": msg, **extra})
    sys.exit(code)


def emit(obj: dict) -> None:
    """Single machine-readable line-set on stdout: the agent reads this."""
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def git_rev(repo: str) -> str:
    """The other half of the approval unit. A code change invalidates
    verification but not the build, which is what makes reverify cheap."""
    try:
        # -c safe.directory=* : a host bind-mount is owned by another uid inside
        # the container, and git refuses such a repo by default. Read-only command.
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", repo,
                              "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30, check=False)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def preflight(cfg, state) -> dict:
    """Prove the substrate works before spending a build on finding out.

    A missing RBAC rule or an unreachable API server is a deployment fault.
    Discovering it *after* a ten-minute build costs an attempt of the budget and,
    worse, writes a row saying the model failed for reasons nobody can
    reconstruct.
    """
    checks = {}
    checks["pods"] = make_builder(cfg, state).check_builder()
    checks["logs"] = make_verifier(cfg, state).check_cluster()
    return checks


def _die_infra(cfg, state, exc: Exception) -> None:
    """Infra fault: close the job, tear down, and write NO attempt row.

    The verdict still gets written -- every exit path writes one -- but with
    status "error", so it is filterable out of the corpus instead of masquerading
    as a model failure.
    """
    try:
        if not state.get("closed"):
            _write_verdict(cfg, state, "error", f"infrastructure unavailable: {exc}")
    finally:
        try:
            make_verifier(cfg, state).cleanup(state["job_id"])
            make_builder(cfg, state).cleanup(state["job_id"])
        except InfraError:
            pass                       # cannot clean up through a dead daemon
    die(str(exc), code=4, job_id=state["job_id"], verdict="error",
        attempt_recorded=False,
        note="No attempt row written: this is a deployment fault, not a model failure.")


def builder_id(cfg) -> str:
    """What actually built the image, for the attempt record. The pinned Kaniko
    tag: two corpus passes built by different executor versions stay
    distinguishable instead of silently merging into one."""
    return "kaniko " + cfg.get("builder", "kaniko_image").rsplit(":", 1)[-1]


def make_client(cfg, state):
    """One Kubernetes credential, resolved the same way for both seams.

    Namespace left empty in config means "whatever namespace this pod runs in",
    which is the right default once the agent is itself a pod -- it cannot be
    wrong, and it needs no configuration.
    """
    return k8s.Client.resolve(
        namespace=cfg.get("builder", "namespace") or None,
        token=cfg.get("builder", "token") or None,
        api_server=cfg.get("builder", "api_server") or None,
        kubeconfig=cfg.get("builder", "kubeconfig") or None,
        workdir=job_dir(cfg, state["job_id"]),
    )


def make_builder(cfg, state) -> K8sBuilder:
    """The one `Builder`. See builder.py's module docstring for why Kaniko, and
    why the context travels on a volume rather than through the API."""
    b = K8sBuilder(
        job_id=state["job_id"],
        client=make_client(cfg, state),
        registry=cfg.get("builder", "registry_push"),
        work_pvc=cfg.get("builder", "work_pvc"),
        work_mount=cfg.get("builder", "work_mount"),
        models_pvc=cfg.get("builder", "models_pvc"),
        models_mount=cfg.get("builder", "models_mount"),
        kaniko_image=cfg.get("builder", "kaniko_image"),
        docker_config_secret=cfg.get("builder", "docker_config_secret"),
        insecure_registry=cfg.getboolean("builder", "insecure_registry"),
        flat_registry=cfg.getboolean("builder", "flat_registry"),
    )
    b.attempt = state["attempt"]          # keep image tags aligned with attempts
    return b


def make_verifier(cfg, state) -> K8sVerifier:
    return K8sVerifier(
        job_id=state["job_id"],
        client=make_client(cfg, state),
        work_pvc=cfg.get("builder", "work_pvc"),
        work_mount=cfg.get("builder", "work_mount"),
        models_pvc=cfg.get("builder", "models_pvc"),
        models_mount=cfg.get("builder", "models_mount"),
        code_ref=state["repo"],
        docker_config_secret=cfg.get("builder", "docker_config_secret"),
        memory=cfg.get("verifier", "memory") or None,
    )


# ---------------------------------------------------------------- init
def draft_spec(ev: dict, ann: dict, choice, digest: str, builder_version: str) -> EnvSpec:
    """A first spec from evidence alone. The agent refines it per specs/synthesis.md.

    Declared deps win over scanned ones when the annotation has them: the
    annotation was human-reviewed, requirements.txt was not.
    """
    deps = list(ann.get("declared_deps") or []) or list(ev.get("python", {}).get("declared_deps") or [])
    if choice.pkg_manager in ("renv", "pkg"):
        deps = list(ann.get("declared_deps") or []) or list((ev.get("r") or {}).get("deps") or [])
    if choice.pkg_manager == "mamba":
        deps = list((ev.get("conda") or {}).get("deps") or deps)
    apt = sorted(set(list(ann.get("system_deps") or []) + list(ev["system_hints"]["apt"])))
    # A local module living directly at repo root or under src/ needs that
    # directory on the import path -- the entry point's own file is often
    # nested a few levels below it, so relying on "the entry file's own
    # directory" (ladder.py's L2 probe) is not enough. Evidence already knows
    # which of the two it is (evidence._local_modules); seed it here instead of
    # waiting for the L2 failure + a repair attempt to rediscover the same fact
    # per repo.
    mount = MountContract()
    local_dirs = sorted(set((ev.get("local_module_paths") or {}).values()))
    if local_dirs:
        mount = MountContract(**{
            **mount.to_dict(),
            "extra_path": tuple(
                mount.code_path if not d else f"{mount.code_path}/{d}" for d in local_dirs
            ),
        })
    return EnvSpec(
        base_image=choice.base_image,
        base_digest=digest,
        pkg_manager=choice.pkg_manager,
        install_mode=choice.install_mode,
        apt_packages=apt,
        pkg_specs=deps,
        env_vars={},
        mount=mount,
        entrypoint=[],
        builder_version=builder_version,
    )


def cmd_init(args, cfg) -> None:
    repo = str(Path(args.repo).resolve())
    if not Path(repo).is_dir():
        die(f"--repo is not a directory: {repo}")
    job_id = args.job_id or f"job-{dt.datetime.now():%Y-%m-%d}-{uuid.uuid4().hex[:4]}"
    max_ctx = cfg.getint("budgets", "max_context_bytes")

    # biomodel-annotator writes `metadata-package/` INSIDE the model's own
    # directory, so when no --annotation is given, look there before giving up.
    # Without an annotation there is no entry point, which caps the whole job at
    # L1 -- too expensive a consequence for a forgotten flag.
    ann_path, ann_auto = args.annotation, False
    if not ann_path and (Path(repo) / "metadata-package").is_dir():
        ann_path, ann_auto = str(Path(repo) / "metadata-package"), True
    ev = evidence_mod.scan(repo, max_bytes=max_ctx)
    ann = evidence_mod.read_annotation(ann_path) if ann_path else {}
    choice = baseselect.select(ev, ann)
    try:
        digest = baseselect.resolve_digest(choice.base_image)
    except RuntimeError as exc:
        die(str(exc), code=4)

    spec = draft_spec(ev, ann, choice, digest, builder_id(cfg))
    entry = ladder_mod.entry_from_command(
        (ann.get("entry_points") or [{}])[0].get("command") or "")

    state = {
        "job_id": job_id,
        "model_id": args.model_id or f"local:{Path(repo).name}",
        "repo": repo,
        "annotation_path": ann_path,
        "annotation": ann,
        "code_revision": git_rev(repo),
        "started_at": time.time(),
        "attempt": 0,
        "spec": spec.to_dict(),
        "entry": entry,
        "command": (ann.get("entry_points") or [{}])[0].get("command") or "",
        # Only assert "wrote files" when the model record says where it writes.
        "expect_outputs": bool((ann.get("entry_points") or [{}])[0]
                              .get("default_output_location")),
        "best": {"rung": None, "spec": None, "image_ref": None, "image_digest": None},
        "forbidden": [],
        "patched_since_attempt": True,       # the draft counts as the first spec
        "closed": False,
        "local_modules": ev.get("local_modules") or [],
    }
    d = job_dir(cfg, job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence.json").write_text(json.dumps(ev, indent=2))
    save_state(cfg, state)

    emit({
        "ok": True, "job_id": job_id, "model_id": state["model_id"],
        "code_revision": state["code_revision"],
        "evidence_file": str(d / "evidence.json"),
        "annotation_path": ann_path,
        "annotation_autodetected": ann_auto,
        "evidence_summary": {
            "languages": ev["languages"], "markers": sorted(ev["markers"]),
            "declared_deps": (ev.get("python", {}).get("declared_deps") or [])[:40],
            "system_hints": ev["system_hints"]["apt"],
            "local_modules": ev["local_modules"],
            "entrypoint_candidates": ev["entrypoint_candidates"],
            "ci_files": [c["file"] for c in ev["ci"]],
            "compiled": ev["compiled"], "oversize": ev["oversize"],
        },
        "base_choice": {"base_image": choice.base_image, "pkg_manager": choice.pkg_manager,
                        "install_mode": choice.install_mode, "reason": choice.reason,
                        "base_digest": digest},
        "annotation": ann,
        "entry": entry,
        "draft_spec": spec.to_dict(),
        "warning": (None if ann.get("entry_points") else
                    "No entry point: this job cannot pass L2. Supply --annotation "
                    "(a metadata-package/ dir or YAML), or expect ENTRYPOINT_UNKNOWN."),
        "next": ("Review the draft against specs/synthesis.md. Write the final spec "
                 f"with `envbuild spec --job-id {job_id} --set-spec FILE`, "
                 "then `envbuild attempt`."),
    })


def cmd_spec(args, cfg) -> None:
    state = load_state(cfg, args.job_id)
    data = json.loads(Path(args.set_spec).read_text(encoding="utf-8"))
    spec = EnvSpec.from_dict(data)
    if not spec.base_digest:
        try:
            spec.base_digest = baseselect.resolve_digest(spec.base_image)
        except RuntimeError as exc:
            die(str(exc), code=4)
    spec.builder_version = builder_id(cfg)
    old = render(EnvSpec.from_dict(state["spec"])).text
    state["spec"] = spec.to_dict()
    state["patched_since_attempt"] = True
    save_state(cfg, state)
    emit({"ok": True, "job_id": state["job_id"], "envspec_hash": spec.hash(),
          "diff": _diff(old, render(spec).text), "dockerfile": render(spec).text})


# ---------------------------------------------------------------- attempt
def _diff(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        "before", "after"))


def _budget_check(cfg, state) -> str | None:
    if state.get("closed"):
        return "job already closed; start a new one"
    if state["attempt"] >= cfg.getint("budgets", "max_attempts"):
        return f"attempt budget exhausted ({state['attempt']} attempts)"
    elapsed = time.time() - state["started_at"]
    if elapsed > cfg.getfloat("budgets", "wall_clock_s"):
        return f"wall-clock budget exhausted ({elapsed:.0f}s)"
    return None


def _finish_budget(cfg, state, reason: str) -> None:
    """Budget exhaustion is an exit path, so it writes a verdict like any other."""
    _write_verdict(cfg, state, "budget_exhausted", reason)
    die(reason, code=5, job_id=state["job_id"], verdict="budget_exhausted")


def cmd_attempt(args, cfg) -> None:
    state = load_state(cfg, args.job_id)
    blocked = _budget_check(cfg, state)
    if blocked:
        _finish_budget(cfg, state, blocked)

    try:
        preflight(cfg, state)
    except InfraError as exc:
        _die_infra(cfg, state, exc)

    spec = EnvSpec.from_dict(state["spec"])
    state["attempt"] += 1
    attempt_no = state["attempt"]
    started = time.time()
    builder, verifier = make_builder(cfg, state), make_verifier(cfg, state)

    # ---- L0 -----------------------------------------------------------
    # The build context is a path, not a tar: the build pod mounts the same
    # models claim this process reads, so there is nothing to ship.
    try:
        build = builder.build(spec, state["repo"], cfg.getint("budgets", "build_timeout_s"))
    except InfraError as exc:
        state["attempt"] -= 1                 # the attempt never happened
        _die_infra(cfg, state, exc)
    (job_dir(cfg, state["job_id"]) / f"Dockerfile.a{attempt_no}").write_text(build.dockerfile)

    lad = ladder_mod.LadderResult(reached="L0" if build.ok else "L0")
    rung_failed, stderr, exit_code = "L0", build.stderr, None
    outputs: list[str] = []

    if build.ok:
        # ---- L1-L3 ------------------------------------------------------
        try:
            # Read off the registry manifest, so the size ceiling is checked
            # against a real number before anything executes.
            build.image_bytes = verifier.image_size(build.image_ref)
            if build.image_bytes and build.image_bytes > cfg.getint("budgets", "max_image_bytes"):
                _finish_budget(cfg, state,
                               f"image size {build.image_bytes} exceeds max_image_bytes")
            lad = ladder_mod.climb(verifier, build.image_ref, verifier.code_ref, spec,
                                   state["entry"], state["command"],
                                   cfg.getint("budgets", "verify_timeout_s"),
                                   start=args.start,
                                   expect_outputs=bool(state.get("expect_outputs")))
        except InfraError as exc:
            # This is what turned a working L0 into an `UNKNOWN` corpus row: the
            # substrate was unreachable, which says nothing about the model.
            state["attempt"] -= 1
            _die_infra(cfg, state, exc)
        except (RuntimeError, ValueError) as exc:
            lad = ladder_mod.LadderResult(reached="L0", failed_at="L1",
                                          stderr=f"verifier error: {exc}", exit_code=1)
        rung_failed = lad.failed_at or lad.reached
        stderr, exit_code, outputs = lad.stderr, lad.exit_code, lad.outputs

    reached = lad.reached if build.ok else "L0"
    ok = build.ok and lad.failed_at is None

    # ---- classify ------------------------------------------------------
    cls = None
    if not ok:
        cls = classify_mod.classify(stderr, rung=rung_failed,
                                    local_modules=state.get("local_modules") or (),
                                    exit_code=exit_code)

    duration = time.time() - started
    row = {
        "model_id": state["model_id"], "job_id": state["job_id"], "attempt": attempt_no,
        "install_mode": spec.install_mode, "base_image": spec.base_image,
        "base_digest": spec.base_digest, "builder_version": spec.builder_version,
        "envspec_hash": spec.hash(),
        "ladder_reached": reached,
        "failed_step_index": build.failed_step_index,
        "failed_step_kind": build.failed_step_kind,
        "failed_step_field": build.failed_step_field,
        "error_signature": normalize.signature(stderr) if not ok else None,
        "error_raw": (stderr or "")[-8000:] if not ok else "",
        "failure_class": cls.failure_class if cls else None,
        "classified_by": cls.classified_by if cls else None,
        "action_taken": None,                    # filled by the next `patch`
        "next_ladder": "L0" if not ok else "L4",
        "duration_s": round(duration, 2),
        "image_bytes": build.image_bytes,
        "cache_hits": build.cache_hits,
        "vertices_total": build.vertices_total,
        "tokens": 0,                             # the agent does the LLM work, not this process
        "code_revision": state["code_revision"],
        "image_digest": build.image_digest,
        "outputs_written": outputs,
        "failed_rung": None if ok else rung_failed,
    }
    record.append_attempt(cfg.get("paths", "outputs"), row)

    # ---- best-so-far ----------------------------------------------------
    best = state["best"]
    if ladder_mod.rung_index(reached) > ladder_mod.rung_index(best.get("rung") or ""):
        state["best"] = {"rung": reached, "spec": spec.to_dict(),
                         "image_ref": build.image_ref, "image_digest": build.image_digest}
    state["last"] = {"attempt": attempt_no, "reached": reached, "ok": ok,
                     "failure_class": cls.failure_class if cls else None,
                     "failed_rung": None if ok else rung_failed,
                     "image_ref": build.image_ref, "image_digest": build.image_digest}
    state["patched_since_attempt"] = False
    save_state(cfg, state)

    out = {
        "ok": ok, "job_id": state["job_id"], "attempt": attempt_no,
        "ladder_reached": reached, "failed_rung": row["failed_rung"],
        "failed_step": {"index": build.failed_step_index, "kind": build.failed_step_kind,
                        "field": build.failed_step_field},
        "image_ref": build.image_ref, "image_digest": build.image_digest,
        "image_bytes": build.image_bytes, "cache_hits": build.cache_hits,
        "vertices_total": build.vertices_total,
        "duration_s": row["duration_s"], "outputs_written": outputs,
        "error_signature": row["error_signature"],
        "stderr_tail": (stderr or "")[-4000:],
        "attempts_left": cfg.getint("budgets", "max_attempts") - attempt_no,
        "seconds_left": round(cfg.getfloat("budgets", "wall_clock_s")
                              - (time.time() - state["started_at"])),
    }
    if ok:
        out["next"] = ("Verified through L3. Close the job with "
                       f"`envbuild verdict --job-id {state['job_id']} --status verified`.")
    else:
        out["classification"] = cls.to_dict()
        out["forbidden"] = state["forbidden"]
        if cls.routes_to == "retry" and cls.action:
            out["suggested_action"] = {"type": cls.action, "arg": cls.arg}
            out["next"] = ("Rule matched. Apply it with `envbuild patch --job-id "
                           f"{state['job_id']} --action {cls.action}"
                           + (f" --arg '{cls.arg}'" if cls.arg else "") + "`.")
        elif cls.routes_to == "retry":
            out["next"] = ("Rule matched the class but not an argument. Read "
                           "specs/repair.md and choose one typed action.")
        elif cls.routes_to == "llm":
            out["next"] = ("No rule matched -- classify per specs/failure_taxonomy.md and "
                           "repair per specs/repair.md, then `envbuild patch ... "
                           "--classified-by llm --failure-class CLASS`. "
                           "Every LLM fallback is a candidate rule for classify.py.")
        else:
            out["next"] = (f"Class {cls.failure_class} routes to '{cls.routes_to}'. "
                           f"Close with `envbuild verdict --job-id {state['job_id']} "
                           f"--status {'escalated' if cls.routes_to == 'dead-letter' else 'failed'}"
                           f" --reason ...`.")
    emit(out)
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- patch
def cmd_patch(args, cfg) -> None:
    state = load_state(cfg, args.job_id)
    if state.get("closed"):
        die("job already closed")
    if state.get("patched_since_attempt"):
        # One action per attempt. A second patch is a discarded action, logged.
        die("spec already patched since the last attempt -- one action per attempt. "
            "Run `envbuild attempt` first.", code=3,
            discarded={"action": args.action, "arg": args.arg})

    last = state.get("last") or {}
    failure_class = args.failure_class or last.get("failure_class") or "UNKNOWN"
    triple = [failure_class, args.action, args.arg or ""]
    if triple in state["forbidden"]:
        die(f"already tried {triple} in this job -- pick a different action", code=3,
            forbidden=state["forbidden"])

    spec = EnvSpec.from_dict(state["spec"])
    rollback = None
    best = state.get("best") or {}
    # Retain best-so-far: a repair that lowered the rung is discarded, and the
    # action that lowered it is forbidden, so the loop cannot re-walk it.
    if best.get("spec") and ladder_mod.rung_index(last.get("reached") or "") \
            < ladder_mod.rung_index(best.get("rung") or ""):
        spec = EnvSpec.from_dict(best["spec"])
        rollback = f"regressed to {last.get('reached')}; rolled back to best {best['rung']}"

    try:
        result = patch_mod.apply(spec, args.action, args.arg or "")
    except patch_mod.PatchError as exc:
        die(str(exc), code=3)

    if result.needs_digest:
        try:
            result.spec.base_digest = baseselect.resolve_digest(result.spec.base_image)
        except RuntimeError as exc:
            die(str(exc), code=4)

    old_df = render(EnvSpec.from_dict(state["spec"])).text
    new_df = render(result.spec).text
    state["spec"] = result.spec.to_dict()
    state["forbidden"].append(triple)
    state["patched_since_attempt"] = True
    save_state(cfg, state)

    # Backfill the action onto the attempt row it responds to: the record is
    # only useful if the failure and the repair sit in the same row.
    _backfill_action(cfg, state, {"type": args.action, "arg": args.arg or None,
                                  "why": args.why or "", "classified_by": args.classified_by})

    emit({"ok": True, "job_id": state["job_id"], "action": args.action, "arg": args.arg,
          "note": result.note, "rollback": rollback,
          "envspec_hash": result.spec.hash(), "diff": _diff(old_df, new_df),
          "forbidden": state["forbidden"],
          "next": f"`envbuild attempt --job-id {state['job_id']}`"})


def _backfill_action(cfg, state, action: dict) -> None:
    """Rewrite the last attempt row so `action_taken` is not perpetually null.

    ponytail: read-modify-write of a small JSONL file. Fine at Phase 0 volume;
    if the corpus run makes attempts.jsonl large, write actions to a sidecar
    and join at analysis time instead.
    """
    path = Path(cfg.get("paths", "outputs")) / "attempts.jsonl"
    rows = record.read_jsonl(path)
    for row in reversed(rows):
        if row["job_id"] == state["job_id"] and row["attempt"] == state["attempt"]:
            row["action_taken"] = action
            if action.get("classified_by"):
                row["classified_by"] = action["classified_by"]
            break
    else:
        return
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


# ---------------------------------------------------------------- reverify
def cmd_reverify(args, cfg) -> None:
    """The cheap path: only the code changed, so replay L2-L3 against the image
    that already verified. No rebuild, no L0, no L1."""
    state = load_state(cfg, args.job_id)
    ref = args.image_ref or (state.get("best") or {}).get("image_ref")
    if not ref:
        die("no image to re-verify against; run `attempt` first")
    spec = EnvSpec.from_dict((state["best"] or {}).get("spec") or state["spec"])
    verifier = make_verifier(cfg, state)
    started = time.time()
    try:
        lad = ladder_mod.climb(verifier, ref, verifier.code_ref, spec, state["entry"],
                               state["command"], cfg.getint("budgets", "verify_timeout_s"),
                               start=args.start,
                               expect_outputs=bool(state.get("expect_outputs")))
    except InfraError as exc:
        _die_infra(cfg, state, exc)
    except (RuntimeError, ValueError) as exc:
        die(f"reverify failed: {exc}", code=4)
    state["code_revision"] = git_rev(state["repo"])
    # A successful reverify is the highest rung this job has reached. Without
    # this the verdict reports the pre-reverify rung and understates the result.
    if ladder_mod.rung_index(lad.reached) > ladder_mod.rung_index(
            (state.get("best") or {}).get("rung") or ""):
        state.setdefault("best", {})
        state["best"].update({"rung": lad.reached, "image_ref": ref})
        state["best"].setdefault("spec", spec.to_dict())
    state["last"] = {"attempt": state["attempt"], "reached": lad.reached,
                     "ok": lad.failed_at is None, "failure_class": None,
                     "failed_rung": lad.failed_at, "image_ref": ref,
                     "image_digest": (state.get("best") or {}).get("image_digest")}
    save_state(cfg, state)
    emit({"ok": lad.failed_at is None, "job_id": state["job_id"],
          "image_ref": ref, "code_revision": state["code_revision"],
          "ladder_reached": lad.reached, "failed_rung": lad.failed_at,
          "duration_s": round(time.time() - started, 2),
          "stderr_tail": (lad.stderr or "")[-4000:], "outputs_written": lad.outputs})
    sys.exit(0 if lad.failed_at is None else 1)


# ---------------------------------------------------------------- verdict
def _write_verdict(cfg, state, status: str, reason: str) -> dict:
    best = state.get("best") or {}
    last = state.get("last") or {}
    row = {
        "model_id": state["model_id"], "job_id": state["job_id"], "status": status,
        "ladder_reached": best.get("rung") or "L0",
        "attempts": state["attempt"],
        # The approval unit is the pair (image digest, code revision): a code
        # change invalidates verification but not the build.
        "image_digest": best.get("image_digest"),
        "image_ref": best.get("image_ref"),
        "code_revision": state.get("code_revision"),
        "envspec_hash": EnvSpec.from_dict(best.get("spec") or state["spec"]).hash(),
        "failure_class": last.get("failure_class"),
        "routes_to": classify_mod.ROUTING.get(last.get("failure_class") or "", "retry"),
        "duration_s": round(time.time() - state["started_at"], 2),
        "reason": reason,
        "l4": None,                              # reference-trace comparison: Phase 0 never runs it
        "forbidden": state.get("forbidden"),
    }
    written = record.write_verdict(cfg.get("paths", "outputs"), row)
    state["closed"] = True
    save_state(cfg, state)
    return written


def cmd_verdict(args, cfg) -> None:
    state = load_state(cfg, args.job_id)
    already = state.get("closed")
    try:
        row = _write_verdict(cfg, state, args.status, args.reason or "") if not already else None
    finally:
        # Teardown always runs. On a laptop, skipping this fills the disk in an
        # afternoon; in a cluster it leaks PVCs.
        if not args.keep:
            make_verifier(cfg, state).cleanup(state["job_id"])
            make_builder(cfg, state).cleanup(state["job_id"])
    emit({"ok": True, "job_id": state["job_id"], "already_closed": bool(already),
          "verdict": row, "torn_down": not args.keep})


def cmd_inspect(args, cfg) -> None:
    state = load_state(cfg, args.job_id)
    rows = [r for r in record.read_jsonl(Path(cfg.get("paths", "outputs")) / "attempts.jsonl")
            if r["job_id"] == args.job_id]
    emit({"ok": True, "job_id": args.job_id, "state": {k: v for k, v in state.items()
                                                       if k not in ("annotation",)},
          "attempts": [{k: r.get(k) for k in ("attempt", "ladder_reached", "failure_class",
                                              "classified_by", "failed_step_kind",
                                              "action_taken", "duration_s")} for r in rows]})


def cmd_render(args, cfg) -> None:
    if args.spec:
        spec = EnvSpec.from_dict(json.loads(Path(args.spec).read_text(encoding="utf-8")))
    else:
        spec = EnvSpec.from_dict(load_state(cfg, args.job_id)["spec"])
    print(render(spec).text)


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="envbuild", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="extra config.ini to layer on top of the defaults")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="scan a repo, pick a base, draft an EnvSpec")
    s.add_argument("--repo", required=True)
    s.add_argument("--annotation", help="annotation YAML or a metadata-package/ dir")
    s.add_argument("--model-id")
    s.add_argument("--job-id")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("spec", help="replace the job's EnvSpec with an agent-authored one")
    s.add_argument("--job-id", required=True)
    s.add_argument("--set-spec", required=True, help="path to an EnvSpec JSON file")
    s.set_defaults(fn=cmd_spec)

    s = sub.add_parser("attempt", help="build, push and climb the ladder once")
    s.add_argument("--job-id", required=True)
    s.add_argument("--start", default="L1", choices=("L1", "L2"))
    s.set_defaults(fn=cmd_attempt)

    s = sub.add_parser("patch", help="apply exactly one typed remediation action")
    s.add_argument("--job-id", required=True)
    s.add_argument("--action", required=True, choices=patch_mod.ACTIONS)
    s.add_argument("--arg", default="")
    s.add_argument("--why", default="", help="one line of justification, recorded")
    s.add_argument("--failure-class", help="required when you classified by LLM")
    s.add_argument("--classified-by", choices=("rule", "llm"), default="rule")
    s.set_defaults(fn=cmd_patch)

    s = sub.add_parser("reverify", help="replay the ladder against an existing image")
    s.add_argument("--job-id", required=True)
    s.add_argument("--image-ref")
    # L2 is the code-changed path. L1 re-walks the image checks too, which is what
    # you want after an infra fault killed verification on an image that did build.
    s.add_argument("--start", default="L2", choices=("L1", "L2"))
    s.set_defaults(fn=cmd_reverify)

    s = sub.add_parser("verdict", help="close the job (always) and tear down")
    s.add_argument("--job-id", required=True)
    s.add_argument("--status", required=True, choices=record.VALID_STATUS)
    s.add_argument("--reason", default="")
    s.add_argument("--keep", action="store_true", help="skip teardown (debugging only)")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("inspect", help="show job state and its attempt rows")
    s.add_argument("--job-id", required=True)
    s.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("render", help="print the Dockerfile for a spec (no Docker needed)")
    s.add_argument("--job-id")
    s.add_argument("--spec")
    s.set_defaults(fn=cmd_render)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
