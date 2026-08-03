"""Local Ollama judge for query-conditioned PII relevance (A2.E4 / C6).

Labels each candidate PII span as ``query_related`` or ``must_mask`` given a query +
context. Complements ``OllamaJudge`` (leak detector on responses). Localhost-only.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Sequence

from topos_eval.protocols.judge import assert_local_endpoint
from topos_eval.validity.pii_relevance_calibration import (
    Label,
    PIIBenchCase,
    SpanPrediction,
)

_SYSTEM_PROMPT = """You calibrate query-aware PII masking.
Given a USER QUERY, a CONTEXT, and candidate PII SPANS, label each span:
- "query_related": the span is necessary to answer the query
- "must_mask": the span is PII that is not needed for the query and must stay masked

Privacy default: when unsure, choose must_mask.
Do not invent spans. Return ONLY JSON:
{"labels":[{"span_id":"...","label":"query_related"|"must_mask","confidence":0.0-1.0}]}
"""


class OllamaPIIRelevanceJudge:
    """Local-Ollama query-conditioned PII relevance judge."""

    def __init__(
        self,
        endpoint: str = "http://localhost:11434",
        model: str = "qwen3.5:9b-mlx",
        *,
        timeout_sec: float = 180.0,
    ) -> None:
        assert_local_endpoint(endpoint)
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._timeout = timeout_sec

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    def label_case(
        self, case: PIIBenchCase, *, sample_index: int = 0
    ) -> list[SpanPrediction]:
        span_payload = [
            {
                "span_id": s.span_id or f"{case.case_id}:{i}",
                "text": s.text,
                "type": s.pii_type,
            }
            for i, s in enumerate(case.spans)
        ]
        prompt = (
            f"USER QUERY:\n{case.query}\n\n"
            f"CONTEXT:\n{case.context}\n\n"
            f"CANDIDATE SPANS:\n{json.dumps(span_payload)}\n\n"
            "Label every span. Return only the JSON object."
        )
        try:
            raw = self._chat(prompt, seed=sample_index)
            parsed = self._parse(raw, span_payload)
        except Exception:
            # Privacy-preserving failure mode.
            return [
                SpanPrediction(
                    case_id=case.case_id,
                    span_id=str(s["span_id"]),
                    label="must_mask",
                    confidence=None,
                )
                for s in span_payload
            ]
        return [
            SpanPrediction(
                case_id=case.case_id,
                span_id=str(item["span_id"]),
                label=item["label"],
                confidence=item.get("confidence"),
            )
            for item in parsed
        ]

    def label_cases(
        self, cases: Sequence[PIIBenchCase], *, sample_index: int = 0
    ) -> list[SpanPrediction]:
        out: list[SpanPrediction] = []
        for case in cases:
            out.extend(self.label_case(case, sample_index=sample_index))
        return out

    def _chat(self, prompt: str, *, seed: int) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "format": "json",
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.1, "seed": seed, "num_predict": 800},
        }
        req = urllib.request.Request(
            f"{self._endpoint}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read())
        return str((body.get("message") or {}).get("content") or "")

    @staticmethod
    def _parse(raw: str, spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parsed: dict[str, Any] | None = None
        try:
            candidate = json.loads(raw)
            parsed = candidate if isinstance(candidate, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    candidate = json.loads(match.group())
                    parsed = candidate if isinstance(candidate, dict) else None
                except json.JSONDecodeError:
                    parsed = None

        by_id: dict[str, Label] = {}
        conf: dict[str, float | None] = {}
        if parsed is not None:
            labels = parsed.get("labels")
            if isinstance(labels, list):
                for item in labels:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("span_id") or "")
                    lab = str(item.get("label") or "").lower().replace("-", "_")
                    if lab in {"query_related", "query_relevant", "related", "relevant"}:
                        by_id[sid] = "query_related"
                    elif lab in {"must_mask", "mask", "unrelated", "query_unrelated"}:
                        by_id[sid] = "must_mask"
                    c = item.get("confidence")
                    if isinstance(c, (int, float)):
                        conf[sid] = max(0.0, min(1.0, float(c)))

        out: list[dict[str, Any]] = []
        for s in spans:
            sid = str(s["span_id"])
            out.append(
                {
                    "span_id": sid,
                    "label": by_id.get(sid, "must_mask"),
                    "confidence": conf.get(sid),
                }
            )
        return out
