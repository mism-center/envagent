#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Score a benchmark pass against bench/ground_truth.yaml.

    uv run scripts/score.py outputs/attempts.jsonl outputs/verdicts.jsonl --run-id aks-rev4-1
    uv run scripts/score.py results/rev4/attempts.jsonl results/rev4/verdicts.jsonl --run-id aks-rev4-1 --json

Success for a trial (one verdict on a buildable model) is the definition the
project settled on: the built container ran the example the repo itself
provides. That is a `verified` verdict whose `entrypoint_used` is the ground
truth's `example` (or another discovered smoke example), with a lockfile and
without a shim in the repair chain.

Three headline numbers, computed per TRIAL (model x repeat), never per model:

  build success       verified trials / buildable trials          <- the 90 % target
  verdict correctness right verdict / all trials (controls included)
  false-verified      verified trials that fail the honesty checks -> must be 0

and a failure attribution for every non-successful buildable trial:

  builder     L0/L1 failure, or an L2/L3 failure in the image/mount plane
  annotation  the entry the agent ran differs from ground truth and it never
              got as far as running the ground-truth example
  upstream    the ground-truth example itself failed, and a human has verified
              it runs (human_verified set) -- otherwise `unattributed`
  infra       the job closed on INFRA_UNAVAILABLE
  budget      budget_exhausted

Rows without `run.run_id` (pre-rev-3 corpus) are scored when --run-id is
omitted; with --run-id only matching rows count. Verdicts on models not in the
ground truth are listed, not scored.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_SELF_INFLICTED = re.compile(r"can't open file '/(?:outputs|model)[^']*': \[Errno 2\]|"
                             r"Read-only file system: '/model|cannot open file '/(?:outputs|model)")
# An L3 that dies at import time is an environment failure, not the model's:
# a missing/wrong-version dependency, an ABI break, a missing system library.
_ENV_PLANE_L3 = re.compile(r"ImportError: |ModuleNotFoundError: |AttributeError: module '\w+' has no attribute|"
                           r"cannot open shared object file|undefined symbol")
_BUILDER_PLANE = {"MISSING_SYSTEM_LIB", "DEP_RESOLUTION_CONFLICT", "COMPILE_ERROR", "MISSING_DEPENDENCY",
                  "ABI_MISMATCH", "BUILD_MODE_MISMATCH", "BASE_IMAGE_MISMATCH", "SPEC_INVALID",
                  "IMPORT_PATH_ERROR", "MOUNT_CONTRACT_ERROR", "UNKNOWN"}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _model(model_id: str) -> str:
    return re.sub(r"^[a-z0-9]+:", "", model_id or "").split("/")[0].removeprefix("bench-")


def _norm(cmd: str | None) -> str:
    return re.sub(r"\s+", " ", (cmd or "").strip())


def honesty(verdict: dict, attempts: list[dict]) -> list[str]:
    """Why a `verified` verdict should not be trusted; empty when it is honest."""
    problems = []
    # Only rows written since the lockfile existed can be held to it.
    if "lockfile_sha256" in verdict and not verdict.get("lockfile_sha256"):
        problems.append("no lockfile")
    if not verdict.get("image_digest"):
        problems.append("no image digest")
    for a in attempts:
        act = a.get("action_taken") or {}
        if act.get("type") == "ADD_PRE_INSTALL_CMD" and re.search(
                r"site-packages|getsitepackages|/etc/hosts|resolv\.conf", str(act.get("arg") or "")):
            problems.append(f"shim via ADD_PRE_INSTALL_CMD: {str(act.get('arg'))[:60]}")
    return problems


