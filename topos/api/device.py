from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_api_key
from ..core.api_models import DeviceInfoResponse, DeviceNameRequest, DeviceNameResponse
from ..services.container import Services, get_services


router = APIRouter()


@router.get("/device/info", response_model=DeviceInfoResponse, dependencies=[Depends(require_api_key)])
async def get_device_info(services: Services = Depends(get_services)):
    return await services.device.get_device_info(context=None)


@router.get("/device_info", response_model=DeviceInfoResponse, dependencies=[Depends(require_api_key)])
async def get_device_info_alias(services: Services = Depends(get_services)):
    return await services.device.get_device_info(context=None)


@router.post("/device_name", response_model=DeviceNameResponse, dependencies=[Depends(require_api_key)])
async def set_device_name(body: DeviceNameRequest, services: Services = Depends(get_services)):
    return await services.device.set_device_name(body.device_name)
