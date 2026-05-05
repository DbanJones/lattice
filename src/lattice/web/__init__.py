"""Lattice web UI: FastAPI app + WebSocket progress streaming.

The frontend is a single-page vanilla JS app served from
``web/static/``. WebSockets stream progress events from
``ProgressTracker`` callbacks straight to the browser timeline.

Runs go through the activity dispatcher in ``activities.py`` —
verb-named entry points (ingest, scaffold, draft, find_gaps, refine,
restructure, review). The older quick/standard/deep "review levels"
in ``runner.py`` are retained for backwards compatibility with
``run_history.json`` but no longer drive any UI surface.

Entry point: ``lattice serve`` boots the API + serves the static UI.
"""