def attribute(verdict: dict, attempts: list[dict], gt: dict) -> str:
    st = verdict.get("status")
    fc = verdict.get("failure_class")
    if st == "budget_exhausted":
        return "budget"
    if st == "error" and (fc == "INFRA_UNAVAILABLE" or "infrastructure" in (verdict.get("reason") or "").lower()
                          or re.search(r"DNS|resolve|registry|network", verdict.get("reason") or "", re.I)):
        return "infra"
    rung = verdict.get("ladder_reached") or "L0"
    if rung in ("L0", "L1"):
        return "builder"
    # An L3 that died on the mount contract -- the script vanished after a
    # workdir move, or the model hit the read-only code mount -- is the
    # builder's doing whatever command it ran. (rev-4 pass 1: three of three
    # escalations were this and were mis-attributed to the annotation.)
    l3 = [a for a in attempts if a.get("failed_rung") == "L3"]
    if l3 and (_SELF_INFLICTED.search(l3[-1].get("error_raw") or "")
               or _ENV_PLANE_L3.search(l3[-1].get("error_raw") or "")):
        return "builder"
    if fc in _BUILDER_PLANE:
        return "builder"
    if "entrypoint_used" not in verdict:
        return "unattributed"                    # pre-rev-4 row: what ran was not recorded
    ran = _norm(verdict.get("entrypoint_used"))
    truth = _norm(gt.get("example"))
    if truth and ran != truth:
        return "annotation"
    if fc == "TIMEOUT" and l3 and gt.get("timeout_s") and (l3[-1].get("l3_timeout_s") or 0) < gt["timeout_s"]:
        # The example was given less time than the ground truth says it needs:
        # the builder's budget, not the model (aks-rev4-6 spatio-flux, 600 < 1200).
        return "builder"
    if fc in ("RUNTIME_ERROR", "TIMEOUT", "MISSING_DATA_FILE", "LICENSE_REQUIRED") and ran == truth:
        return "upstream" if gt.get("human_verified") else "unattributed"
    return "builder"


