"""
Tiny pub/sub broadcaster.

Every connected client receives every channel's updates (channels are just
collection names, e.g. "boxes", "orders", "settings/business"). The client
filters locally for the channels it actually cares about. This is the
simplest thing that works well for a small internal tool - no per-client
subscription bookkeeping needed.
"""
import asyncio
import json
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, payload: dict):
        message = json.dumps(payload)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self.active:
                        self.active.remove(ws)


manager = ConnectionManager()
