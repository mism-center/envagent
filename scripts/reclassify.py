#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Re-derive failure classes for historical attempt rows, offline.

    uv run scripts/reclassify.py outputs/attempts.jsonl            # histogram only
    uv run scripts/reclassify.py outputs/attempts.jsonl -o out.jsonl
    uv run scripts/reclassify.py outputs/attempts.jsonl --show UNKNOWN

Runs `logs.decode` + the current `classify.py` rule table over each row's
`error_raw` and reports old class -> new class. This is how a rule-table change
is evaluated against the corpus BEFORE a cluster run: the classifier is pure and
the rows are all there. It never rewrites attempts.jsonl in place -- `-o` writes
a copy with `failure_class_v2`, `classifier_rule_v2`, `routes_to_v2` and
`error_signature_v2` added, so both readings stay comparable.

Caveat that the numbers carry: rows recorded before src/logs.py existed hold the
raw tail of a progress stream, and for some of them the real message was cut
off before the 8 KB window. Those stay UNKNOWN here and would not recur -- the
builder now decodes before truncating.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import classify   # noqa: E402
import logs       # noqa: E402
import normalize  # noqa: E402


def reclassify_row(row: dict) -> dict:
    text = logs.decode(row.get("error_raw") or "")
    if not text and not row.get("failure_class"):
        return {}                                   # a passing attempt
    rung = row.get("failed_rung") or "L0"
    # A failed L3 row had a non-zero exit by construction; the historical
    # schema did not record the code.
    exit_code = row.get("exit_code") if row.get("exit_code") is not None else (1 if rung == "L3" else None)
    c = classify.classify(text, rung=rung, local_modules=(), exit_code=exit_code)
    old = row.get("failure_class")
    # Two classes depend on context the row does not carry (the repo's own
    # module names). When the live classifier had it and the replay does not,
    # the live answer stands.
    if old in ("IMPORT_PATH_ERROR", "RUNTIME_ERROR") and row.get("classified_by") == "rule" \
            and c.failure_class in ("UNKNOWN", "MISSING_DEPENDENCY"):
        c = classify.Classification(old, classified_by="rule", rule="(historical, context-dependent)",
                                    routes_to=classify.ROUTING[old])
    return {
        "failure_class_v2": c.failure_class,
        "classifier_rule_v2": c.rule,
        "routes_to_v2": c.routes_to,
        "classified_by_v2": c.classified_by,
        "error_signature_v2": normalize.signature(text),
        "charged_v2": c.routes_to != "infra",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("attempts", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="write rows + v2 fields here (never in place)")
    ap.add_argument("--show", metavar="CLASS", help="print salient lines for rows re-derived as CLASS")
    args = ap.parse_args(argv)
    if args.out and args.out.resolve() == args.attempts.resolve():
        ap.error("refusing to overwrite the input; pick another -o path")

    rows = [json.loads(ln) for ln in args.attempts.read_text(encoding="utf-8").splitlines() if ln.strip()]
    trans = collections.Counter()
    by_rule = collections.Counter()
    sigs_old, sigs_new = set(), set()
    out_rows = []
    for row in rows:
        v2 = reclassify_row(row)
        out_rows.append({**row, **v2})
        if not v2:
            continue
        old = row.get("failure_class") or "None"
        trans[(old, v2["failure_class_v2"])] += 1
        by_rule[v2["classifier_rule_v2"] or "(llm)"] += 1
        if row.get("error_signature"):
            sigs_old.add(row["error_signature"])
        sigs_new.add(v2["error_signature_v2"])
        if args.show and v2["failure_class_v2"] == args.show:
            sal = normalize.salient_lines(logs.decode(row.get("error_raw") or ""))
            print(f"--- {row['job_id']} a{row['attempt']} {row.get('failed_rung')} (was {old})")
            for ln in sal[-4:]:
                print("   ", ln[:160])

    n = sum(trans.values())
    old_hist = collections.Counter(o for (o, _), k in trans.items() for _ in range(k))
    new_hist = collections.Counter(nw for (_, nw), k in trans.items() for _ in range(k))
    print(f"{n} failed attempts in {args.attempts}\n")
    print(f"{'class':32s} {'recorded':>9s} {'re-derived':>11s}")
    for cls in sorted(set(old_hist) | set(new_hist), key=lambda c: -new_hist.get(c, 0)):
        print(f"{cls:32s} {old_hist.get(cls, 0):9d} {new_hist.get(cls, 0):11d}")
    print(f"\nrouted to llm: {old_hist.get('UNKNOWN', 0)} -> {new_hist.get('UNKNOWN', 0)}")
    print(f"infra (uncharged) attempts: {new_hist.get('INFRA_UNAVAILABLE', 0)}")
    print(f"distinct error signatures: {len(sigs_old)} -> {len(sigs_new)}")
    print("\nrule hits:")
    for rule, k in by_rule.most_common():
        print(f"  {k:4d}  {rule}")
    changed = [(o, nw, k) for (o, nw), k in trans.items() if o != nw]
    if changed:
        print("\nchanges (recorded -> re-derived):")
        for o, nw, k in sorted(changed, key=lambda t: -t[2]):
            print(f"  {k:4d}  {o} -> {nw}")

    if args.out:
        args.out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in out_rows),
                            encoding="utf-8")
        print(f"\nwrote {len(out_rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
