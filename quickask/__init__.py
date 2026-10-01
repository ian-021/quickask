"""quickask: scope routing for llm-popup.

The popup itself stays in llm_popup.py. This package holds the parts that do
not need Qt, so they can be run and tested from the command line:

  scopes        one markdown file per project, loaded and validated at startup
  desktop       frontmost app and window title (osascript), the Jev state
  config        .env loading, API keys, router tunables
  router        the Jev call: scope Choice + needs_frontier Noul -> RoutingResult
  routing_log   one JSON line per query, checkbox on or off
  review_log    python -m quickask.review_log   read that log as a table
  fixtures      labeled example queries
  eval_router   python -m quickask.eval_router  accuracy of the router on them
"""
