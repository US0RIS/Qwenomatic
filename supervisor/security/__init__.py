"""Fail-closed execution boundary owned by the supervisor."""

from .network import NetworkBoundaryError, NetworkGuard
from .registry import SecurityRegistry, SecurityScopeError

__all__ = ["NetworkBoundaryError", "NetworkGuard", "SecurityRegistry", "SecurityScopeError"]
