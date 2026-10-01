"""Run the fixtures through the router and report accuracy and misses.

    python -m quickask.eval_router                 all cases
    python -m quickask.eval_router --only trawl    cases expecting one scope
    python -m quickask.eval_router --timeout 5     be patient (default: the app's)
    python -m quickask.eval_router --no-desktop    questions only, no app/title

Two numbers are reported. "argmax" counts Jev's top pick against the label.
"gated" applies the popup's rule: a scope counts only when its confidence is
at or above the threshold, otherwise the result is "none". Gated is what the
popup does, so it is the one to tune for; argmax tells you whether a miss is
a wrong pick or just a timid one.
"""

import argparse
import sys
import time

from . import config, desktop, router
from .fixtures import CASES
from .scopes import load_scopes


def gate(result, threshold):
    if result.error or result.scope == router.NONE:
        return router.NONE
    return result.scope if result.scope_confidence >= threshold else router.NONE


def run(cases, scopes, timeout, threshold, use_desktop=True):
    rows = []
    for c in cases:
        ds = desktop.DesktopState(app_name=c.get("app"), window_title=c.get("title")) if use_desktop else None
        state = desktop.build_state(c["question"], ds)
        result = router.route(state, scopes, timeout=timeout)
        rows.append((c, result, gate(result, threshold)))
    return rows


def report(rows, threshold):
    n = len(rows)
    argmax_ok = sum(1 for c, r, _ in rows if not r.error and r.scope == c["expected"])
    gated_ok = sum(1 for c, _, g in rows if g == c["expected"])
    errors = [(c, r) for c, r, _ in rows if r.error]
    lat = sorted(r.latency_ms for _, r, _ in rows if not r.error)
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else 0

    print(f"cases: {n}   argmax accuracy: {argmax_ok}/{n} ({100 * argmax_ok / n:.0f}%)   "
          f"gated accuracy (threshold {threshold:g}): {gated_ok}/{n} ({100 * gated_ok / n:.0f}%)")
    print(f"router errors: {len(errors)}   latency ms: p50 {p(0.5)}  p90 {p(0.9)}  max {lat[-1] if lat else 0}")

    # Per expected scope: how many of its cases were gated right.
    per = {}
    for c, _, g in rows:
        hit, tot = per.get(c["expected"], (0, 0))
        per[c["expected"]] = (hit + (g == c["expected"]), tot + 1)
    print("per scope (gated): " + "   ".join(f"{k} {h}/{t}" for k, (h, t) in sorted(per.items())))

    misses = [(c, r, g) for c, r, g in rows if g != c["expected"]]
    if misses:
        print(f"\nmisses ({len(misses)}):")
    for c, r, g in misses:
        where = " · ".join(x for x in (c.get("app"), c.get("title")) if x) or "(no desktop)"
        print(f"\n  Q: {c['question']}")
        print(f"     seen in: {where}")
        if r.error:
            print(f"     error: {r.error}")
            continue
        kind = ("timid" if r.scope == c["expected"] else "wrong")
        print(f"     expected {c['expected']}, gated -> {g}  [{kind}]"
              f"   argmax {r.scope} conf {r.scope_confidence:.2f}   frontier {r.needs_frontier_prob:.2f}")
        dist = ", ".join(f"{k} {v:.2f}" for k, v in sorted(r.probabilities.items(), key=lambda kv: -kv[1]))
        print(f"     probabilities: {dist}")
    return 0 if gated_ok == n else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="only cases whose expected scope is this")
    ap.add_argument("--timeout", type=float, help="router timeout in seconds (default: the app's)")
    ap.add_argument("--threshold", type=float, help="confidence gate (default: the app's)")
    ap.add_argument("--no-desktop", action="store_true", help="send the question only")
    args = ap.parse_args(argv)

    config.setup()
    loaded = load_scopes()
    for err in loaded.errors:
        print(f"scopes: {err}", file=sys.stderr)
    if not loaded:
        print("no scopes loaded; nothing to route to", file=sys.stderr)
        return 2
    templates = [s.name for s in loaded if s.is_template]
    if templates:
        print(f"note: still templates (TODO descriptions): {', '.join(templates)}\n")

    cases = [c for c in CASES if not args.only or c["expected"] == args.only]
    threshold = config.SCOPE_THRESHOLD if args.threshold is None else args.threshold
    started = time.monotonic()
    rows = run(cases, loaded, args.timeout, threshold, use_desktop=not args.no_desktop)
    code = report(rows, threshold)
    print(f"\n({time.monotonic() - started:.1f}s total)")
    return code


if __name__ == "__main__":
    sys.exit(main())
