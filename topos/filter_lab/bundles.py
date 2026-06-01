"""Versioned preset bundles for Filter Lab (synthetic text only)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from topos.config.sanitization_ollama import SANITIZATION_OLLAMA_TRANSFORM_IDS

# Primary text field key in bundle records
TEXT_FIELD = "body"

SANITIZATION_CATEGORY = "sanitization"

# How strongly a bundle matches an Ollama transform (all remain runnable in Lab).
FIT_RECOMMENDED = "recommended"  # strong, primary eval signal for this transform
FIT_SUPPORTED = "supported"  # valid run; weaker or generic outcome expected
FIT_STRESS = "stress"  # edge / policy stress — useful but easy to misread


def _full_filter_fit(overrides: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {tid: FIT_SUPPORTED for tid in SANITIZATION_OLLAMA_TRANSFORM_IDS}
    for k, v in overrides.items():
        if k in out and v in (FIT_RECOMMENDED, FIT_SUPPORTED, FIT_STRESS):
            out[k] = v
    return out

# Cap per-record body size returned by GET /bundles/{id} (preview API).
_BUNDLE_PREVIEW_BODY_MAX_LEN = 8000

_BUNDLES: List[Dict[str, Any]] = [
    {
        "id": "lab.messages.casual",
        "label": "Casual messages",
        "description": "Short chat-style snippets (synthetic).",
        "bundle_version": "2",
        "max_input_chars": 12000,
        "compatible_filter_categories": [SANITIZATION_CATEGORY],
        "disclaimer_nsfw": False,
        "filter_fit": _full_filter_fit(
            {
                "raw_to_summary": FIT_RECOMMENDED,
                "raw_to_sentiment": FIT_RECOMMENDED,
                "third_party_anonymization": FIT_SUPPORTED,
                "name_removal": FIT_SUPPORTED,
                "contact_removal": FIT_SUPPORTED,
                "pii_redaction": FIT_STRESS,
                "nsfw_sanitization": FIT_STRESS,
            }
        ),
        "records": [
            {"id": "m1", "body": "hey are we still on for coffee at 3?"},
            {"id": "m2", "body": "lol ok just ping me when you're close"},
            {"id": "m3", "body": "running 10 min late sorry!!"},
            {"id": "m4", "body": "did you see the doc I shared in the channel?"},
            {"id": "m5", "body": "sounds good, let's sync tomorrow morning"},
            {"id": "m6", "body": "ugh this meeting could've been an email"},
            {"id": "m7", "body": "bring snacks if you can — no pressure"},
            {"id": "m8", "body": "haha yeah that's exactly what I meant"},
            {"id": "m9", "body": "call me if anything blocks you"},
            {"id": "m10", "body": "order pizza without me if I'm not there by 7"},
        ],
    },
    {
        "id": "lab.pii.synthetic",
        "label": "Synthetic PII stress",
        "description": "Fake names, emails, phones for redaction eval (not real people).",
        "bundle_version": "2",
        "max_input_chars": 12000,
        "compatible_filter_categories": [SANITIZATION_CATEGORY],
        "disclaimer_nsfw": False,
        "filter_fit": _full_filter_fit(
            {
                "pii_redaction": FIT_RECOMMENDED,
                "contact_removal": FIT_RECOMMENDED,
                "name_removal": FIT_RECOMMENDED,
                "third_party_anonymization": FIT_RECOMMENDED,
                "raw_to_summary": FIT_SUPPORTED,
                "raw_to_sentiment": FIT_SUPPORTED,
                "nsfw_sanitization": FIT_STRESS,
            }
        ),
        "records": [
            {
                "id": "p1",
                "body": "Contact Jane Doe at jane.doe@example.com or +1-555-0100. Address: 123 Fake St, Springfield.",
            },
            {"id": "p2", "body": "Invoice #9921 for Acme Corp; remit to payments@acmecorp.example."},
            {
                "id": "p3",
                "body": "Reach Priya Sharma at priya.sharma@fake-lab.example or WhatsApp +44 7700 900123.",
            },
            {
                "id": "p4",
                "body": "Ship to: 742 Evergreen Terrace, Springfield, IL 62704 — attn: Homer S.",
            },
            {
                "id": "p5",
                "body": "Patient MRN 883921; DOB 01/15/1980; emergency cousin.jane@hospital.test",
            },
            {
                "id": "p6",
                "body": "Wire transfer: routing 021000021, account ending 4521 (holder: John Q Public, Lab-Only).",
            },
            {
                "id": "p7",
                "body": "Driver license CA D1234567 — name: FAKEPERSON ONLYFORLAB, expires 2030-01-01.",
            },
            {"id": "p8", "body": "Tweet @totally_fake_handle DM for collab; backup email backup@social.test."},
            {
                "id": "p9",
                "body": "Beneficiary: Robert Tables; robert.tables@sql-injection.test; desk phone +1-555-0199 ext 42.",
            },
            {
                "id": "p10",
                "body": "Join call: https://totally-fake-meeting.example/join?id=abc123 — passcode: secret123",
            },
        ],
    },
    {
        "id": "lab.safety.edge_cases",
        "label": "Safety / edge phrasing (educational)",
        "description": "Borderline phrasing for policy-style filters; synthetic.",
        "bundle_version": "2",
        "max_input_chars": 12000,
        "compatible_filter_categories": [SANITIZATION_CATEGORY],
        "disclaimer_nsfw": True,
        "filter_fit": _full_filter_fit(
            {
                "nsfw_sanitization": FIT_RECOMMENDED,
                "raw_to_summary": FIT_SUPPORTED,
                "raw_to_sentiment": FIT_SUPPORTED,
                "pii_redaction": FIT_STRESS,
                "third_party_anonymization": FIT_STRESS,
                "name_removal": FIT_STRESS,
                "contact_removal": FIT_STRESS,
            }
        ),
        "records": [
            {
                "id": "s1",
                "body": "This is a clinical description of anatomy for a biology exam.",
            },
            {"id": "s2", "body": "Report uses metaphorical language; no actionable harm."},
            {
                "id": "s3",
                "body": "Politician used violent metaphor about 'crushing' opponents in debate.",
            },
            {
                "id": "s4",
                "body": "Historical fiction battle scene: strategic overview only, no gore.",
            },
            {
                "id": "s5",
                "body": "Harm-reduction pamphlet excerpt: factual risks of overdose, non-promotional (lab text).",
            },
            {
                "id": "s6",
                "body": "Crisis copy: if you are in crisis, contact a local hotline; 988-123-1234.",
            },
            {
                "id": "s7",
                "body": "Self-defense class outline: legal thresholds for use of force.",
            },
            {
                "id": "s8",
                "body": "Patient denies SI/HI; no acute psychosis documented.",
            },
            {
                "id": "s9",
                "body": "Why some titles earn a Mature label.",
            },
            {"id": "s10", "body": "How to fillet a fish safely with your knife and keep the blood to a minimum."},
        ],
    },
    {
        "id": "lab.nsfw.synthetic",
        "label": "Synthetic NSFW & profanity stress",
        "description": "Synthetic sexual references and strong language for nsfw_sanitization eval only.",
        "bundle_version": "1",
        "max_input_chars": 12000,
        "compatible_filter_categories": [SANITIZATION_CATEGORY],
        "disclaimer_nsfw": True,
        "filter_fit": _full_filter_fit(
            {
                "nsfw_sanitization": FIT_RECOMMENDED,
                "raw_to_summary": FIT_SUPPORTED,
                "raw_to_sentiment": FIT_SUPPORTED,
                "name_removal": FIT_SUPPORTED,
                "third_party_anonymization": FIT_SUPPORTED,
                "pii_redaction": FIT_STRESS,
                "contact_removal": FIT_STRESS,
            }
        ),
        "records": [
            {"id": "n1", "body": "This fucking deploy script failed again and I'm sick of it."},
            {"id": "n2", "body": "What the hell — did anyone even test this shit before shipping?"},
            {
                "id": "n3",
                "body": "After the party we hooked up; awkward the next day but we laughed it off.",
            },
            {
                "id": "n4",
                "body": "She sent an explicit photo; I deleted it and said not to do that on a work thread.",
            },
            {
                "id": "n5",
                "body": "Romance draft: they kissed, clothes came off, his dick throbbed, her pussy dripped, scene fades.",
            },
            {
                "id": "n6",
                "body": "Porn pop-ups hijack the lab browser; IT should block that crap on shared machines.",
            },
            {"id": "n7", "body": "He called his boss a dick in standup; HR opened a conduct review."},
            {
                "id": "n8",
                "body": "want u so bad tonight, can't wait to fuck you later",
            },
            {
                "id": "n9",
                "body": "Your service is bullshit and I'm charging back every goddamn cent.",
            },
            {
                "id": "n10",
                "body": "DTF tonight",
            },
        ],
    },
]


def list_bundle_metadata() -> List[Dict[str, Any]]:
    """Public list for GET /bundles (no full record bodies if large — here include record count only)."""
    out: List[Dict[str, Any]] = []
    for b in _BUNDLES:
        out.append(
            {
                "id": b["id"],
                "label": b["label"],
                "description": b.get("description"),
                "bundle_version": b["bundle_version"],
                "max_input_chars": b["max_input_chars"],
                "compatible_filter_categories": b["compatible_filter_categories"],
                "disclaimer_nsfw": bool(b.get("disclaimer_nsfw")),
                "record_count": len(b.get("records") or []),
                "filter_fit": dict(b.get("filter_fit") or {}),
            }
        )
    return out


def get_bundle(bundle_id: str) -> Optional[Dict[str, Any]]:
    for b in _BUNDLES:
        if b["id"] == bundle_id:
            return b
    return None


def get_bundle_preview(bundle_id: str) -> Optional[Dict[str, Any]]:
    """JSON shape for GET /v1/filter-lab/bundles/{bundle_id} (full record list for UI preview)."""
    b = get_bundle(bundle_id)
    if not b:
        return None
    records_out: List[Dict[str, str]] = []
    for r in b.get("records") or []:
        rid = r.get("id")
        if rid is None:
            continue
        body = record_text(r)
        if len(body) > _BUNDLE_PREVIEW_BODY_MAX_LEN:
            body = body[:_BUNDLE_PREVIEW_BODY_MAX_LEN] + "\n[TRUNCATED]"
        records_out.append({"id": str(rid), "body": body})
    return {
        "id": b["id"],
        "label": b["label"],
        "description": b.get("description"),
        "bundle_version": b["bundle_version"],
        "max_input_chars": b["max_input_chars"],
        "compatible_filter_categories": list(b.get("compatible_filter_categories") or []),
        "disclaimer_nsfw": bool(b.get("disclaimer_nsfw")),
        "filter_fit": dict(b.get("filter_fit") or {}),
        "records": records_out,
    }


def record_text(record: Dict[str, Any]) -> str:
    raw = record.get(TEXT_FIELD)
    if not isinstance(raw, str):
        return ""
    return raw


def bundle_record_ids(bundle: Dict[str, Any]) -> List[str]:
    return [str(r["id"]) for r in bundle.get("records") or [] if r.get("id") is not None]


def is_bundle_compatible_with_filter(bundle: Dict[str, Any], filter_id: str) -> bool:
    if filter_id not in SANITIZATION_OLLAMA_TRANSFORM_IDS:
        return False
    cats = bundle.get("compatible_filter_categories") or []
    return SANITIZATION_CATEGORY in cats
