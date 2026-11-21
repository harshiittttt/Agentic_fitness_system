"""HTTP surface for push: /push/register (POST/DELETE), /push/settings, /push/test, /push/receipts/check."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import push

router = APIRouter(prefix="/push", tags=["push"])


class RegisterIn(BaseModel):
    token: str
    platform: str = ""


class PrefsIn(BaseModel):
    enabled: bool | None = None
    categories: dict[str, bool] | None = None


@router.post("/register")
def register(r: RegisterIn):
    try:
        push.register(r.token, r.platform)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"registered": True}


@router.delete("/register")
def unregister():
    return {"registered": False, "removed": push.unregister()}


@router.get("/settings")
def settings():
    return push.status()


@router.put("/settings")
def put_settings(p: PrefsIn):
    try:
        push.set_prefs(p.enabled, p.categories)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return push.status()


@router.post("/test")
def test():
    res = push.send_test()
    if res["reason"] == "no_token":
        raise HTTPException(409, "This phone is not registered for push yet")
    return res


@router.post("/receipts/check")
def receipts():
    return push.check_receipts()
