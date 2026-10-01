"""Review the routing log.

    python -m quickask.review_log            last 50 entries
    python -m quickask.review_log -n 200     last 200
    python -m quickask.review_log --all
    python -m quickask.review_log --file path/to/routing.jsonl

Prints one row per query plus a short summary: how often Jev picked a scope,
how often that pick would have passed the threshold, how the router path
compared with what actually ran, error rate, and latency.
"""

import argparse
import sys

from . import routing_log

NONE = "none"


def _cut(text, width):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _would_route(e):
    """What the gate would have done with this line, checkbox aside."""
    if e.get("error") or e.get("scope") in (None, NONE):
        return None
    threshold = e.get("scope_threshold", 0.6)
    return e["scope"] if e.get("scope_confidence", 0) >= threshold else None


def format_table(entries, width=None):
    """The rows as text. Columns: time, jev on/off, app, question, Jev's pick
    with confidence, frontier probability, latency, path used, error."""
    cols = [
        ("time", 16), ("jev", 3), ("app", 14), ("question", 44),
        ("jev scope", 24), ("front", 5), ("ms", 5), ("path", 22), ("error", 30),
    ]
    header = "  ".join(f"{name:<{w}}" for name, w in cols)
    lines = [header, "-" * len(header)]
    for e in entries:
        if e.get("error"):
            pick = "—"
        else:
            pick = f"{e.get('scope')} {e.get('scope_confidence', 0):.2f}"
        would = _would_route(e)
        if would and e.get("path") == "default":
            pick += " ↑"   # would have routed if the box had been checked
        title = e.get("window_title")
        app = e.get("app_name") or ""
        if title:
            app = f"{app}"
        row = [
            (e.get("timestamp") or "")[:16].replace("T", " "),
            "on" if e.get("use_jev") else "off",
            _cut(app, 14),
            _cut(e.get("question"), 44),
            _cut(pick, 24),
            f"{e.get('needs_frontier_prob', 0):.2f}" if not e.get("error") else "",
            str(e.get("latency_ms", "")),
            _cut(e.get("path"), 22),
            _cut(e.get("error"), 30),
        ]
        lines.append("  ".join(f"{val:<{w}}" for val, (_, w) in zip(row, cols)).rstrip())
    return "\n".join(lines)


def summarize(entries):
    n = len(entries)
    if not n:
        return "no entries"
    errors = [e for e in entries if e.get("error")]
    ok = [e for e in entries if not e.get("error")]
    picked = [e for e in ok if e.get("scope") not in (None, NONE)]
    gated = [e for e in ok if _would_route(e)]
    used_scope = [e for e in entries if (e.get("path") or "").startswith("scope:")]
    on = [e for e in entries if e.get("use_jev")]
    frontier = [e for e in ok if e.get("needs_frontier_prob", 0) >= 0.7]
    lat = sorted(e.get("latency_ms", 0) for e in ok)
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else 0
    # Disagreements worth a look: routed but Jev picked none/low, or the box
    # was on and the gate passed but the path was default (timeout race).
    lines = [
        f"{n} queries, {len(on)} with 'use jev' on, {len(errors)} router errors "
        f"({100 * len(errors) / n:.0f}%)",
        f"Jev picked a scope in {len(picked)}/{len(ok)} answered queries; "
        f"{len(gated)} pass the confidence gate; {len(used_scope)} actually ran with a scope",
        f"frontier hint (≥0.7) on {len(frontier)}/{len(ok)}",
        f"latency ms: p50 {p(0.5)}  p90 {p(0.9)}  max {lat[-1] if lat else 0}",
    ]
    by_scope = {}
    for e in gated:
        by_scope[e["scope"]] = by_scope.get(e["scope"], 0) + 1
    if by_scope:
        lines.append("gate passes by scope: " + ", ".join(f"{k} {v}" for k, v in sorted(by_scope.items())))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-n", type=int, default=50, help="show the last N entries (default 50)")
    ap.add_argument("--all", action="store_true", help="show every entry")
    ap.add_argument("--file", help="log file (default: the app's routing.jsonl)")
    args = ap.parse_args(argv)
    entries = routing_log.read(args.file, last=None if args.all else args.n)
    if not entries:
        print(f"no entries in {args.file or routing_log.default_log_file()}")
        return 0
    print(format_table(entries))
    print()
    print(summarize(entries))
    print("\n↑ = Jev would have routed this query had 'use jev' been on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
