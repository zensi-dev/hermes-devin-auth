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

from typing import Any, List, Optional

from providers import register_provider
from providers.base import ProviderProfile

from .client import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, DEVIN_API_URL, DevinClient, list_models

# Shown when the live GetCliModelConfigs fetch fails or no token is configured.
_FALLBACK_MODELS = ("swe-1-6", "swe-1-6-slow")


class DevinProviderProfile(ProviderProfile):
    """Devin Cascade — Connect/protobuf wire, OAuth session-token auth."""

    def create_client(self, **client_kwargs: Any):
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
