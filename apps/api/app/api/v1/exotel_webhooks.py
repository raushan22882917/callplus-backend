"""Exotel's status callback for outbound calls we placed through it (app/services/exotel.py).

Like the Twilio status webhook, it only finalizes calls that never reached the realtime gateway
(no answer, busy, failed); a call that connected is ended by the gateway's /end. Exotel does not
sign callbacks, so the URL carries a token issued for that one call.
"""

import json
import uuid

from app.core.deps import get_db
from app.services import exotel
from app.services.call_finalize import TERMINAL_CALL_STATUSES, finalize_call
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import PlainTextResponse

from packages.database.models import Call, CallStatus

router = APIRouter(prefix="/webhooks/telephony/exotel", tags=["telephony-webhooks"])

_EXOTEL_STATUS_MAP = {
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.FAILED,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.FAILED,
}


async def _payload(request: Request) -> dict:
    """We ask for JSON, but accept a form too: Exotel's older callbacks post one."""
    body = await request.body()
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    return dict((await request.form()).items())


@router.post("/status")
async def status_callback(
    request: Request,
    call_id: uuid.UUID = Query(...),
    t: str = Query(""),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    if not exotel.status_token_ok(call_id, t):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid callback")
    call = await db.get(Call, call_id)
    if call is None:
        return PlainTextResponse("")
    data = await _payload(request)
    new_status = _EXOTEL_STATUS_MAP.get(str(data.get("Status") or data.get("CallStatus") or ""))
    if new_status is not None and call.status not in TERMINAL_CALL_STATUSES:
        duration = str(data.get("ConversationDuration") or data.get("Duration") or "")
        await finalize_call(
            db,
            call,
            status=new_status,
            reason=f"exotel_reported_{data.get('Status') or data.get('CallStatus')}",
            duration_seconds=int(duration) if duration.isdigit() else None,
        )
    return PlainTextResponse("")
