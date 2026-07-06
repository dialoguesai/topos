"""Versioned synthetic bundles for the Enrichment Lab (no real user data)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

FIT_RECOMMENDED = "recommended"
FIT_SUPPORTED = "supported"
FIT_STRESS = "stress"

_TEXT_JOBS = ("emo_27", "sentiment", "entities", "topics", "embeddings", "goal_extraction")


def _fit(overrides: Dict[str, str], *, jobs: tuple = _TEXT_JOBS) -> Dict[str, str]:
    out = {job: FIT_SUPPORTED for job in jobs}
    for k, v in overrides.items():
        if v in (FIT_RECOMMENDED, FIT_SUPPORTED, FIT_STRESS):
            out[k] = v
    return out


_BUNDLES: List[Dict[str, Any]] = [
    {
        "id": "enrich.messages.personal",
        "label": "Personal messages",
        "description": "Chat-style snippets with emotional range (synthetic).",
        "bundle_version": "1",
        "enrichment_fit": _fit({"emo_27": FIT_RECOMMENDED, "sentiment": FIT_RECOMMENDED}),
        "records": [
            {"id": "pm1", "body": "I'm so proud of you — you crushed that interview!"},
            {"id": "pm2", "body": "honestly kind of dreading tomorrow's review meeting"},
            {"id": "pm3", "body": "can't stop laughing at the video you sent lol"},
            {"id": "pm4", "body": "I feel terrible about missing your birthday dinner."},
            {"id": "pm5", "body": "not sure how I feel about the move yet, it's a lot"},
            {"id": "pm6", "body": "THIS IS THE BEST NEWS I'VE HEARD ALL YEAR"},
            {"id": "pm7", "body": "thanks for checking in, means a lot right now"},
            {"id": "pm8", "body": "kinda annoyed the plans changed again last minute"},
        ],
    },
    {
        "id": "enrich.messages.entities",
        "label": "People, orgs, and places",
        "description": "Snippets dense with named entities (synthetic).",
        "bundle_version": "1",
        "enrichment_fit": _fit({"entities": FIT_RECOMMENDED, "topics": FIT_SUPPORTED}),
        "records": [
            {"id": "en1", "body": "Met Sarah Chen from Meridian Labs at the Berlin summit last week."},
            {"id": "en2", "body": "Lunch with Marcus at Cafe Aurora in Lisbon, then a call with Vertex Capital."},
            {"id": "en3", "body": "Dr. Okafor recommended the clinic on Hudson Street near Prospect Park."},
            {"id": "en4", "body": "Flying to Tokyo Thursday to see the Nakamura team about the Osaka rollout."},
            {"id": "en5", "body": "Priya joined Helios Energy as CTO; she was at Northwind before that."},
            {"id": "en6", "body": "The contract with Bramble & Fox goes to their Chicago office attn: J. Alvarez."},
        ],
    },
    {
        "id": "enrich.goals.journal",
        "label": "Goals and intentions",
        "description": "Journal-style entries stating goals and plans (synthetic).",
        "bundle_version": "1",
        "enrichment_fit": _fit({"goal_extraction": FIT_RECOMMENDED, "topics": FIT_RECOMMENDED}),
        "records": [
            {"id": "gl1", "body": "This quarter I want to ship the beta and get 50 real users testing it."},
            {"id": "gl2", "body": "Committing to running three mornings a week until the half marathon."},
            {"id": "gl3", "body": "Need to finally sort out the visa paperwork before the June deadline."},
            {"id": "gl4", "body": "Long term I'd like to move closer to family and work remotely."},
            {"id": "gl5", "body": "Saving 20% of every paycheck this year; want the house deposit by spring."},
            {"id": "gl6", "body": "Goal for the week: finish the grant draft and send it to two reviewers."},
        ],
    },
    {
        "id": "enrich.browser.urls",
        "label": "Browser history sample",
        "description": "Synthetic visited pages for URL/interest classification.",
        "bundle_version": "1",
        "enrichment_fit": {"url_classification": FIT_RECOMMENDED, "embeddings": FIT_SUPPORTED},
        "records": [
            {"id": "u1", "url": "https://news.example.com/markets/rates-decision", "title": "Central bank holds rates steady", "body": "Central bank holds rates steady"},
            {"id": "u2", "url": "https://recipes.example.org/thai-basil-noodles", "title": "20-minute Thai basil noodles", "body": "20-minute Thai basil noodles"},
            {"id": "u3", "url": "https://docs.example.dev/python/asyncio", "title": "asyncio — Asynchronous I/O", "body": "asyncio — Asynchronous I/O"},
            {"id": "u4", "url": "https://travel.example.net/guides/kyoto-autumn", "title": "Kyoto in autumn: a walking guide", "body": "Kyoto in autumn: a walking guide"},
            {"id": "u5", "url": "https://fitness.example.io/programs/5k-plan", "title": "Couch to 5K training plan", "body": "Couch to 5K training plan"},
            {"id": "u6", "url": "https://shop.example.com/product/espresso-grinder", "title": "Precision espresso grinder", "body": "Precision espresso grinder"},
        ],
    },
]

_PREVIEW_BODY_MAX_LEN = 4000


def list_bundle_metadata() -> List[Dict[str, Any]]:
    return [
        {
            "id": b["id"],
            "label": b["label"],
            "description": b["description"],
            "bundle_version": b["bundle_version"],
            "record_count": len(b["records"]),
            "enrichment_fit": dict(b["enrichment_fit"]),
        }
        for b in _BUNDLES
    ]


def get_bundle(bundle_id: str) -> Optional[Dict[str, Any]]:
    for b in _BUNDLES:
        if b["id"] == bundle_id:
            return b
    return None


def get_bundle_preview(bundle_id: str) -> Optional[Dict[str, Any]]:
    bundle = get_bundle(bundle_id)
    if not bundle:
        return None
    out = {k: v for k, v in bundle.items() if k != "records"}
    out["records"] = [
        {**r, "body": str(r.get("body") or "")[:_PREVIEW_BODY_MAX_LEN]}
        for r in bundle["records"]
    ]
    return out


def bundle_record_ids(bundle: Dict[str, Any]) -> List[str]:
    return [str(r["id"]) for r in bundle.get("records", []) if r.get("id")]


def is_bundle_compatible_with_job(bundle: Dict[str, Any], job_id: str) -> bool:
    return job_id in (bundle.get("enrichment_fit") or {})
