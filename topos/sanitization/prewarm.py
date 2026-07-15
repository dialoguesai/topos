"""Background prewarm for ingest-time sanitization models."""

from __future__ import annotations

import logging

logger = logging.getLogger("topos.sanitization.prewarm")


def prewarm_sanitization_models() -> None:
    """Load privacy-filter and NSFW pipelines so first ingest does not cold-start during UI use."""
    from topos.config.settings import settings

    if not getattr(settings, "sanitization_prewarm_on_startup", True):
        logger.debug("sanitization prewarm skipped (SANITIZATION_PREWARM_ON_STARTUP=off)")
        return

    try:
        from topos.sanitization.privacy_filter import prewarm_privacy_filter

        prewarm_privacy_filter()
        logger.info("sanitization prewarm: privacy-filter ready")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sanitization prewarm: privacy-filter failed (non-fatal): %s", exc)

    try:
        from topos.sanitization.nsfw_classifier import prewarm_nsfw_classifier

        prewarm_nsfw_classifier()
        logger.info("sanitization prewarm: nsfw classifier ready")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sanitization prewarm: nsfw classifier failed (non-fatal): %s", exc)
