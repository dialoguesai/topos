from __future__ import annotations

CHATGPT_DEV_PROFILE = {
    "profile_id": "chatgpt_dev",
    "queries": [
        "messages_per_day",
        "total_messages",
        "messages_by_sender",
        "avg_message_length",
    ],
}

PROFILE_REGISTRY = {
    CHATGPT_DEV_PROFILE["profile_id"]: CHATGPT_DEV_PROFILE,
    # Allow per-source profile ids to map to the shared ChatGPT profile.
    "chatgpt_file_ingestion": CHATGPT_DEV_PROFILE,
    "chatgpt_ui_conversation": CHATGPT_DEV_PROFILE,
}


def get_profile(profile_id: str) -> dict | None:
    return PROFILE_REGISTRY.get(profile_id)
