"""Admin API (token protected). Filled out in later milestones."""

import hmac

from fastapi import APIRouter, Header, HTTPException

from defuser.config import get_settings

router = APIRouter(prefix="/admin")


def require_admin(authorization: str | None) -> None:
    token = get_settings().admin_token
    if not token or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401)
    if not hmac.compare_digest(authorization.removeprefix("Bearer "), token.get_secret_value()):
        raise HTTPException(401)


@router.get("/ping")
async def ping(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return {"ok": True}
