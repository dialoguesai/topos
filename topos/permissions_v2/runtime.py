"""Explicit, disabled-by-default, single-process beta protocol configuration."""
from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


from .canonical import PolicyError
from .contract import Hash, Identifier, StrictModel
from .ledger import NodeIdentity, PolicyLedger
from .node_protocol import NodePolicyProtocol
from .protection_clock import current_protection_revision, ensure_protection_clock


class NodeProtocolConfig(StrictModel):
    version: Literal["topos-policy-node-config/v1"]
    identity: NodeIdentity
    cp_issuer_id: Identifier
    frontend_client_id: Identifier
    trusted_cp_keys: dict[Identifier, Hash]
    node_signing_kid: Identifier
    node_signing_key_path: str
    canonical_database_path: str
    ledger_path: str
    evidence_review_store_path: str | None = None


class Runtime:
    def __init__(self, protocol: NodePolicyProtocol, lock_file, config_path: Path, *, evidence_review_store_path: Path | None = None):
        self.protocol = protocol
        self.lock_file = lock_file
        self.config_path = config_path
        self.pid = os.getpid()
        self.evidence_review_store_path = evidence_review_store_path
        self._evidence_review_runtime = None

    def evidence_reviews(self, *, require_existing=True):
        """Trusted runtime accessor; request payloads never select enrollment."""
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        if os.environ.get("TOPOS_PERMISSIONS_V2_ENABLED", "").lower() != "true":
            raise PolicyError("permissions_v2_disabled")
        if os.environ.get("TOPOS_PERMISSIONS_V2_EVIDENCE_REVIEWS_ENABLED", "").lower() != "true":
            raise PolicyError("evidence_reviews_disabled")
        if self.evidence_review_store_path is None:
            raise PolicyError("evidence_reviews_not_configured")
        from .evidence import EvidenceBinding
        from .evidence_review_runtime import ReviewEnrollmentRuntime
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            if self._evidence_review_runtime is None:
                self._evidence_review_runtime = ReviewEnrollmentRuntime(canonical_database=self.protocol.canonical_database,
                    binding=EvidenceBinding.parse(self.protocol.ledger.identity.model_dump()), path=self.evidence_review_store_path)
            return self._evidence_review_runtime.get(require_existing=require_existing)

    def close(self):
        self.lock_file.close()


_runtime: Runtime | None = None
_lock = threading.Lock()


def _private_file(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise PolicyError("private_config_required")
    return path.read_bytes()


def load_runtime(config_path: Path, *, active_database: Path) -> Runtime:
    """Explicit trusted config; no environment secret/key discovery fallback."""
    config_path = config_path.resolve(strict=True)
    config = NodeProtocolConfig.parse(_private_file(config_path))
    if not config.identity.environment_id.startswith("permissions-beta-") or not config.trusted_cp_keys:
        raise PolicyError("beta_configuration_required")
    for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = os.environ.get(variable, "1")
        if not value.isdecimal() or int(value) != 1:
            raise PolicyError("single_process_required")
    canonical = Path(config.canonical_database_path)
    ledger_path = Path(config.ledger_path)
    key_path = Path(config.node_signing_key_path)
    if not all(path.is_absolute() for path in (canonical, ledger_path, key_path)):
        raise PolicyError("absolute_paths_required")
    canonical = canonical.resolve(strict=True)
    if canonical != active_database.resolve(strict=True):
        raise PolicyError("canonical_database_binding")
    durable = canonical.parent / "permissions-v2"
    durable.mkdir(mode=0o700, exist_ok=True)
    if durable.is_symlink() or durable.stat().st_mode & 0o077:
        raise PolicyError("private_directory_required")
    if ledger_path.resolve().parent != durable or key_path.resolve(strict=True).parent != durable:
        raise PolicyError("durable_path_binding")
    review_path = Path(config.evidence_review_store_path) if config.evidence_review_store_path is not None else None
    if review_path is not None:
        from .evidence import _checked_file
        marker_path = review_path.with_name(review_path.name + ".enrollment.json")
        if (not review_path.is_absolute() or review_path.parent != durable
            or {review_path, marker_path} & {canonical, ledger_path, key_path, config_path, durable / "protocol.lock"}):
            raise PolicyError("review_database_binding")
        _checked_file(review_path, code="review_database_binding", may_create=True)
        _checked_file(marker_path, code="review_database_binding", may_create=True)
    try:
        seed = bytes.fromhex(_private_file(key_path).decode("ascii").strip())
        key = Ed25519PrivateKey.from_private_bytes(seed)
    except (UnicodeError, ValueError):
        raise PolicyError("node_signing_key_invalid") from None
    lock_file = (durable / "protocol.lock").open("a+")
    os.chmod(lock_file.name, 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise PolicyError("single_process_required") from None
    try:
        # Existing floor is loaded only to reopen the ledger; the first signed
        # request synchronizes actual protection before claiming current state.
        if ledger_path.exists():
            with sqlite3.connect(ledger_path.as_uri() + "?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT protection_revision FROM p2a_node WHERE singleton=1").fetchone()
                if row is None:
                    raise PolicyError("ledger_identity")
                revision = row[0]
        else:
            ensure_protection_clock(canonical, owner_id=config.identity.owner_id)
            with sqlite3.connect(canonical.as_uri() + "?mode=ro", uri=True) as conn:
                conn.execute("BEGIN")
                revision = current_protection_revision(conn, owner_id=config.identity.owner_id)
        keys = {kid: bytes.fromhex(value) for kid, value in config.trusted_cp_keys.items()}
        ledger = PolicyLedger(ledger_path, identity=config.identity, protection_revision=revision, trusted_keys=keys)
        protocol = NodePolicyProtocol(ledger, canonical_database=canonical, cp_issuer_id=config.cp_issuer_id, frontend_client_id=config.frontend_client_id, trusted_cp_keys=keys, node_signing_kid=config.node_signing_kid, node_signing_key=key)
        return Runtime(protocol, lock_file, config_path, evidence_review_store_path=review_path)
    except BaseException:
        lock_file.close()
        raise


def get_runtime() -> Runtime:
    global _runtime
    if os.environ.get("TOPOS_PERMISSIONS_V2_ENABLED", "").lower() != "true":
        raise PolicyError("permissions_v2_disabled")
    raw_path = os.environ.get("TOPOS_PERMISSIONS_V2_CONFIG_PATH", "")
    if not raw_path or not Path(raw_path).is_absolute():
        raise PolicyError("beta_configuration_required")
    path = Path(raw_path).resolve(strict=True)
    with _lock:
        if _runtime is not None:
            if _runtime.pid != os.getpid() or _runtime.config_path != path:
                raise PolicyError("configuration_restart_required")
            return _runtime
        from topos.config.settings import settings
        if not settings.topos_database_path:
            raise PolicyError("canonical_database_binding")
        _runtime = load_runtime(path, active_database=Path(settings.topos_database_path))
        return _runtime
