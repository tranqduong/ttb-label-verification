"""
Vercel serverless entrypoint.

Vercel's Python runtime expects each file under api/ to expose a WSGI/ASGI
app object at module level -- it discovers `app` here and wraps it. This
file does no work of its own; it just makes the existing backend package
importable from Vercel's function context (which runs api/index.py with
the repo root on sys.path, not backend/ itself) and re-exports the same
FastAPI app that `uvicorn app.main:app` runs locally. Local development
and the Vercel deployment run the identical application code -- nothing
here is Vercel-specific except this import shim.

Frontend static files (frontend/index.html) are NOT served through this
function on Vercel -- see vercel.json, which serves frontend/ directly via
Vercel's static file hosting and rewrites "/" to it. That keeps the static
frontend on Vercel's CDN rather than round-tripping it through a Python
serverless invocation. Locally (via `uvicorn`), app/main.py's own
StaticFiles mount still serves it, since that code path is untouched.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.main import app  # noqa: E402,F401
