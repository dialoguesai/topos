"""The model names the app shows for the privacy stages must stay true.

The import screen names the model doing each step, because "checking for
private details" LOOKS like a step that would call a language model and does
not: it is a token-classification (NER) encoder running locally on CPU, with no
API call and no tokens. Telling a user their messages went to an LLM when they
did not is a claim about where their data goes, so it has to be right.

The app cannot import these constants across repos, so it carries the strings.
This is the guard on that copy: change a model here and this fails HERE, naming
the file to update, rather than the app quietly mislabelling the stage.

Update together:
  topos-react-app/next/features/analytics/lib/ingestionStages.ts  (PRIVACY_STAGES)
"""

from __future__ import annotations

from topos.sanitization.nsfw_classifier import NSFW_TAGGER_ID
from topos.sanitization.privacy_filter import PRIVACY_FILTER_MODEL_ID

# What the import screen prints, verbatim. "checking for sensitive content" is no model at all now: the
# explicit-wording rule decides the NSFW tag, so the app names the rule and its version there.
SHOWN_IN_APP = {
    "checking_for_private_details": "openai/privacy-filter",
    "checking_for_sensitive_content": "explicit-wording/v1",
}


def test_the_privacy_filter_model_is_the_one_we_name():
    assert PRIVACY_FILTER_MODEL_ID == SHOWN_IN_APP["checking_for_private_details"]


def test_the_nsfw_tagger_is_the_one_we_name():
    assert NSFW_TAGGER_ID == SHOWN_IN_APP["checking_for_sensitive_content"]


def test_neither_privacy_stage_uses_a_language_model():
    """The claim the app makes is 'on your machine', so this pins the mechanism.

    Both stages run through a transformers pipeline, not a chat completion. If
    one ever routes through the LLM the app's label becomes false, and it should
    fail here first.
    """
    import inspect

    from topos.sanitization import hub_pipeline, nsfw_classifier, privacy_filter

    # Call sites, not names. A first pass matched `sanitization_ollama_max_input_chars`
    # -- a legacy SETTING name reused as the input-length cap, with no LLM behind
    # it. Grepping for the word here would fail on a rename and pass on a real
    # regression, which is backwards.
    llm_calls = (
        "chat_completion(",
        "chat.completions",
        "run_llm(",
        "generate_completion(",
        "ollama.chat(",
        "ollama.generate(",
    )
    # hub_pipeline is where the transformers call lives; the privacy filter reaches it through
    # load_pipeline(). The NSFW stage is a rule: it loads no pipeline and reaches no model of any kind.
    for module in (privacy_filter, hub_pipeline):
        src = inspect.getsource(module)
        assert "pipeline(" in src, f"{module.__name__} no longer loads a transformers pipeline"
    for module in (privacy_filter, nsfw_classifier, hub_pipeline):
        src = inspect.getsource(module)
        for call in llm_calls:
            assert call not in src, f"{module.__name__} now reaches an LLM: {call}"
    assert "pipeline(" not in inspect.getsource(nsfw_classifier)
