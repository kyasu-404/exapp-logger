from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request

from ..config import in_nextcloud_runtime
from ..security import webhook_token
from ..services.events import process_event_payload

router = APIRouter(tags=["events"])


@router.post("/api/events/nextcloud")
async def nextcloud_event(request: Request):
    payload = await request.json()
    if in_nextcloud_runtime():
        supplied = request.headers.get("X-File-Control-Webhook", "")
        if not hmac.compare_digest(supplied, webhook_token()):
            raise HTTPException(status_code=403, detail="Некорректная подпись события")
        signed_user = str(request.scope.get("username") or "")
        payload_user = str((payload.get("user") or {}).get("uid") or "")
        if signed_user and payload_user and signed_user != payload_user:
            raise HTTPException(status_code=403, detail="Пользователь события не совпадает с подписью AppAPI")
    try:
        return await process_event_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
