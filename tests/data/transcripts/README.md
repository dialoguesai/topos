# Transcript ingest fixtures

YouTube caption archives used as the test corpus for `youtube_transcripts`
→ `transcript.session.v1`. These are **not** a product source.

Captions have **no speaker labels**. That is the hard attribution case:
every segment must land `actor_role=ambient` with no contacts and no
social-graph edges.

## Videos

| File | URL |
|------|-----|
| `NVZwqkxEX6g.archive.json` | https://youtu.be/NVZwqkxEX6g |
| `5B9EjKUFDFs.archive.json` | https://youtu.be/5B9EjKUFDFs |
| `xdXLzFzxA9Q.archive.json` | https://youtu.be/xdXLzFzxA9Q |

## Refresh

The fetch service lives in the sibling `content-transcription` repo
(`ytx.core.process_video`). Cloud Run `canonical-archive` is an optional
HTTP front:

```bash
# Local CLI (from content-transcription)
PYTHONPATH=src:. uv run python -c "
from ytx.core import process_video
import json
from pathlib import Path
out = Path('...')
archive = process_video(url_or_id='https://youtu.be/VIDEO_ID', augment_metadata=True)
(out / 'VIDEO_ID.archive.json').write_text(json.dumps(archive, indent=2))
"

# Deployed (when the backend is up)
curl -sS 'https://canonical-archive-e5vq5p2rgq-uc.a.run.app/api/transcripts' \
  -H 'Content-Type: application/json' \
  -d '{"youtube_url":"https://youtu.be/VIDEO_ID","augment_metadata":true}'
```

Schema: `yt_transcript_archive` v2. Items are `{text, start, duration}`.

The parser stitches adjacent open-sentence lines into utterances **before**
canonical rows exist (`stitch_caption_items`). Co-occurrence is same-record
only, so “generation of OpenAI” + “called Astra” must share one item here.
