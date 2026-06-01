from __future__ import annotations

from dataclasses import dataclass

from ..config.settings import settings
from .embeddings.base import EmbeddingsService
from .embeddings.local import LocalEmbeddingsService
from .embeddings.remote import RemoteEmbeddingsService
from .interfaces import DbService, DeviceService, LLMService, SyncService
from .local import LocalDbService, LocalDeviceService, LocalSyncService
from .llm.openai import OpenAILLMService
from .postgres import HostedDeviceService, HostedSyncService, PostgresDbService


@dataclass(frozen=True)
class Services:
    db: DbService
    sync: SyncService
    device: DeviceService
    llm: LLMService
    embeddings: EmbeddingsService


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        if settings.topos_database_mode == "postgres":
            _services = Services(
                db=PostgresDbService(),
                sync=HostedSyncService(),
                device=HostedDeviceService(),
                llm=OpenAILLMService(),
                embeddings=RemoteEmbeddingsService(),
            )
        else:
            _services = Services(
                db=LocalDbService(),
                sync=LocalSyncService(),
                device=LocalDeviceService(),
                llm=OpenAILLMService(),
                embeddings=LocalEmbeddingsService(),
            )
    return _services
