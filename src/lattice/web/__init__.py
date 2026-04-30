"""Lattice web UI: FastAPI app + WebSocket progress streaming.

Three review levels (quick, standard, deep) sit on top of the existing
pipeline modules. The frontend is a single-page vanilla JS app served
from `web/static/`. WebSockets stream progress events from
``ProgressTracker`` callbacks straight to the browser timeline.

Entry point: ``lattice serve`` boots the API + serves the static UI.
"""
