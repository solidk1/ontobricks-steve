"""The LLM endpoint OntoBricks calls.

One provider shape, one credential, one URL::

    POST {ONTOBRICKS_LLM_BASE_URL}/chat/completions
    Authorization: Bearer {ONTOBRICKS_LLM_API_KEY}
    {"model": "{ONTOBRICKS_LLM_MODEL}", "messages": [...], ...}

That is the whole contract. Any OpenAI-compatible provider works — OpenAI, Azure
OpenAI, vLLM, Ollama, LiteLLM, a Bedrock gateway — and **Databricks is one of
them, not a special case**: Foundation Model APIs serve the OpenAI shape at
``{workspace}/serving-endpoints``, which is the documented ``base_url`` for the
OpenAI SDK, with the serving-endpoint name as the model.

**Nothing is inferred and nothing falls back.** An earlier revision of this file
resolved either an external provider *or* a Databricks preset at
``{host}/serving-endpoints/{model}/invocations``, choosing between them by
whichever happened to be configured. That meant two URL shapes, two credential
sources, and a provider selected as a side effect of unrelated settings — so a
workspace configured for Unity Catalog reads silently became the model provider,
and a typo'd base URL silently changed which model answered. Absent
configuration is now an error that names the variable to set.

The one layering that remains is deliberate and not a fallback:
``ONTOBRICKS_LLM_MODEL`` is the declared default, and a domain may select a
different model from ``ONTOBRICKS_LLM_MODELS``. Both are explicit operator
choices; neither invents a provider or a credential.

See ``.planning/agents/engine_base/SPEC.md`` for the contract and
``tests/eval/datasets/engine_base/`` for the cases that pin it down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from back.core.errors import ValidationError
from back.core.logging import get_logger

logger = get_logger(__name__)

ENV_BASE_URL = "ONTOBRICKS_LLM_BASE_URL"
ENV_API_KEY = "ONTOBRICKS_LLM_API_KEY"
ENV_MODEL = "ONTOBRICKS_LLM_MODEL"
ENV_MODELS = "ONTOBRICKS_LLM_MODELS"
ENV_EMBEDDING_MODEL = "ONTOBRICKS_EMBEDDING_MODEL"

_CHAT_PATH = "/chat/completions"
_EMBEDDINGS_PATH = "/embeddings"


def _env(name: str) -> str:
    """Read *name*, treating whitespace-only as unset."""
    return (os.getenv(name) or "").strip()


def _normalise_base(raw: str) -> str:
    """Strip trailing slashes and a trailing ``/chat/completions``.

    Input normalisation, not a fallback: operators paste the endpoint they were
    handed, which is usually the full completions URL. Appending the path again
    gives ``…/v1/chat/completions/chat/completions`` and a 404 that reads as if
    the provider were broken.
    """
    base = raw.strip().rstrip("/")
    if base.lower().endswith(_CHAT_PATH):
        base = base[: -len(_CHAT_PATH)].rstrip("/")
    return base


@dataclass(frozen=True)
class LLMTarget:
    """A resolved OpenAI-compatible chat-completions endpoint."""

    base_url: str
    api_key: str
    model: str

    # ---------------------------------------------------------------- request

    def completions_url(self) -> str:
        """The URL to POST to. There is only one."""
        return f"{self.base_url}{_CHAT_PATH}"

    def embeddings_url(self) -> str:
        """The OpenAI-compatible embeddings URL on the same provider.

        Sharing ``base_url`` and ``api_key`` with chat is deliberate: an operator
        configures one provider, not two. Only the model differs, because an
        embedding model is a different model.
        """
        return f"{self.base_url}{_EMBEDDINGS_PATH}"

    def headers(self) -> dict[str, str]:
        """Request headers, omitting ``Authorization`` when there is no key.

        A self-hosted provider (Ollama, a bare vLLM) rejects a ``Bearer`` header
        carrying nothing, and against a key-checking provider ``Bearer `` returns
        a 401 that gives no hint the credential was simply absent. Sending no
        header is the correct request for a keyless endpoint.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def describe(self) -> str:
        """A log-safe identification of this target. Never includes the key."""
        return f"{self.base_url} model={self.model}"

    # --------------------------------------------------------------- factories

    @staticmethod
    def is_configured() -> bool:
        """True when a provider is configured. Nothing else implies one."""
        return bool(_env(ENV_BASE_URL))

    @staticmethod
    def embedding_model() -> str:
        """The declared embedding model, or "" when none is configured.

        Separate from ``ONTOBRICKS_LLM_MODEL`` because a chat model cannot serve
        ``/embeddings``. Absent, callers that need embeddings must degrade rather
        than guess a model name — guessing produces a 404 from the provider that
        reads like the endpoint is broken.
        """
        return _env(ENV_EMBEDDING_MODEL)

    @staticmethod
    def for_embeddings() -> "LLMTarget":
        """Resolve the embeddings target, or raise naming what is missing."""
        base = _normalise_base(_env(ENV_BASE_URL))
        model = _env(ENV_EMBEDDING_MODEL)
        missing = [
            name
            for name, value in ((ENV_BASE_URL, base), (ENV_EMBEDDING_MODEL, model))
            if not value
        ]
        if missing:
            raise ValidationError(
                "Embeddings are not configured: set " + " and ".join(missing)
            )
        return LLMTarget(base_url=base, api_key=_env(ENV_API_KEY), model=model)

    @staticmethod
    def models() -> list[str]:
        """Models an operator has declared, for the Domain Settings picker.

        ``ONTOBRICKS_LLM_MODELS`` if given, else the single default. Most
        OpenAI-compatible providers have no listing endpoint worth querying, so
        the set of offered models is declared rather than discovered.
        """
        listed = [m.strip() for m in _env(ENV_MODELS).split(",") if m.strip()]
        if listed:
            return listed
        single = _env(ENV_MODEL)
        return [single] if single else []

    @classmethod
    def from_env(cls, model: str = "") -> LLMTarget:
        """Build the target, raising when it is not fully configured.

        Args:
            model: The domain's selected model. Empty means "use the declared
                default", :data:`ENV_MODEL`.

        Raises:
            ValidationError: naming the variable that is missing. No provider,
                credential or model is ever derived from anything else.
        """
        base = _env(ENV_BASE_URL)
        if not base:
            raise ValidationError(
                f"No LLM provider configured. Set {ENV_BASE_URL} to an "
                "OpenAI-compatible base URL (for Databricks Foundation Model "
                "APIs that is https://<workspace>/serving-endpoints)."
            )
        chosen = (model or "").strip() or _env(ENV_MODEL)
        if not chosen:
            raise ValidationError(
                f"No LLM model configured. Set {ENV_MODEL}, or select one in "
                "Domain Settings."
            )
        target = cls(
            base_url=_normalise_base(base),
            api_key=_env(ENV_API_KEY),
            model=chosen,
        )
        logger.debug("LLM target: %s", target.describe())
        return target
