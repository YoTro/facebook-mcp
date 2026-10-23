"""Lightspeed gateway registry (L2).

The Messenger send path is a WebSocket, so its endpoint, build-specific version
id and task vocabulary are **data** — the same rule as ``doc_id`` in
``routes/graphql.py``: when Facebook ships a new frontend build these values
rotate, and only this table should change.

Re-capture with the *capture* tool (``lightspeed_probe.py`` is a sender, not a
sniffer)::

    .venv/bin/python tools/sniff.py --account <name> --websocket \
        --navigate "https://www.facebook.com/messages/t/<thread-id>/" --seconds 20

Frames land in ``.cache/ws-frames/``; the send is the task-46 frame
(``transport.lightspeed.split_frame`` decodes it).

Layer note: this module holds **data** (endpoint, version id, task markers) and
the URL template. It imports ``transport.lightspeed`` for the wire-level ``APP_ID``
only — a downward dependency (routes is L2, transport L1), not a contract with the
clients or services above it.
"""

from __future__ import annotations

import uuid

from ..transport import lightspeed

GATEWAY = "wss://gateway.facebook.com/ws/lightspeed"

# Sent as ``x-dgw-version``; observed stable across captures.
PROTOCOL_VERSION = 5

# Identifies the client build inside the task envelope. Rotates with the build,
# like a doc_id — re-capture when the gateway starts rejecting sends.
DEFAULT_VERSION_ID = "39529440880004510"

# Task names the gateway answers with, in order of appearance.
ACCEPT_MARKER = "replaceOptimsiticMessage"  # the typo is Facebook's
REJECT_MARKER = "markOptimisticMessageFailed"


def gateway_url(uid: str, *, version: int = PROTOCOL_VERSION) -> str:
    """Build the gateway URL for ``uid`` (auth is cookie-based)."""
    return (
        f"{GATEWAY}?x-dgw-appid={lightspeed.APP_ID}&x-dgw-appversion=0"
        f"&x-dgw-authtype=1:0&x-dgw-version={version}&x-dgw-uuid={uid}"
        f"&x-dgw-tier=prod&x-dgw-loggingid={uuid.uuid4()}"
        f"&x-dgw-regionhint=FTW&x-dgw-deviceid={uuid.uuid4()}"
    )
