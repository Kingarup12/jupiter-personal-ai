"""Request-local identity and credential access without process-global secrets."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator


CredentialResolver = Callable[[str], str | None]
CredentialStatusRecorder = Callable[[str, str], None]


@dataclass(frozen=True)
class JupiterRequestContext:
    user_id: str | None = None
    conversation_id: str | None = None
    job_id: str | None = None
    privacy_class: str = "NORMAL_USER_DATA"
    no_persist: bool = False
    credential_resolver: CredentialResolver | None = None
    credential_status_recorder: CredentialStatusRecorder | None = None
    authorized_role: str = "user"
    managed_google_credentials: bool = False
    low_latency_conversation: bool = False
    # Non-secret route observability labels (e.g. "live_degraded" + an allowlisted fallback reason).
    generation_mode: str = ""
    live_fallback_reason: str = ""


_CURRENT: ContextVar[JupiterRequestContext] = ContextVar(
    "jupiter_request_context", default=JupiterRequestContext()
)


def current_request_context() -> JupiterRequestContext:
    return _CURRENT.get()


@contextmanager
def request_context(context: JupiterRequestContext) -> Iterator[JupiterRequestContext]:
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def resolve_request_credential(scope: str) -> str | None:
    resolver = current_request_context().credential_resolver
    saved = resolver(scope) if resolver is not None else None
    if saved or scope != "USER_GOOGLE":
        return saved
    policy = _account_google_policy()
    return policy.secret() if policy is not None else None


def _account_google_policy():
    """A preconfigured personal key belongs to its exact server-side account,
    never to anyone who merely selects Owner/Testing view or role."""
    context = current_request_context()
    if not context.user_id:
        return None
    from config.account_policy import account_policy
    from config.credential_policy import managed_google_policy
    for credential_id in account_policy(context.user_id).personal_google_credential_ids:
        policy = managed_google_policy(credential_id)
        if policy is not None and policy.enabled:
            return policy
    return None


def request_google_quota_group() -> str:
    """The same configured key/project must not gain a second personal pool."""
    context = current_request_context()
    policy = _account_google_policy()
    if policy is not None and resolve_request_credential("USER_GOOGLE") == policy.secret():
        return policy.quota_group_key()
    return f"user-google:{str(context.user_id or 'unknown').casefold()}"
