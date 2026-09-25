"""Load a transformers pipeline from the local Hub cache first, the Hub second.

A warm cache is not an offline load. ``pipeline(model="openai/privacy-filter")``
with every file already on disk still makes seven Hub requests (measured
2026-09-25, transformers 5.10.4 / huggingface_hub 1.18.0): four HEADs for etags,
each with a 10 s timeout, and three GETs with none. The tokenizer loader lists
``additional_chat_templates/`` through ``HfApi.list_repo_tree``, whose
``paginate()`` calls ``session.get`` with no timeout on an httpx client built
with ``timeout=None``; ``HF_HUB_ETAG_TIMEOUT`` and ``HF_HUB_DOWNLOAD_TIMEOUT``
do not reach it.

That is a hang waiting for a network flap, and on 2026-09-25 it got one. The
Mac's address changed seconds after boot, the tree GET's socket was orphaned on
the old address, and the prewarm worker sat in a blocking ``read()`` for hours.
The tray said "preparing models (0 of 2)", and because ``ModelCache.acquire``
marks the slot as loading, every later caller -- ingestion PII redaction, the
disclose API -- waited on it with no deadline. A green healthcheck and no
privacy filter.

So the tokenizer and model are built with ``local_files_only=True`` first and
handed to ``pipeline()`` as objects, which leaves it nothing to resolve: zero
requests, same 1-3 s. A cache that lacks a file the loader wants -- cold, or an
older download that a newer transformers asks more of -- fails that attempt at
once with ``OSError`` and without a request, and only then does the load go
through the Hub, the one case where the download progress the tray shows is
real. No cache-completeness heuristic is consulted: the loader is the judge of
what it needs, and the answer costs nothing to ask.

The obvious one-liner, ``pipeline(..., local_files_only=True)``, is refused:
transformers 5.10 forwards the flag to its loaders but also leaves it in the
kwargs it passes to the pipeline class, and
``TokenClassificationPipeline._sanitize_parameters`` rejects it.

One more Hub caller hides behind every guard: a ``.bin`` checkpoint (the NSFW
classifier ships one) makes transformers start a fire-and-forget thread that
asks the Hub whether a safetensors conversion PR exists -- four GETs, no
timeout, ``local_files_only`` ignored. Its only switch is the
``DISABLE_SAFETENSORS_CONVERSION`` environment variable, which
``topos.config.settings`` sets for the whole process beside the other Hub
defaults.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("topos.sanitization.hub_pipeline")


def load_pipeline(task: str, model_id: str, *, model_class: str, **pipeline_kwargs: Any):
    """``pipeline(task, model=model_id, **pipeline_kwargs)``, from the cache whenever it can be.

    ``model_class`` names the transformers auto class for the task's head
    (``AutoModelForTokenClassification``, ...): a name rather than a class, so
    nothing from transformers is imported until a load actually happens and the
    loaders stay importable on a node without the ML extras.
    """
    import transformers

    head = getattr(transformers, model_class)
    try:
        tokenizer = transformers.AutoTokenizer.from_pretrained(model_id, local_files_only=True)
        model = head.from_pretrained(model_id, local_files_only=True)
    except OSError:
        # transformers raises OSError, and only OSError, for a wanted file the
        # cache does not hold. Anything else is not a cache miss: a download would
        # not fix it, and a retry through the Hub would reopen the hang.
        logger.info("%s: not in the local cache, loading from the Hub", model_id)
        logger.debug("%s: offline attempt refused", model_id, exc_info=True)
        return transformers.pipeline(task, model=model_id, **pipeline_kwargs)
    logger.info("%s: loaded from the local cache, no Hub requests", model_id)
    return transformers.pipeline(task, model=model, tokenizer=tokenizer, **pipeline_kwargs)