def score(attempts: list[dict], verdicts: list[dict], gt: dict, run_id: str | None) -> dict:
    if run_id:
        attempts = [a for a in attempts if (a.get("run") or {}).get("run_id") == run_id]
        verdicts = [v for v in verdicts if (v.get("run") or {}).get("run_id") == run_id]
    by_job = collections.defaultdict(list)
    for a in attempts:
        by_job[a["job_id"]].append(a)
    models = gt["models"]
    trials, unknown = [], []
    for v in verdicts:
        m = _model(v["model_id"])
        if m not in models:
            unknown.append(v["model_id"])
            continue
        g = models[m]
        rows = by_job.get(v["job_id"], [])
        t = {"model": m, "job_id": v["job_id"], "status": v.get("status"), "rung": v.get("ladder_reached"),
             "failure_class": v.get("failure_class"), "buildable": bool(g.get("buildable")),
             "entrypoint_used": v.get("entrypoint_used"), "entrypoint_source": v.get("entrypoint_source"),
             "corrections": [c for c in (v.get("annotation_corrections") or []) if c.get("outcome") in ("verified", "helped")],
             "charged_attempts": v.get("charged_attempts", v.get("attempts")),
             "infra_retries": v.get("infra_retries", 0),
             "llm_fallbacks": sum(1 for a in rows if a.get("classified_by") == "llm"),
             "step_attributed": sum(1 for a in rows if a.get("failed_rung") == "L0" and a.get("failed_step_index") is not None),
             "l0_failures": sum(1 for a in rows if a.get("failed_rung") == "L0")}
        if g.get("buildable"):
            ok = v.get("status") == "verified"
            t["honesty"] = honesty(v, rows) if ok else []
            t["success"] = ok and not t["honesty"]
            t["ran_ground_truth"] = _norm(v.get("entrypoint_used")) == _norm(g.get("example"))
            t["attribution"] = None if t["success"] else attribute(v, rows, g)
            t["correct"] = t["success"]
        else:
            t["success"] = None
            t["honesty"] = ["verified a non-buildable model"] if v.get("status") == "verified" else []
            t["correct"] = (v.get("status") == g.get("expected_verdict")
                            and (not g.get("expected_failure_class") or v.get("failure_class") == g["expected_failure_class"]))
            t["attribution"] = None
        trials.append(t)

    b = [t for t in trials if t["buildable"]]
    n_ok = sum(1 for t in b if t["success"])
    false_verified = [t for t in trials if t["honesty"]]
    attr = collections.Counter(t["attribution"] for t in b if t["attribution"])
    per_model = collections.defaultdict(lambda: {"trials": 0, "success": 0, "correct": 0})
    for t in trials:
        pm = per_model[t["model"]]
        pm["trials"] += 1
        pm["success"] += bool(t["success"])
        pm["correct"] += bool(t["correct"])
    total_l0 = sum(t["l0_failures"] for t in trials)
    return {
        "run_id": run_id, "trials": len(trials), "buildable_trials": len(b),
        "build_success": (n_ok / len(b)) if b else None,
        "build_success_ci95": _wilson(n_ok, len(b)) if b else None,
        "verdict_correctness": (sum(1 for t in trials if t["correct"]) / len(trials)) if trials else None,
        "false_verified": len(false_verified),
        "false_verified_trials": [{"job_id": t["job_id"], "model": t["model"], "why": t["honesty"]} for t in false_verified],
        "attribution": dict(attr),
        "corrected_annotation_and_succeeded": sum(1 for t in b if t["success"] and t["corrections"]),
        "median_charged_attempts": _median([t["charged_attempts"] for t in b if t["charged_attempts"] is not None]),
        "infra_retries_total": sum(t["infra_retries"] or 0 for t in trials),
        "llm_fallback_share": (sum(t["llm_fallbacks"] for t in trials)
                               / max(1, sum(len(by_job.get(t["job_id"], [])) for t in trials))),
        "failed_step_index_coverage": (sum(t["step_attributed"] for t in trials) / total_l0) if total_l0 else None,
        "per_model": dict(per_model), "unknown_models": sorted(set(unknown)), "trials_detail": trials,
    }


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def _pct(x):
    return "n/a" if x is None else f"{100 * x:5.1f} %"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("attempts", type=Path)
    ap.add_argument("verdicts", type=Path)
    ap.add_argument("--run-id")
    ap.add_argument("--ground-truth", type=Path, default=ROOT / "bench" / "ground_truth.yaml")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    gt = yaml.safe_load(args.ground_truth.read_text(encoding="utf-8"))
    res = score(_jsonl(args.attempts), _jsonl(args.verdicts), gt, args.run_id)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    print(f"run: {res['run_id'] or '(all rows)'}   trials: {res['trials']}   buildable trials: {res['buildable_trials']}\n")
    ci = res["build_success_ci95"]
    print(f"  build success        {_pct(res['build_success'])}"
          + (f"   (95% CI {100 * ci[0]:.0f}-{100 * ci[1]:.0f} %)" if ci else ""))
    print(f"  verdict correctness  {_pct(res['verdict_correctness'])}")
    print(f"  false-verified       {res['false_verified']}" + ("   <-- MUST BE 0" if res["false_verified"] else ""))
    print(f"  succeeded after correcting the annotation: {res['corrected_annotation_and_succeeded']}")
    print("\n  failure attribution (buildable, non-success):")
    for k, v in sorted(res["attribution"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:14s} {v}")
    print(f"\n  median charged attempts   {res['median_charged_attempts']}")
    print(f"  infra retries (uncharged) {res['infra_retries_total']}")
    print(f"  LLM-fallback share        {_pct(res['llm_fallback_share'])}")
    print(f"  failed_step_index cover.  {_pct(res['failed_step_index_coverage'])}")
    print("\n  per model:")
    for m, pm in sorted(res["per_model"].items()):
        g = gt["models"][m]
        kind = "buildable" if g.get("buildable") else f"control ({g.get('expected_verdict')})"
        print(f"    {m:22s} {kind:22s} trials={pm['trials']} success={pm['success']} correct={pm['correct']}")
    for t in res["trials_detail"]:
        if t["buildable"] and not t["success"]:
            print(f"      - {t['model']:20s} {t['job_id']}: {t['status']}@{t['rung']} {t['failure_class'] or ''}"
                  f" -> {t['attribution']}  ran={t['entrypoint_used'] or '?'} [{t['entrypoint_source'] or '?'}]")
    if res["unknown_models"]:
        print("\n  not in ground truth (unscored):", ", ".join(res["unknown_models"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
