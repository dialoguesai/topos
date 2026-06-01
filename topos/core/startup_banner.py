"""Startup banner display for Topos Control Plane."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from ..__version__ import __version__

logger = logging.getLogger("topos.core")


def print_startup_banner(
    engine_mode: Optional[str] = None,
    database_mode: Optional[str] = None,
    sync_enabled: bool = False,
) -> None:
    """
    Display a startup banner for Topos.

    Args:
        engine_mode: Engine mode (full, sync, etc.)
        database_mode: Database mode (local, postgres, etc.)
        sync_enabled: Whether sync is enabled
    """
    banner = """
╔═══════════════════════════════════════════════════════════════╗
║                                                                 ║
║        ████████╗ ██████╗ ██████╗  ██████╗ ███████╗           ║
║        ╚══██╔══╝██╔═══██╗██╔══██╗██╔═══██╗██╔════╝           ║
║           ██║   ██║   ██║██████╔╝██║   ██║███████╗           ║
║           ██║   ██║   ██║██╔═══╝ ██║   ██║╚════██║           ║
║           ██║   ╚██████╔╝██║     ╚██████╔╝███████║           ║
║           ╚═╝    ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝           ║
║                                                                 ║
║                 Control Plane v{version:8s}                    ║
║                                                                 ║
╚═══════════════════════════════════════════════════════════════╝
""".format(version=__version__)

    config_lines = []
    if engine_mode:
        config_lines.append(f"  Mode: {engine_mode}")
    if database_mode:
        config_lines.append(f"  Database: {database_mode}")
    config_lines.append(f"  Sync: {'enabled' if sync_enabled else 'disabled'}")

    config_info = "\n".join(config_lines) if config_lines else ""

    print(banner, file=sys.stderr)
    if config_info:
        print(config_info, file=sys.stderr)
    print("", file=sys.stderr)

    logger.info("=" * 65)
    logger.info("Topos Control Plane v%s", __version__)
    logger.info("=" * 65)
    if config_info:
        for line in config_lines:
            logger.info(line)
    logger.info("")
