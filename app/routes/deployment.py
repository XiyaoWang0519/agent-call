from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.security import require_deploy_guard_token

router = APIRouter(
    prefix="/internal/deployment-lock",
    tags=["internal"],
    dependencies=[Depends(require_deploy_guard_token)],
)


@router.post("", include_in_schema=False)
async def acquire_deployment_lock(request: Request) -> dict[str, int | bool]:
    active_calls = await request.app.state.call_service.acquire_deployment_lock()
    if active_calls:
        raise HTTPException(
            status_code=409,
            detail={"ready": False, "active_calls": active_calls},
        )
    return {"ready": True, "active_calls": 0}


@router.delete("", include_in_schema=False)
async def release_deployment_lock(request: Request) -> dict[str, bool]:
    await request.app.state.call_service.release_deployment_lock()
    return {"released": True}
