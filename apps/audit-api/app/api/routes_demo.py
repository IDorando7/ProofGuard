from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import Settings, get_settings
from app.core.paths import protocol_data_root
from app.schemas.demo_bootstrap import DemoBootstrapResult
from app.services.demo_bootstrap_service import (
    DemoBootstrapError,
    bootstrap_gold_demo_network,
)
from app.services.local_agent_executor import (
    LocalAgentRegistry,
    get_local_agent_registry,
)


router = APIRouter(prefix="/demo", tags=["demo"])


@router.post("/bootstrap", response_model=DemoBootstrapResult)
def bootstrap_demo_network(
    root: Path = Depends(protocol_data_root),
    settings: Settings = Depends(get_settings),
    registry: LocalAgentRegistry = Depends(get_local_agent_registry),
) -> DemoBootstrapResult:
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return bootstrap_gold_demo_network(
            root,
            registry,
            demo_mode=settings.demo_mode,
        )
    except DemoBootstrapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

