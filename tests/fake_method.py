"""Stand-in method process for dispatch tests. Behaviour is picked by request['native']['mode']."""

from __future__ import annotations

import time

from methods._common import base_metadata, finish, load_request


def run(request: dict) -> dict:
    started = time.monotonic()
    mode = request["native"]["mode"]
    if mode == "sleep":
        time.sleep(30)
    if mode == "crash":
        raise SystemExit(3)
    metadata = base_metadata(request, final_answer="FINAL ANSWER: no", status="completed")
    return finish(request, metadata, started)


if __name__ == "__main__":
    run(load_request())
