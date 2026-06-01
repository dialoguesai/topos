from __future__ import annotations


class EncryptionManager:
    """No-op encryption manager placeholder."""

    def __init__(self, dataset_id: str, user_id: str):
        self.dataset_id = dataset_id
        self.user_id = user_id
        self._crypto_version = 1

    def get_crypto_version(self) -> int:
        return self._crypto_version

    def encrypt_str(self, payload: str, crypto_version: int | None = None) -> str:
        _ = crypto_version
        return payload

    def decrypt_str(self, ciphertext: str, crypto_version: int | None = None) -> str:
        _ = crypto_version
        return ciphertext
