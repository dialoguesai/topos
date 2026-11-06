"""Topos Facts: the per-pack fact pass without a generative model.

The owner picks it under Settings → Models → Node functions → Facts extraction
(provider ``topos_facts``; ``config.facts_llm``). Off by default. When it is on, the
derivation job asks this module instead of the LLM for every pack it knows, except
``ROUTED_TO_LLM``, which keeps the local extraction model.

Two published encoders, pinned by revision because they decide what the node writes
about its owner. Bumping them is a release, not a fetch:

- gate: DeBERTa-v3-large NLI cross-encoder (MIT). For each predicate of a pack it scores
  "The author mentions their <predicate> (<pack title>)." against the message, and for each
  fired predicate it picks the options of its choice fields the same way.
- reader: DeBERTa-v3-large extractive QA (CC-BY-4.0, from deepset/deberta-v3-large-squad2).
  It copies each free-text field out of the message, or leaves it empty.

Every value is a listed option or text copied from the record. Output is the LLM's JSON
shape, so ``template.parse_output`` applies the same schema, enum and person-grounding
checks, and the verifier still judges every assertion.

Training data. v0.1's training data is private, so it is an exception to the rule
``query.scope_head`` enforces for routing heads (public or synthetic training data only).
The owner ruled on 3 Nov 2026 to ship it as an opt-in option, off by default. Nothing a
node holds is sent anywhere: inference runs in process.

Measured (aggregates only): on the 40-message synthetic gold set, predicate precision
0.81 against Qwen 9B's 0.53, recall 0.45 against 0.83.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION = "v0.1"
GATE_REPO = "Dialogues/topos-facts-gate-v0.1"
GATE_REVISION = "bd4bd85a4f9e62fe5affd95d1a8c3f28ab133565"
READER_REPO = "Dialogues/topos-facts-reader-v0.1"
READER_REVISION = "ef45d5a241d9039ae63db1f137c315c298170778"
SPEC_FILE = "topos_facts_spec.json"
MODEL_ID = f"topos-facts-{VERSION}"

#: Packs that keep the local extraction model while Topos Facts is on. Goals are where
#: v0.1 over-produces most, and the owner ruled (3 Nov 2026) that they stay on the LLM.
ROUTED_TO_LLM = frozenset({"aspirations.goals"})

ENV_GATE = "TOPOS_FACTS_GATE"        # local directory: never contacts a network
ENV_READER = "TOPOS_FACTS_READER"
ENV_DEVICE = "TOPOS_FACTS_DEVICE"    # cpu (default) | auto | mps | cuda
ENV_IDLE = "TOPOS_FACTS_IDLE_SEC"    # release the encoders after this long unused; 0 = never
#: Ollama's own keep-alive for the local models (5 min): an idle node holds no model (owner,
#: 6 Nov 2026), so the pin outlives a batch by no more than the LLMs it runs beside.
DEFAULT_IDLE_SEC = 300.0
_release_timer: Optional[threading.Timer] = None
_timer_lock = threading.Lock()

_WEIGHTS = ["config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json"]
_ABOUT = "__about"
_BUDGET = 12000  # tokens per forward batch


def resolve_device(preferred: Optional[str] = None) -> str:
    """``cpu`` unless asked, for scope_head's reason: an accelerator-resident model
    contends with the owner's LLM for memory, and torch/MPS has wedged this node before."""
    choice = (preferred or os.environ.get(ENV_DEVICE) or "cpu").strip().lower()
    if choice != "auto":
        return choice or "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 — a probe must never break loading
        logger.debug("device probe failed; using cpu", exc_info=True)
    return "cpu"


def _local_dir(env: str, repo: str, revision: str, extra: Sequence[str] = ()) -> Path:
    override = os.environ.get(env)
    if override:
        return Path(override)
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=repo, revision=revision, allow_patterns=[*_WEIGHTS, *extra]))


