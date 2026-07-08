"""LongMemEval answer judge on local Ollama. JUDGE_PROMPT_VERSION = "lme-judge-1".

``get_anscheck_prompt`` is a VERBATIM mirror of the official LongMemEval
``evaluate_qa.py`` (xiaowu0162/LongMemEval) — template strings copied exactly, one
per task type, plus the abstention template. Do not edit the templates: the judge
prompt is part of the instrument and comparability to the official harness rests on
it. The parse rule is also the official one: ``label = 'yes' in response.lower()``.

Differences from the official harness (deliberate, recorded in the lane manifest):
- The metric model is a LOCAL Ollama model (default qwen3.5:9b-mlx, the validated
  judge) instead of GPT-4o — a comparability threat the runner must report.
- max tokens is slightly above the official 10 (local models spend tokens on
  punctuation/formatting); temperature 0 and the parse rule are identical.

Locality is enforced via topos_eval.protocols.judge.assert_local_endpoint (imported
lazily in __init__: the engine venv does not ship topos_eval; the CP runner env does).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict

JUDGE_PROMPT_VERSION = "lme-judge-1"

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:9b-mlx"  # the validated local judge
# Official harness uses max_tokens=10 with GPT-4o; local models need a little
# headroom to finish "Yes."/"No." cleanly. Parsing is unchanged.
DEFAULT_MAX_TOKENS = 32


def get_anscheck_prompt(
    task: str, question: str, answer: str, response: str, abstention: bool = False
) -> str:
    """Verbatim mirror of official LongMemEval evaluate_qa.get_anscheck_prompt."""
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        prompt = template.format(question, answer, response)
    return prompt


def select_anscheck_prompt(
    question_id: str,
    question_type: str,
    question: str,
    gold_answer: str,
    hypothesis: str,
) -> str:
    """Route to the right template. '_abs' in question_id → abstention template,
    overriding question_type (exactly the official harness's routing rule)."""
    return get_anscheck_prompt(
        question_type,
        question,
        gold_answer,
        hypothesis,
        abstention='_abs' in question_id,
    )


def parse_label(raw: str) -> bool:
    """Official parse rule, exactly: strip, lowercase, substring 'yes'."""
    return 'yes' in raw.strip().lower()


class LMEJudge:
    """LongMemEval yes/no answer judge against a LOCAL Ollama endpoint."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        *,
        timeout_sec: float = 180.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        # Lazy import: prompt construction above stays usable (and unit-testable)
        # without topos_eval installed; a judge INSTANCE always enforces locality.
        from topos_eval.protocols.judge import assert_local_endpoint

        assert_local_endpoint(endpoint)  # refuse non-local — enforced, not trusted
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._timeout = timeout_sec
        self._max_tokens = max_tokens

    def judge_answer(
        self,
        question_id: str,
        question_type: str,
        question: str,
        gold_answer: str,
        hypothesis: str,
    ) -> Dict[str, Any]:
        """Grade one hypothesis. Returns {label: bool, raw: str, model, prompt_version}.

        Judge unavailability raises (loud): a silently-vacuous 'no' would deflate
        benchmark accuracy and corrupt the measurement.
        """
        prompt = select_anscheck_prompt(
            question_id, question_type, question, gold_answer, hypothesis
        )
        raw = self._chat(prompt).strip()
        return {
            "label": parse_label(raw),
            "raw": raw,
            "model": self._model,
            "prompt_version": JUDGE_PROMPT_VERSION,
        }

    # -- internals ------------------------------------------------------------------

    def _chat(self, prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            # Deterministic single-token-ish verdict: temp 0 (official), fixed seed,
            # no chain-of-thought, bounded output. keep_alive avoids reload thrash
            # across the ~500-question sweep.
            "think": False,
            "keep_alive": "10m",
            "options": {"temperature": 0, "seed": 0, "num_predict": self._max_tokens},
        }
        req = urllib.request.Request(
            f"{self._endpoint}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read())
        content = str((body.get("message") or {}).get("content") or "")
        if not content.strip():
            raise RuntimeError(
                f"LME judge returned empty content (model={self._model}); "
                "refusing to score it as 'no'"
            )
        return content

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model
