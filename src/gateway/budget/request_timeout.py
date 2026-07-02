"""RequestTimeoutGuard — full-link timeout enforcement for gateway processing.

Placed at priority 3, this middleware enforces a ceiling on the total wall-clock
time a request can spend inside the gateway (middleware chain + upstream
forwarding + response processing). If exceeded, the request is aborted with a
504 response and the trace is closed with status=\"timeout\".

Differs from the existing ``upstream_timeout`` (httpx-level) in that it covers
the ENTIRE gateway pathway, including middleware execution that may be blocked
by slow external dependencies (e.g. Presidio PII analysis, custom guardrail
rules with network calls).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from gateway.proxy.middleware import Middleware
from shared.logging import get_logger

if TYPE_CHECKING:
    from shared.models import RequestContext, ResponseContext

logger = get_logger()


class RequestTimeoutGuard(Middleware):
    """Enforce a total timeout on the full request lifecycle inside the gateway.

    This middleware stores the deadline in the request context so
    ProxyEngine can enforce it with ``asyncio.wait_for`` around the
    entire Phase 2-3 processing block.

    Runs at priority=3 — BEFORE security checks (priority=10) so a stuck
    guardrail rule doesn't hang the gateway process.
    """

    priority: int = 3

    def __init__(self, total_timeout_seconds: float = 60.0) -> None:
        """Args:
        total_timeout_seconds: 端到端超时（秒），默认 60s。
        """
        self._timeout = total_timeout_seconds

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        deadline = asyncio.get_running_loop().time() + self._timeout
        ctx.timeout_deadline = deadline
        ctx.timeout_seconds = self._timeout
        return ctx

    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        return ctx