class ToposFacts:
    """Both encoders and the pack spec, resident through the model cache (see ``load``)."""

    def __init__(self, gate_dir: Path, reader_dir: Path, device: str) -> None:
        import torch
        from transformers import (AutoModelForQuestionAnswering,
                                  AutoModelForSequenceClassification, AutoTokenizer)

        self.torch, self.dev = torch, device
        dtype = torch.float32 if device == "cpu" else torch.float16
        self.spec = json.loads((gate_dir / SPEC_FILE).read_text())
        self.thr = self.spec["thresholds"]
        self.gtok = AutoTokenizer.from_pretrained(gate_dir)
        self.gate = AutoModelForSequenceClassification.from_pretrained(gate_dir, dtype=dtype).to(device).eval()
        self.ent = {v.lower(): int(k) for k, v in self.gate.config.id2label.items()}["entailment"]
        self.rtok = AutoTokenizer.from_pretrained(reader_dir)
        self.reader = AutoModelForQuestionAnswering.from_pretrained(reader_dir, dtype=dtype).to(device).eval()
        self._lock = threading.Lock()

    def knows(self, pack_id: str) -> bool:
        return pack_id in self.spec["packs"]

    def _nli(self, pairs: List[Tuple[str, str]]) -> List[float]:
        torch = self.torch
        enc = [self.gtok(t, h, truncation="only_first", max_length=self.spec["max_length"])["input_ids"] for t, h in pairs]
        order = sorted(range(len(enc)), key=lambda k: len(enc[k]))
        out, i = [0.0] * len(enc), 0
        with torch.no_grad():
            while i < len(order):
                j = i + 1
                while j < len(order) and (j - i + 1) * len(enc[order[j]]) <= _BUDGET:
                    j += 1
                idx = order[i:j]
                feats = self.gtok.pad({"input_ids": [enc[k] for k in idx]}, return_tensors="pt").to(self.dev)
                probs = torch.softmax(self.gate(**feats).logits.float(), -1)[:, self.ent].cpu().tolist()
                for k, p in zip(idx, probs):
                    out[k] = p
                i = j
        return out

    def _spans(self, pairs: List[Tuple[str, str]]) -> List[Tuple[float, str]]:
        torch, L, S = self.torch, self.spec["max_length"], self.spec["max_span"]
        es = [self.rtok(q, t, truncation="only_second", max_length=L, return_offsets_mapping=True) for q, t in pairs]
        order = sorted(range(len(es)), key=lambda k: len(es[k]["input_ids"]))
        out: List[Any] = [None] * len(es)
        i = 0
        with torch.no_grad():
            while i < len(order):
                j = i + 1
                while j < len(order) and (j - i + 1) * len(es[order[j]]["input_ids"]) <= _BUDGET:
                    j += 1
                idx = order[i:j]
                feats = self.rtok.pad({"input_ids": [es[k]["input_ids"] for k in idx]}, return_tensors="pt").to(self.dev)
                o = self.reader(**feats)
                st, en = o.start_logits.float().cpu(), o.end_logits.float().cpu()
                for row, k in enumerate(idx):
                    seq, off = es[k].sequence_ids(), es[k]["offset_mapping"]
                    ctx = [p for p, s in enumerate(seq) if s == 1]
                    null = float(st[row, 0] + en[row, 0])
                    best, span = None, ""
                    top_s = sorted(ctx, key=lambda p: -float(st[row, p]))[:20]
                    top_e = sorted(ctx, key=lambda p: -float(en[row, p]))[:20]
                    for a in top_s:
                        for b in top_e:
                            if a <= b < a + S:
                                v = float(st[row, a] + en[row, b])
                                if best is None or v > best:
                                    best, span = v, pairs[k][1][off[a][0]:off[b][1]]
                    out[k] = ((best if best is not None else null - 99) - null, span)
                i = j
        return out

    def extract(self, pack_id: str, text: str) -> List[Dict[str, Any]]:
        """Assertions in the LLM's shape: predicate, value, confidence, about."""
        with self._lock:
            return self._extract(pack_id, text)

    def _extract(self, pack_id: str, text: str) -> List[Dict[str, Any]]:
        preds = self.spec["packs"][pack_id]["predicates"]
        names = list(preds)
        gates = self._nli([(text, preds[q]["hypothesis"]) for q in names])
        fired = [(q, p) for q, p in zip(names, gates) if p >= self.thr["gate"][preds[q]["tier"]]]
        if not fired:
            return []
        cells = []
        for m, (q, _p) in enumerate(fired):
            d = preds[q]
            for f, opts in d["options"].items():
                for o, h in opts.items():
                    cells.append((m, f, o, h))
            cells.append((m, _ABOUT, "author", f"{d['hypothesis']} {self.spec['about_suffix']}"))
        scores = self._nli([(text, h) for _m, _f, _o, h in cells])
        by: Dict[int, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
        for (m, f, o, _h), s in zip(cells, scores):
            by[m][f][o] = s
        facts: List[Optional[Dict[str, Any]]] = []
        asks = []
        for m, (q, p) in enumerate(fired):
            d, cell = preds[q], by[m]
            who = cell.get(_ABOUT, {}).get("author", 1.0)
            if who < self.thr["who"][d["tier"]]:
                facts.append(None)
                continue
            value: Dict[str, Any] = {}
            for fd in d["fields"]:
                if fd["choices"]:
                    if cell.get(fd["name"]):
                        option, v = max(cell[fd["name"]].items(), key=lambda kv: kv[1])
                        if v >= self.thr["option"]:
                            value[fd["name"]] = option
                else:
                    asks.append((m, fd["name"], (d["questions"][fd["name"]], text)))
            facts.append({"predicate": q, "value": value, "confidence": round(p, 4), "about": "owner"})
        for (m, f, _), (margin, span) in zip(asks, self._spans([a[2] for a in asks]) if asks else []):
            if margin >= self.thr["span"] and span.strip() and facts[m] is not None:
                facts[m]["value"][f] = span.strip()
        out = []
        for (q, _p), fact in zip(fired, facts):
            if fact is None:
                continue
            fields = [fd["name"] for fd in preds[q]["fields"]]
            if fields == ["value"]:
                fact["value"] = fact["value"].get("value")
            out.append(fact)
        return out


def idle_seconds() -> float:
    """How long the encoders stay resident after their last batch (``TOPOS_FACTS_IDLE_SEC``)."""
    raw = os.environ.get(ENV_IDLE)
    try:
        return max(0.0, float(raw)) if raw not in (None, "") else DEFAULT_IDLE_SEC
    except ValueError:
        return DEFAULT_IDLE_SEC


def load() -> ToposFacts:
    """The resident model, loading it on first use (download once, then the HF cache).

    Pinned in the model cache: the cache's LRU budget is sized for the small enrichment
    encoders, which every ingest chunk cycles through, so an entry that joins that rotation
    is evicted between derivation batches. Measured 6 Nov 2026 on an isolated node: 8 loads
    in 8 batches, about 5 GiB of float32 each time. Pinned, the encoders load once and stay
    until the owner picks another provider (``release``) or they sit idle ``idle_seconds``;
    a timer armed here releases them then, whether or not another pipeline runs.
    """
    from ...engine.model_cache import ModelSlot, get_model_cache

    device = resolve_device()

    def _load() -> ToposFacts:
        gate = _local_dir(ENV_GATE, GATE_REPO, GATE_REVISION, extra=[SPEC_FILE])
        reader = _local_dir(ENV_READER, READER_REPO, READER_REVISION)
        logger.info("Topos Facts %s loading on %s", VERSION, device)
        return ToposFacts(gate, reader, device)

    idle = idle_seconds()
    handle, _ = get_model_cache().acquire(ModelSlot.TOPOS_FACTS, f"{MODEL_ID}@{device}", _load,
                                          pinned=True, pin_idle_sec=idle)
    _arm_release(idle)
    return handle


def _arm_release(idle: float) -> None:
    """One daemon timer, re-armed on every ``load``: after ``idle`` seconds without a load it
    drops the encoders (``trim_to_budget`` only runs after a pipeline, so without this a
    pinned model would wait for the next ingest to be released)."""
    global _release_timer
    if idle <= 0:
        return
    with _timer_lock:
        if _release_timer is not None:
            _release_timer.cancel()
        _release_timer = threading.Timer(idle, _release_if_idle, args=(idle,))
        _release_timer.daemon = True
        _release_timer.start()


def _release_if_idle(idle: float) -> None:
    from ...engine.model_cache import ModelSlot, get_model_cache

    unused = get_model_cache().idle_for(ModelSlot.TOPOS_FACTS)
    if unused is None:
        return
    if unused + 0.01 >= idle:
        release()
    else:                                  # loaded again since the timer was armed
        _arm_release(idle - unused)


def release() -> None:
    """Drop the resident encoders (the owner picked another provider). A no-op when absent."""
    from ...engine.model_cache import ModelSlot, get_model_cache

    cache = get_model_cache()
    if ModelSlot.TOPOS_FACTS.value in cache.resident_slots():
        cache.evict_slot(ModelSlot.TOPOS_FACTS)
        logger.info("Topos Facts %s released", VERSION)


def routes_to_classifier(pack_id: str, model: Optional[ToposFacts]) -> bool:
    """Does this pack go to Topos Facts? Only when it is on, knows the pack, and the pack
    is not one the owner kept on the LLM."""
    return model is not None and pack_id not in ROUTED_TO_LLM and model.knows(pack_id)


def raw_output(model: ToposFacts, pack_id: str, text: str) -> str:
    """The pack's assertions as the JSON text ``template.parse_output`` reads."""
    return json.dumps({"assertions": model.extract(pack_id, text)}, ensure_ascii=False)
