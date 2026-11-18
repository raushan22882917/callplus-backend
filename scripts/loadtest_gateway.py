"""Probe how many simultaneous calls the realtime gateway will hold.

It opens N WebSocket connections to /ws/twilio, speaks the Twilio Media Streams opening
("connected", "start") and streams 20 ms frames of μ-law silence on each, for as long as you ask.
It counts connections that were accepted, turned away (503), or dropped, and prints the gateway's
/statz numbers and this machine's own limits. It does not place real calls and it does not use any
voice provider, so it measures the gateway's sockets, memory and CPU, not the AI turn path.

    uv run python scripts/loadtest_gateway.py ws://localhost:9000 --calls 1000 --hold 60 --ramp 20

Run it against a local or staging gateway, never production. The call ids are made up and carry no
signature, so start that gateway with ALLOW_UNSIGNED_STREAMS=true (otherwise every stream is closed
as soon as it starts and shows up as "dropped") and a stub BACKEND_BASE_URL, since it will fail to
fetch a call config from the API. Raise `ulimit -n` first: each call is one open file.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import resource
import time
import urllib.request

import websockets

SILENCE = base64.b64encode(b"\xff" * 160).decode()  # 20 ms of 8 kHz μ-law silence


async def one_call(i: int, url: str, hold: float, stats: dict) -> None:
    try:
        async with websockets.connect(url + "/ws/twilio", open_timeout=15, max_size=2**20) as ws:
            stats["accepted"] += 1
            stats["live"] += 1
            stats["peak"] = max(stats["peak"], stats["live"])
            sid = f"LOAD{i:06d}"
            await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
            await ws.send(json.dumps({"event": "start", "streamSid": sid, "start": {
                "streamSid": sid, "callSid": sid, "customParameters": {"callId": sid},
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
            end = time.monotonic() + hold
            n = 0
            try:
                while time.monotonic() < end:
                    n += 1
                    await ws.send(json.dumps({"event": "media", "streamSid": sid,
                                              "media": {"payload": SILENCE, "track": "inbound", "chunk": str(n)}}))
                    await asyncio.sleep(0.02)
            finally:
                stats["live"] -= 1
            stats["completed"] += 1
    except websockets.exceptions.InvalidStatus as e:
        stats["denied" if e.response.status_code == 503 else "failed"] += 1
    except websockets.exceptions.ConnectionClosed:
        stats["dropped"] += 1
    except Exception as e:  # noqa: BLE001 - a probe: count it and carry on
        stats["failed"] += 1
        stats.setdefault("errors", {}).setdefault(type(e).__name__, 0)
        stats["errors"][type(e).__name__] += 1


def statz(url: str) -> str:
    http = url.replace("ws://", "http://").replace("wss://", "https://") + "/statz"
    try:
        return urllib.request.urlopen(http, timeout=3).read().decode()
    except Exception as e:  # noqa: BLE001
        return f"unavailable ({type(e).__name__})"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="gateway address, e.g. ws://localhost:9000")
    ap.add_argument("--calls", type=int, default=100)
    ap.add_argument("--hold", type=float, default=30, help="seconds each call stays up")
    ap.add_argument("--ramp", type=float, default=20, help="new calls per second")
    a = ap.parse_args()
    if "callplus.autobot360.com" in a.url:
        raise SystemExit("Refusing to load-test the production address. Use a local or staging gateway.")
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < a.calls + 100:
        print(f"warning: open-file limit is {soft}; run `ulimit -n {a.calls + 500}` first")
    stats = {"accepted": 0, "denied": 0, "failed": 0, "dropped": 0, "completed": 0, "live": 0, "peak": 0}
    t0 = time.monotonic()
    tasks = []
    for i in range(a.calls):
        tasks.append(asyncio.create_task(one_call(i, a.url.rstrip("/"), a.hold, stats)))
        await asyncio.sleep(1 / a.ramp)
        if i and i % 100 == 0:
            print(f"  {i} started, live {stats['live']}, gateway {statz(a.url)}")
    await asyncio.gather(*tasks)
    print(json.dumps({**stats, "seconds": round(time.monotonic() - t0, 1), "gateway": statz(a.url)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
