"""
BoxTrack API - a small Python backend replacing Firebase.

- Auth: email/password with JWT bearer tokens (bcrypt-hashed passwords).
- Data: a generic per-collection document store (SQLAlchemy + SQLite by
  default), so the flexible, schema-less records the frontend already
  works with (boxes, orders, invoices, ...) don't need a rigid SQL schema.
- Real-time: a single WebSocket endpoint broadcasts the fresh contents of
  a collection to every connected client whenever something changes,
  mirroring the "live snapshot" behaviour the frontend used to get from
  Firestore.
"""
import time
from typing import Any

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import User, Document, new_id
from auth import (
    hash_password, verify_password, create_token,
    get_current_user, require_admin, decode_token_for_ws,
)
from ws_manager import manager

Base.metadata.create_all(bind=engine)

app = FastAPI(title="BoxTrack API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Collections handled by the generic document store. "users" is deliberately
# excluded - it has its own table/router below because auth needs real
# fields (password hashes) rather than free-form JSON.
COLLECTIONS = {"boxes", "orders", "auditLog", "invoices", "customers", "companies", "settings"}


def _doc_list(db: Session, collection: str) -> list[dict]:
    rows = db.query(Document).filter(Document.collection == collection).all()
    return [r.as_doc() for r in rows]


async def _broadcast_collection(db: Session, collection: str):
    await manager.broadcast({"channel": collection, "type": "collection", "docs": _doc_list(db, collection)})


async def _broadcast_users(db: Session):
    rows = db.query(User).all()
    await manager.broadcast({"channel": "users", "type": "collection", "docs": [u.as_doc() for u in rows]})


async def _broadcast_settings_business(db: Session):
    row = db.query(Document).filter(Document.collection == "settings", Document.doc_id == "business").first()
    await manager.broadcast({
        "channel": "settings/business",
        "type": "doc",
        "id": "business",
        "exists": row is not None,
        "data": row.as_doc() if row else None,
    })


async def _after_write(db: Session, collection: str, doc_id: str):
    if collection == "settings" and doc_id == "business":
        await _broadcast_settings_business(db)
    else:
        await _broadcast_collection(db, collection)


# ── Auth ───────────────────────────────────────────────────────────────
@app.post("/api/auth/signup")
def signup(body: dict, db: Session = Depends(get_db)):
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    display_name = body.get("displayName") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail={"code": "auth/invalid-email", "message": "Email and password are required."})
    if len(password) < 6:
        raise HTTPException(status_code=400, detail={"code": "auth/weak-password", "message": "Password must be at least 6 characters."})
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail={"code": "auth/email-already-in-use", "message": "This email is already registered."})

    user = User(
        uid=new_id(), email=email, password_hash=hash_password(password),
        display_name=display_name, role="employee", created_at=int(time.time() * 1000),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": create_token(user.uid), "user": user.as_doc()}


@app.post("/api/auth/login")
def login(body: dict, db: Session = Depends(get_db)):
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "auth/invalid-credential", "message": "Incorrect email or password."})
    return {"token": create_token(user.uid), "user": user.as_doc()}


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return user.as_doc()


@app.patch("/api/auth/me")
async def update_me(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if "displayName" in body:
        user.display_name = body["displayName"] or ""
    db.commit()
    await _broadcast_users(db)
    return user.as_doc()


# ── Users (admin-managed roles) ─────────────────────────────────────────
@app.get("/api/users")
def list_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [u.as_doc() for u in db.query(User).all()]


@app.get("/api/users/{uid}")
def get_user(uid: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(User).filter(User.uid == uid).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return row.as_doc()


@app.put("/api/users/{uid}")
async def put_user(uid: str, body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Self-service profile upsert - mirrors the app's fallback that creates
    a missing 'users' profile doc for the signed-in account."""
    if uid != user.uid and user.role != "admin":
        raise HTTPException(status_code=403, detail="Cannot write another user's profile")
    row = db.query(User).filter(User.uid == uid).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if "displayName" in body:
        row.display_name = body["displayName"] or ""
    if "role" in body:
        row.role = body["role"]
    db.commit()
    await _broadcast_users(db)
    return row.as_doc()


@app.patch("/api/users/{uid}")
async def patch_user_role(uid: str, body: dict, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.query(User).filter(User.uid == uid).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if "role" in body:
        row.role = body["role"]
    db.commit()
    await _broadcast_users(db)
    return row.as_doc()


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Generic document collections (boxes, orders, auditLog, invoices, ── #
# ── customers, companies, settings) ─────────────────────────────────── #
def _check_collection(collection: str):
    if collection not in COLLECTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown collection '{collection}'")


@app.get("/api/{collection}")
def list_docs(collection: str, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_collection(collection)
    return _doc_list(db, collection)


@app.post("/api/{collection}")
async def create_doc(collection: str, body: dict, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_collection(collection)
    doc_id = new_id()
    row = Document(collection=collection, doc_id=doc_id, data=body)
    db.add(row)
    db.commit()
    await _after_write(db, collection, doc_id)
    return {"id": doc_id}


@app.get("/api/{collection}/{doc_id}")
def get_doc(collection: str, doc_id: str, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_collection(collection)
    row = db.query(Document).filter(Document.collection == collection, Document.doc_id == doc_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return row.as_doc()


@app.put("/api/{collection}/{doc_id}")
async def set_doc(collection: str, doc_id: str, body: dict, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Full replace, creating the document if it doesn't exist yet (setDoc)."""
    _check_collection(collection)
    row = db.query(Document).filter(Document.collection == collection, Document.doc_id == doc_id).first()
    if row:
        row.data = body
    else:
        row = Document(collection=collection, doc_id=doc_id, data=body)
        db.add(row)
    db.commit()
    await _after_write(db, collection, doc_id)
    return row.as_doc()


@app.patch("/api/{collection}/{doc_id}")
async def merge_doc(collection: str, doc_id: str, body: dict, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Partial merge update, creating the doc if missing (updateDoc / setDoc-merge)."""
    _check_collection(collection)
    row = db.query(Document).filter(Document.collection == collection, Document.doc_id == doc_id).first()
    if row:
        merged = dict(row.data or {})
        merged.update(body)
        row.data = merged
    else:
        row = Document(collection=collection, doc_id=doc_id, data=body)
        db.add(row)
    db.commit()
    await _after_write(db, collection, doc_id)
    return row.as_doc()


@app.delete("/api/{collection}/{doc_id}")
async def delete_doc(collection: str, doc_id: str, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_collection(collection)
    row = db.query(Document).filter(Document.collection == collection, Document.doc_id == doc_id).first()
    if row:
        db.delete(row)
        db.commit()
    await _after_write(db, collection, doc_id)
    return {"ok": True}


# ── Real-time WebSocket ──────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    db = next(get_db())
    try:
        user = decode_token_for_ws(token, db)
        if not user:
            await websocket.close(code=4401)
            return

        await manager.connect(websocket)
        # Clients do an initial REST fetch for each collection before opening
        # this socket, so no initial snapshot is sent here - just live pushes.
        try:
            while True:
                await websocket.receive_text()  # client sends nothing meaningful; keeps the socket alive
        except WebSocketDisconnect:
            pass
    finally:
        await manager.disconnect(websocket)
        db.close()

