"""/ws/live — push updates to subscribed clients.

Each connected browser opens a single WS. The poller publishes
snapshot, transition, alarm and event messages onto the EventBus and
we fan them out here.

Auth: cookie is the canonical path (httponly, SameSite=Strict). The
legacy ?token=... query parameter is still accepted for headless
clients but logs a deprecation warning — URLs leak into proxy access
logs and browser history. Prefer the cookie path or a future
one-time-ticket endpoint.

After the initial check at connect time, the session is re-validated
periodically inside the message loop (REVALIDATE_EVERY_S) so a
logout-revoked, disabled or expired session can't keep streaming live
data for the rest of the original session window. Validation goes
through services/session.py, the same path the HTTP routes use — a
session revoked from the Users page stops the live feed too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..services import session as session_svc
from ..services.auth import AuthError

log = logging.getLogger("genwatch.ws")

router = APIRouter(tags=["ws"])

# How often (seconds) the live loop re-decodes the current cookie/token.
# An attacker who captured a JWT keeps read access until natural expiry
# — but with this periodic check the maximum exposure window after a
# logout / config-driven secret rotation drops from session_hours to
# this constant. Shorter is more secure but bursts decode work; 60s is
# the conservative balance.
REVALIDATE_EVERY_S = 60.0


def _current_token(websocket: WebSocket, fallback_query_token: str | None) -> str | None:
    """Pull the live auth material for re-validation.

    The cookie value lives on the WebSocket object and is captured at
    connect time — Starlette does not refresh it mid-stream — so the
    periodic re-validation re-checks the SAME credential. That is enough
    now that sessions are server-side: the recheck sees a revoked `jti`,
    a bumped `token_epoch`, a disabled account, or an idle timeout, not
    just expiry and secret rotation.
    """
    return session_svc.read_cookie(websocket.cookies) or fallback_query_token


def _origin_allowed(websocket: WebSocket) -> tuple[bool, str | None]:
    """Validate the Origin header against an allowlist.

    Returns (allowed, reason). When no Origin is present we accept —
    non-browser WS clients (curl, websocat) don't send one, and on a
    LAN deployment the cookie's SameSite=Strict + auth check is already
    sufficient. Browsers always send Origin; that's the case we gate.

    Allowlist: the request's own host (same-origin WS) + any entries
    in settings.cors_origins. This mirrors the HTTP CORS posture so the
    operator only has to configure trusted origins in one place.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True, None  # non-browser client, allow
    settings = websocket.app.state.settings
    # Same-origin: scheme+host:port of the WS upgrade matches the
    # Origin header. The browser computes Origin from the page that
    # opened the socket, so this is the common-case allow.
    host_header = websocket.headers.get("host", "")
    # WebSocket Upgrade requests carry the Host header, but the WS
    # scheme on the page that opened us is http(s), not ws(s). Accept
    # http(s)://host as same-origin against an Upgrade on the same host.
    same_origin_candidates = {
        f"http://{host_header}",
        f"https://{host_header}",
    }
    if origin in same_origin_candidates:
        return True, None
    cors_list = [o for o in (settings.cors_origins or []) if o]
    if origin in cors_list:
        return True, None
    return False, f"origin {origin!r} not in allowlist"


async def _authed(websocket: WebSocket, token: str | None) -> bool:
    raw = _current_token(websocket, token)
    if not raw:
        return False
    try:
        # touch=False: a socket streaming telemetry into a tab nobody is
        # looking at must not keep the session alive. The Live view is
        # exactly the page people leave open, so counting its WebSocket
        # as activity would quietly disable the idle timeout for the
        # common case. The frontend sends a real keepalive on user input.
        session_svc.validate(
            db=websocket.app.state.db,
            settings=websocket.app.state.settings,
            token=raw,
            touch=False,
        )
        return True
    except AuthError as e:
        log.debug("ws auth rejected: %s", e)
        return False


@router.websocket("/ws/live")
async def live(websocket: WebSocket, token: str | None = Query(None)):
    # Origin allowlist — closes cross-origin WS hijack with SameSite=Lax
    # cookies (the cookie would be sent by the browser, but Origin gives
    # us a second factor independent of cookie policy). Same-origin
    # requests + non-browser clients pass.
    origin_ok, origin_reason = _origin_allowed(websocket)
    if not origin_ok:
        log.warning("ws: rejecting connection — %s", origin_reason)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not await _authed(websocket, token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if token:
        # Deprecation signal — the query-string token leaks into proxy
        # access logs and browser history. Tracked for a future removal
        # once we've confirmed nothing field-deployed depends on it.
        log.warning(
            "ws: client used deprecated ?token= query auth from %s; "
            "switch to the session cookie",
            websocket.client.host if websocket.client else "unknown",
        )

    await websocket.accept()
    bus = websocket.app.state.bus
    q = bus.subscribe()

    # Initial hello — let the client know the WS is live and what the
    # current state is, so it doesn't have to wait for the next poll.
    last_revalidated = time.monotonic()
    try:
        st = websocket.app.state
        snap = st.state_machine.snap
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "state": snap.engine_state,
                    "comms": {
                        "state": snap.comms.state,
                        "successPct": snap.comms.success_pct,
                        "rateMs": snap.comms.rate_ms,
                    },
                    "serverTs": snap.last_reading.ts,
                }
            )
        )

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
                await websocket.send_text(json.dumps(msg))
            except asyncio.TimeoutError:
                # Keep-alive — many proxies drop idle WS at 30-60s.
                await websocket.send_text(json.dumps({"type": "ping"}))

            # Periodic re-validation. Tied to wall-monotonic clock so a
            # quiet WS (only keep-alives) still gets checked at cadence
            # rather than only when a real event arrives.
            now_mono = time.monotonic()
            if now_mono - last_revalidated >= REVALIDATE_EVERY_S:
                if not await _authed(websocket, token):
                    log.info(
                        "ws: re-validation failed (token expired or "
                        "secret rotated); closing connection"
                    )
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                last_revalidated = now_mono
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ws error: %s", e)
    finally:
        bus.unsubscribe(q)
