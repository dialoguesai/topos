"""OFF2N (kept questions Wave 2, scope §10): the owner's chat under a background backlog, with the yield rule on and off.

A stubbed model host with one slot, first come first served and no preemption (the local model host queues per model
in arrival order: a call already sent cannot be overtaken). Against it:

- background workers (the node's catch-up and assessment lanes) send model calls back to back, each marked
  background work and passing ``answer_gate.yield_to_answers_if_background`` before it is sent, as the node's model
  clients do;
- the owner's ``llm_generation`` frames, signed ``owner_app`` as the control plane will stamp them, arrive at a steady
  interval through the real control-plane client (the owner lane classifies and counts them). Each makes
  ``owner_calls`` model calls in a row, as a home-chat turn does (plan, tools, answer), each taking its turn.

Neither setting can let the owner overtake a background call already sent (in the host's queue or running): that is
the first call's wait either way. The rule's effect is between the owner's own calls: off, a background lane that
finished re-queues at once and the owner's next call queues behind it; on, it waits until the owner's frame ends.

For each setting of ``TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER`` it reports the owner frames' time waiting
for the model (summed over a frame's calls) at p50 and p95, their end-to-end time, and the background calls
completed per second. The model's times are sleeps, so the machine's load moves every number; read the ratios between the two
settings, not the seconds. Nothing is called but the stub; keys are made at run time; every id is invented.

    TOPOS_KEY=synthetic TOPOS_DATABASE_PATH=<scratch>/db.sqlite TOPOS_ENV_FILE=<scratch>/env \\
        .venv/bin/python3 scripts/permissions_v2/owner_chat_backlog_harness.py --frames 20 --repeats 2
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _signer():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    os.environ["TOPOS_CP_STAMP_PUBKEY"] = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    return key


def _stamped(key, message: dict, owner: str) -> dict:
    from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload
    now = time.time()
    stamp = {"v": 1, "cls": "owner_app", "client_id": "topos_home_chat", "acting_user": owner, "iat": now,
             "exp": now + 100}
    stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
    return {**message, STAMP_FIELD: stamp}


class _Socket:
    async def send(self, message) -> None:
        return None


def _percentile(values: list, share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(round(share * (len(ordered) - 1)))))]


async def run_one(*, yields: bool, frames: int, interval: float, owner_call: float, background_call: float,
                  workers: int, owner_calls: int = 3, poll: float = 0.02) -> dict:
    """One setting: `frames` owner generations every `interval` seconds while `workers` background lanes keep the
    model busy. Times in milliseconds."""
    from topos.control_plane_client import ControlPlaneClient
    from topos.permissions_v2 import answer_gate, away_config

    os.environ["TOPOS_PERMISSIONS_V2_OWNER_LANE"] = "true"
    os.environ["TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER"] = "true" if yields else "false"
    away_config.apply(away_config.DEFAULT)
    answer_gate.POLL_SECONDS = poll
    key = _signer()
    model = asyncio.Lock()            # one slot; asyncio's lock hands it out in arrival order
    waits, totals, done = [], [], [0]
    arrived: dict = {}

    async def generation(message):
        waited = 0.0
        for _ in range(owner_calls):
            asked = time.monotonic()
            async with model:
                waited += time.monotonic() - asked
                await asyncio.sleep(owner_call)
        waits.append(waited * 1000)
        totals.append((time.monotonic() - arrived[message["id"]]) * 1000)
        return {"id": message["id"], "status": "ok", "payload": {"output": "an invented reply"}}

    client = ControlPlaneClient("ws://example/ws/engine", "an-invented-key", generation, False)
    assert client._owner_lane is not None
    socket = _Socket()
    client._ws = socket
    stop = asyncio.Event()

    async def background_lane():
        with answer_gate.background_work():
            while not stop.is_set():
                await answer_gate.yield_to_answers_if_background(poll=poll)
                async with model:
                    await asyncio.sleep(background_call)
                done[0] += 1

    lanes = [asyncio.create_task(background_lane()) for _ in range(workers)]
    await asyncio.sleep(background_call * 2)          # the backlog is under way before the owner starts chatting
    started = time.monotonic()
    for number in range(frames):
        message = _stamped(key, {"id": f"gen-{number}", "type": "llm_generation",
                                 "payload": {"prompt": "an invented question", "model": "an-invented-model"}},
                           "an-invented-owner")
        arrived[message["id"]] = time.monotonic()
        await client._schedule_inbound_message(socket, message)
        await asyncio.sleep(interval)
    while len(totals) < frames:
        await asyncio.sleep(0.01)
    elapsed = time.monotonic() - started
    stop.set()
    await asyncio.gather(*lanes)
    return {"yield_rule": "on" if yields else "off", "owner_frames": frames,
            "owner_wait_ms": {"p50": round(statistics.median(waits)), "p95": round(_percentile(waits, 0.95))},
            "owner_total_ms": {"p50": round(statistics.median(totals)), "p95": round(_percentile(totals, 0.95))},
            "background_calls_per_s": round(done[0] / elapsed, 2), "seconds": round(elapsed, 1)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--frames", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between owner generations")
    parser.add_argument("--owner-call", type=float, default=0.3, help="model seconds per owner model call")
    parser.add_argument("--owner-calls", type=int, default=3, help="model calls in one owner turn")
    parser.add_argument("--background-call", type=float, default=0.5, help="model seconds per background call")
    parser.add_argument("--workers", type=int, default=2, help="background lanes sending model calls")
    args = parser.parse_args(argv)
    import topos
    rows = []
    load_before = os.getloadavg()
    for _ in range(args.repeats):
        for yields in (False, True):
            rows.append(asyncio.run(run_one(yields=yields, frames=args.frames, interval=args.interval,
                                            owner_call=args.owner_call, background_call=args.background_call,
                                            workers=args.workers, owner_calls=args.owner_calls)))
    print(json.dumps({"tree": str(Path(topos.__file__).resolve().parents[1]), "load_average_before": load_before,
                      "load_average_after": os.getloadavg(), "settings": vars(args), "runs": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
