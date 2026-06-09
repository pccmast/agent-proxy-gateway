"""Proxy module — transparent HTTP proxy core for the gateway."""

from .middleware import Middleware, MiddlewareChain, BlockException
from .core import ProxyEngine
from .sse import SSEInterceptor

__all__ = [
    "ProxyEngine",
    "SSEInterceptor",
    "Middleware",
    "MiddlewareChain",
    "BlockException",
]
