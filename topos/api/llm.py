from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends

from ..auth import require_api_key
from ..core.api_models import GenerationRequest, GenerationResponse, OllamaModelsResponse
from ..engine.usage_observation import emit_usage_observation
from ..rate_limit import rate_limit
from ..services.container import Services, get_services

logger = logging.getLogger("topos.api.llm")

router = APIRouter()


@router.post(
    "/llm_generation",
    response_model=GenerationResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def llm_generation(body: GenerationRequest):
    logger.info(
        "llm_generation request: provider=%r model=%r prompt_chars=%d",
        body.provider,
        body.model,
        len(body.prompt or ""),
    )
    services: Services = get_services()
    result = await services.llm.generate(body.model_dump())
    usage = result.get("usage") if isinstance(result, dict) else {}
    total_tokens = int((usage or {}).get("total_tokens") or (usage or {}).get("completion_tokens") or 0)
    await emit_usage_observation(
        action="llm.generate",
        quantity=total_tokens,
        producer="api.llm",
        canonical_action_identity={
            "provider": body.provider or "default",
            "model": body.model or "",
            "prompt_sha": hashlib.sha256(body.prompt.encode("utf-8")).hexdigest(),
            "max_tokens": body.max_tokens,
            "temperature": body.temperature,
        },
        trust_class="cp_observed_self_hosted",
    )
    return GenerationResponse(output=result["output"], model=result["model"], usage=result["usage"])


@router.post(
    "/llm/generate",
    response_model=GenerationResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def llm_generation_alias(body: GenerationRequest):
    logger.info(
        "llm_generation request (alias): provider=%r model=%r prompt_chars=%d",
        body.provider,
        body.model,
        len(body.prompt or ""),
    )
    services: Services = get_services()
    result = await services.llm.generate(body.model_dump())
    usage = result.get("usage") if isinstance(result, dict) else {}
    total_tokens = int((usage or {}).get("total_tokens") or (usage or {}).get("completion_tokens") or 0)
    await emit_usage_observation(
        action="llm.generate",
        quantity=total_tokens,
        producer="api.llm.alias",
        canonical_action_identity={
            "provider": body.provider or "default",
            "model": body.model or "",
            "prompt_sha": hashlib.sha256(body.prompt.encode("utf-8")).hexdigest(),
            "max_tokens": body.max_tokens,
            "temperature": body.temperature,
        },
        trust_class="cp_observed_self_hosted",
    )
    return GenerationResponse(output=result["output"], model=result["model"], usage=result["usage"])


@router.get(
    "/ollama_models",
    response_model=OllamaModelsResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def ollama_models_list():
    services: Services = get_services()
    result = await services.llm.list_ollama_models()
    return OllamaModelsResponse(models=result["models"])
