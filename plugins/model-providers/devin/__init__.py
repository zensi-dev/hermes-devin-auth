"""Devin model provider for Hermes.

Registers a ``devin`` provider that talks directly to the Devin Cascade API
(``server.codeium.com``) over Connect-protocol protobuf. No Devin CLI, no ACP
subprocess.

Credentials resolve through the standard api_key path: ``DEVIN_API_KEY`` env
var first, then the credential pool entry written by ``hermes devin login``
(the companion ``devin`` plugin provides that command).

Model-provider plugins are imported by ``providers/__init__.py`` for their
module-level ``register_provider(...)`` side effect — there is no ``ctx``.
Keep imports light here; ``httpx``/``openai`` load lazily inside the client.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

logger = logging.getLogger(__name__)

from typing import Any, List, Optional

from providers import register_provider
from providers.base import ProviderProfile

from .client import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, DEVIN_API_URL, DevinClient, list_models

# Shown when the live GetCliModelConfigs fetch fails or no token is configured.
_FALLBACK_MODELS = ("swe-1-6", "swe-1-6-slow")


class DevinProviderProfile(ProviderProfile):
    """Devin Cascade — Connect/protobuf wire, OAuth session-token auth."""

    def create_client(self, **client_kwargs: Any):
        # Retry the aux-route patch here too: at plugin-import time
        # ``agent.auxiliary_client`` can be mid-import (cycle) and the first
        # install attempt no-ops. create_client runs before any aux call can
        # succeed, so this is the last safe install point.
        _install_auxiliary_route()
        api_key = (client_kwargs.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError(
                "Devin credentials not found. Run `hermes devin login` or set DEVIN_API_KEY."
            )
        return DevinClient(
            api_key=api_key,
            base_url=client_kwargs.get("base_url") or DEVIN_API_URL,
            timeout=client_kwargs.get("timeout") or 600.0,
        )

    def fetch_models(self, *, api_key: Optional[str] = None,
                     base_url: Optional[str] = None, timeout: float = 8.0):
        if not api_key:
            return None
        try:
            return list_models(api_key, base_url or DEVIN_API_URL, timeout)
        except Exception:
            return None

    def build_extra_body(self, *, session_id: Optional[str] = None, **context: Any):
        # Thread the Hermes session id through as the Cascade conversation id so
        # server-side prompt caching sees a stable thread across turns.
        if session_id:
            return {"devin_cascade_id": f"hermes-{session_id}"}
        return {}

    def supported_reasoning_efforts(self, model: Optional[str]):
        # Effort is baked into the model uid (swe-1-6 vs swe-1-6-slow); no
        # reasoning params are sent on the wire.
        return ()

class _AsyncDevinClient:
    """Async facade for Hermes' async auxiliary path.

    ``async_call_llm`` awaits ``chat.completions.create()``; forcing
    ``stream=False`` means the await yields a complete response, which the aux
    aggregator accepts via its ``hasattr(chunks, "choices")`` shim check.
    """

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, sync_client: DevinClient):
        self._sync = sync_client
        self.api_key = sync_client.api_key
        self.base_url = sync_client.base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any):
        import asyncio
        kwargs["stream"] = False
        return await asyncio.to_thread(self._sync.chat.completions.create, **kwargs)

    def close(self) -> None:
        self._sync.close()


def _install_auxiliary_route() -> None:
    """Route auxiliary (title/compression/vision/…) calls for ``devin`` through
    :class:`DevinClient`.

    ``agent.auxiliary_client._resolve_api_key_branch`` hardcodes a plain
    ``openai.OpenAI`` client for ``api_key`` providers — it never consults
    ``ProviderProfile.create_client`` (only ``external_process`` providers get
    that). Unpatched, every aux call POSTs ``server.codeium.com/chat/completions``
    and the Cascade ingress answers ``default backend - 404``.

    Idempotent and fail-open: if Hermes internals shift, the original branch
    runs and aux degrades to the pre-patch behaviour rather than breaking.
    """
    try:
        import agent.auxiliary_client as aux
    except Exception:
        return
    original = getattr(aux, "_resolve_api_key_branch", None)
    if original is None or getattr(original, "_devin_patched", False):
        return

    def _resolve(req: Any, pconfig: Any, resolve_creds: Any):
        if getattr(req, "provider", "") != "devin":
            return original(req, pconfig, resolve_creds)
        try:
            creds = resolve_creds("devin") or {}
        except Exception:
            creds = {}
        api_key = str(getattr(req, "explicit_api_key", None) or creds.get("api_key") or "").strip()
        if not api_key:
            return None, None
        base_url = str(
            getattr(req, "explicit_base_url", None) or creds.get("base_url") or DEVIN_API_URL
        ).strip().rstrip("/")
        model = getattr(req, "model", None)
        try:
            model = aux._normalize_resolved_model(model, "devin")
        except Exception:
            pass
        client: Any = DevinClient(api_key=api_key, base_url=base_url)
        if getattr(req, "async_mode", False):
            client = _AsyncDevinClient(client)
        return client, model

    _resolve._devin_patched = True  # type: ignore[attr-defined]
    aux._resolve_api_key_branch = _resolve


try:
    _install_auxiliary_route()
except Exception:  # never let routing setup break provider registration
    logger.debug("devin: auxiliary route patch skipped", exc_info=True)


register_provider(DevinProviderProfile(
    name="devin",
    env_vars=("DEVIN_API_KEY",),
    base_url=DEVIN_API_URL,
    display_name="Devin",
    description="Devin (SWE models) via the Cascade API — sign in with `hermes devin login`",
    signup_url="https://app.devin.ai",
    auth_type="api_key",
    api_mode="chat_completions",
    default_max_tokens=DEFAULT_MAX_TOKENS,
    supports_vision=True,
    supports_health_check=False,  # no REST /models; fetch_models uses the proto RPC
    fallback_models=_FALLBACK_MODELS,
))
