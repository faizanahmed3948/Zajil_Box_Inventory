# BoxTrack

A box/order/invoice inventory tracker for Zajil Fiber Glass, built as a PWA.

The backend is written in **Python (FastAPI)** with **SQLite** (swappable to
Postgres) and real-time sync over **WebSockets**. It replaces what used to
be a Firebase (Firestore + Auth) backend.

## Project structure

```
backend/     Python API - auth, data storage, real-time sync
frontend/    The PWA itself (index.html + service worker + icons)
```

## How it works

- **Auth** - email/password accounts with JWT bearer tokens. Passwords are
  hashed with bcrypt, never stored in plain text.
- **Data** - a small generic document store (SQLAlchemy models in
  `backend/models.py`): every record (a box, an order, an invoice, ...) is
  a JSON document keyed by collection name + id. This keeps the API tiny
  and means new fields added on the frontend never require a database
  migration.
- **Real-time sync** - a single WebSocket endpoint (`/ws`) broadcasts the
  fresh contents of a collection to every connected client whenever
  something changes, so multiple people using the app at once see each
  other's changes immediately - no polling, no page refresh.
- **Frontend integration** - `frontend/api-client.js` is a small shim that
  exposes the same function names the app's ~6,000 lines of existing logic
  already called (`onSnapshot`, `setDoc`, `signInWithEmailAndPassword`,
  etc), just backed by calls to the Python API instead of Firebase. That
  meant migrating the backend didn't require rewriting the app itself.

## Running it locally

### 1. Backend

```bash
cd backend
python3 -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

The API is now live at `http://localhost:8000` (docs at
`http://localhost:8000/docs`). A `boxtrack.db` SQLite file is created
automatically on first run - nothing else to configure for local dev.

### 2. Frontend

The frontend is a set of static files, so any static file server works:

```bash
cd frontend
python3 -m http.server 8090
```

Open `http://localhost:8090/index.html`. It already points at
`http://localhost:8000` for the backend when running on `localhost`/
`127.0.0.1`, so signing up and logging in should work immediately.

The first account you create can be made an Admin at signup by checking
"Register as Admin" and entering the access code found near the top of
`frontend/index.html`'s script (`ADMIN_CODE`) - change this code before
sharing the app with anyone.

## Deploying

- **Backend**: any host that runs a Python/ASGI app works (Render, Fly.io,
  Railway, a VPS with `uvicorn`/`gunicorn`, etc). Set the `DATABASE_URL`
  environment variable to a Postgres connection string for production use
  instead of SQLite, and set `JWT_SECRET` to a long random value (see
  `backend/.env.example`).
- **Frontend**: any static host works (GitHub Pages, Netlify, Vercel,
  Render static sites). After deploying the backend, update the
  `apiBase` fallback URL near the top of `frontend/index.html`'s script
  to point at your backend's public URL.

## Notes

- CORS is wide open (`allow_origins=["*"]`) for simplicity - tighten this
  in `backend/main.py` if you deploy somewhere the API is publicly
  reachable and you want to restrict which frontends can call it.
- Admin/employee permissions are enforced the same way the previous
  version handled them: mostly client-side, with the exception of
  reassigning another user's role, which requires an admin token
  server-side. Treat this as an internal tool rather than a public-facing
  one unless you harden the permission checks further.
