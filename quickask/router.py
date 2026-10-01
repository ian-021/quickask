"""The Jev router: one TypeSafe call per query, two typed questions.

  scope           Choice over every scope name plus "none". Each option's
                  criteria text is the scope's description, so Jev knows what
                  the label means. Returns the pick, per-option probabilities,
                  and a confidence derived from that distribution.
  needs_frontier  Noul (yes/no): is this too hard for a fast small model?
                  Returns the probability of "yes".

Everything here is synchronous and Qt-free. The popup runs route() on a worker
thread; the test harness and shadow logger call it directly. It never raises:
a timeout, a missing key, or any API error comes back as a RoutingResult with
`error` set and scope "none", which the caller treats as "no routing".
"""

import sys
import time
from dataclasses import asdict, dataclass, field

from . import config

NONE = "none"

NONE_DESCRIPTION = (
    "Not about any of the projects above: general knowledge, trivia, unit "
    "conversions, arithmetic, definitions, spelling, translations, or a "
    "question about some unrelated tool or topic."
)

SCOPE_INSTRUCTIONS = (
    "Which project is this question about? `question` is what the user typed; "
    "`frontmost_app` and `window_title`, when present, say what they were "
    "looking at when they asked, which may hint at the project. Pick `none` "
    "when the question is not about any listed project."
)

FRONTIER_INSTRUCTIONS = (
    "Is this question too hard for a fast, small language model, so it should "
    "go to a frontier model instead? Judge the `question` itself."
)
FRONTIER_CRITERIA = {
    "true": {
        "what": "Needs deep multi-step reasoning, careful reading of a lot of context, "
                "nuanced judgment, novel design, or subtle debugging where a small "
                "model is likely to be confidently wrong.",
        "examples": ["Why does my distributed lock still allow two writers under partition?",
                     "Design a migration plan for this schema with zero downtime",
                     "Prove this algorithm is O(n log n) and find the edge case that breaks it"],
    },
    "false": {
        "what": "A lookup, definition, conversion, short snippet, syntax reminder, or "
                "any quick factual answer a small model gets right.",
        "examples": ["how many ounces in a cup", "python list comprehension with condition",
                     "what does HTTP 429 mean"],
    },
}


@dataclass
class RoutingResult:
    scope: str = NONE                 # a scope name, or "none"
    scope_confidence: float = 0.0     # Jev's confidence in that pick, 0..1
    needs_frontier_prob: float = 0.0  # P(yes) that a frontier model is needed
    latency_ms: int = 0
    error: str = None                 # set => treat as "no routing"
    probabilities: dict = field(default_factory=dict)  # every option -> prob
    model: str = None                 # versioned model that answered

    @property
    def routed(self):
        """True when the router picked a real scope without error. The
        confidence gate is applied by the caller, not here."""
        return self.error is None and self.scope != NONE

    def to_dict(self):
        return asdict(self)


def scope_criteria(scopes):
    """The Choice options: {name: description} for each scope, then none."""
    criteria = {s.name: s.description for s in scopes}
    criteria[NONE] = NONE_DESCRIPTION
    return criteria


def build_questions(scopes):
    from typesafe_sdk import Choice, Noul
    return {
        "scope": Choice(instructions=SCOPE_INSTRUCTIONS, criteria=scope_criteria(scopes)),
        "needs_frontier": Noul(instructions=FRONTIER_INSTRUCTIONS, criteria=FRONTIER_CRITERIA),
    }


_client = None


def _get_client(api_key, timeout):
    """One client per process, so the HTTP connection is reused between
    queries (saves a TLS handshake per call). Rebuilt if the key changes."""
    global _client
    from typesafe_sdk import RetryPolicy, TypeSafeClient
    if _client is None or _client[0] != api_key:
        client = TypeSafeClient(
            api_key=api_key,
            model=config.JEV_MODEL,
            timeout=timeout,
            # No retries: a retry would blow the time budget, and the caller
            # falls back to the default path on failure anyway.
            retry=RetryPolicy(max_retries=0, timeout=timeout),
        )
        _client = (api_key, client)
    return _client[1]


def route(state, scopes, timeout=None, api_key=None):
    """Ask Jev about `state` (the object from desktop.build_state). Never
    raises. `scopes` is any iterable of Scope objects."""
    from typesafe_sdk import TypeSafeAPITimeoutError, TypeSafeError

    timeout = config.ROUTER_TIMEOUT if timeout is None else timeout
    scopes = list(scopes)
    started = time.monotonic()

    def done(**kw):
        return RoutingResult(latency_ms=int((time.monotonic() - started) * 1000), **kw)

    api_key = api_key or config.load_typesafe_key()
    if not api_key:
        return done(error="no TYPESAFE_API_KEY (set it in .env or ~/.config/quickask/typesafe_key)")
    if not scopes:
        return done(error="no scopes loaded")

    try:
        client = _get_client(api_key, timeout)
        response = client.system_one(state=state, questions=build_questions(scopes),
                                     timeout=timeout)
    except TypeSafeAPITimeoutError:
        return done(error=f"jev timed out after {timeout}s")
    except TypeSafeError as e:
        status = getattr(e, "status", None)
        return done(error=f"jev error{f' ({status})' if status else ''}: {_short(e)}")
    except Exception as e:  # anything else: still just "no routing"
        return done(error=f"jev error: {_short(e)}")

    try:
        scope = response.choices["scope"]
        frontier = response.nouls["needs_frontier"]
        pick = scope.choice if scope.choice in scope_criteria(scopes) else NONE
        return done(
            scope=pick,
            scope_confidence=float(scope.confidence),
            needs_frontier_prob=float(frontier.noul),
            probabilities={k: round(float(v), 4) for k, v in scope.probabilities.items()},
            model=response.model,
        )
    except Exception as e:
        return done(error=f"unexpected jev response: {_short(e)}")


def _short(e):
    text = " ".join(str(e).split())
    return text[:200] if len(text) > 200 else text or type(e).__name__


def main(argv=None):
    """python -m quickask.router "question" [app name] [window title]: route
    one query from the command line and print the result."""
    import json
    from . import desktop, scopes as scopes_mod
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__.split("\n\n")[0] + "\n\nusage: python -m quickask.router QUESTION [APP] [TITLE]")
        return 2
    config.setup()
    loaded = scopes_mod.load_scopes()
    for err in loaded.errors:
        print(f"scopes: {err}", file=sys.stderr)
    state = desktop.build_state(argv[0], desktop.DesktopState(
        app_name=argv[1] if len(argv) > 1 else None,
        window_title=argv[2] if len(argv) > 2 else None))
    result = route(state, loaded)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 1 if result.error else 0


if __name__ == "__main__":
    sys.exit(main())
