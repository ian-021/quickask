"""Hand-written routing cases for the test harness (python -m quickask.eval_router).

Each case: the question as typed, the app and window title the user was
looking at (either may be None), and the scope you expect. "none" is for
questions that belong to no project. Edit freely: these are your labels, and
the harness exists so you can tune scope descriptions against them.

The project-specific cases assume scope descriptions that say what each
project is. While the scopes are still templates, expect misses on them.
"""

CASES = [
    # --- trawl -----------------------------------------------------------
    {"question": "why is the crawler retrying 429s forever",
     "app": "Cursor", "title": "scheduler.py — trawl", "expected": "trawl"},
    {"question": "how does trawl dedup pages by canonical url",
     "app": "Brave Browser", "title": "trawl/README.md at main", "expected": "trawl"},
    {"question": "robots.txt crawl-delay: does trawl honor it per host or per domain",
     "app": None, "title": None, "expected": "trawl"},
    {"question": "what's the retry backoff in the fetcher",
     "app": "Ghostty", "title": "~/code/trawl", "expected": "trawl"},

    # --- inference-engine ------------------------------------------------
    {"question": "how do I batch requests in the inference engine",
     "app": "Cursor", "title": "batcher.rs — inference-engine", "expected": "inference-engine"},
    {"question": "kv cache eviction policy",
     "app": "Cursor", "title": "kv_cache.rs — inference-engine", "expected": "inference-engine"},
    {"question": "why is decode throughput dropping after 2k tokens",
     "app": None, "title": None, "expected": "inference-engine"},
    {"question": "does the engine support paged attention",
     "app": "Brave Browser", "title": "vLLM paged attention paper", "expected": "inference-engine"},

    # --- reading-assistant -----------------------------------------------
    {"question": "why does the reading assistant lose my highlights",
     "app": "Xcode", "title": "HighlightStore.swift", "expected": "reading-assistant"},
    {"question": "how are epub chapters split for the assistant",
     "app": None, "title": None, "expected": "reading-assistant"},
    {"question": "sync annotations between iPad and Mac",
     "app": "Xcode", "title": "reading-assistant — SyncEngine.swift", "expected": "reading-assistant"},

    # --- none: trivia, conversions, unrelated tools -----------------------
    {"question": "how many ounces in a cup",
     "app": "Brave Browser", "title": "Recipe: banana bread", "expected": "none"},
    {"question": "convert 72F to C",
     "app": "Cursor", "title": "scheduler.py — trawl", "expected": "none"},
    {"question": "git undo last commit but keep changes",
     "app": "Ghostty", "title": "~/code/trawl", "expected": "none"},
    {"question": "what is a monad",
     "app": None, "title": None, "expected": "none"},
    {"question": "spell necessary",
     "app": "Mail", "title": "Re: invoice", "expected": "none"},
    {"question": "capital of mongolia",
     "app": "Brave Browser", "title": "trawl/README.md at main", "expected": "none"},
]
