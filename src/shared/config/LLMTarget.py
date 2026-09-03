"""Resolution of the LLM endpoint an agent should call.

Every OntoBricks agent reached its model through one hardcoded line::

    url = f"{host.rstrip('/')}/serving-endpoints/{endpoint_name}/invocations"

That shape is Databricks-only, and it was the last hard Databricks dependency in
the agent layer.  Databricks serving endpoints already speak the OpenAI
chat-completions request and response shape, so only two things actually differ
between providers: the URL, and whether the model name travels in the body.

:class:`LLMTarget` owns both differences and nothing else.  It is pure
configuration resolution — no I/O, no retries, no tracing — so the transport in
``agents/engine_base.py`` keeps its single responsibility of posting a payload.

**Provenance decides the style, not the hostname.**  Sniffing for
``*.databricks.com`` would break on custom DNS, private workspaces and
proxies, and would silently pick the wrong credential.  Instead: if
``ONTOBRICKS_LLM_BASE_URL`` is set, the caller wants an OpenAI-compatible
provider; otherwise the caller's ``(host, token, endpoint_name)`` triple is a
Databricks workspace.  There is no third knob to get wrong.

See ``.planning/agents/engine_base/SPEC.md`` for the contract this implements
and ``tests/eval/datasets/engine_base/`` for the cases that pin it down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from back.core.errors import ValidationError
from back.core.logging import get_logger

logger = get_logger(__name__)

#: Databricks Foundation Model API — ``{host}/serving-endpoints/{model}/invocations``.
STYLE_DATABRICKS = "databricks"
#: Any OpenAI-compatible provider — ``{base_url}/chat/completions``.
STYLE_OPENAI = "openai"

ENV_BASE_URL = "ONTOBRICKS_LLM_BASE_URL"
ENV_API_KEY = "ONTOBRICKS_LLM_API_KEY"
ENV_MODEL = "ONTOBRICKS_LLM_MODEL"
ENV_MODELS = "ONTOBRICKS_LLM_MODELS"

_CHAT_PATH = "/chat/completions"


def _env(name: str) -> str:
    """Read *name*, treating whitespace-only as unset.

    A blank override must not silently switch providers: an empty
    ``ONTOBRICKS_LLM_BASE_URL`` in a container manifest is a mistake, not a
    request to leave Databricks.
    """
    return (os.getenv(name) or "").strip()


def _normalise_base(raw: str) -> str:
    """Strip whitespace, trailing slashes, and a trailing ``/chat/completions``.

    Users paste the endpoint they were given, which is usually the full
    completions URL. Appending the path again yields
    ``…/v1/chat/completions/chat/completions`` and a 404 that reads as if the
    provider were at fault.
    """
    base = raw.strip().rstrip("/")
    if base.lower().endswith(_CHAT_PATH):
        base = base[: -len(_CHAT_PATH)].rstrip("/")
    return base


@dataclass(frozen=True)
class LLMTarget:
    """Where to POST a chat completion, and in which dialect.

    For :data:`STYLE_DATABRICKS`, ``base_url`` is the workspace host and
    ``model`` is the serving-endpoint name — the endpoint name occupies the
    model slot because that is exactly what it is on the Foundation Model API.
    """

    base_url: str
    api_key: str
    model: str
    api_style: str

    # ---------------------------------------------------------------- request

    def completions_url(self) -> str:
        """The full URL to POST to."""
        if self.api_style == STYLE_DATABRICKS:
            return f"{self.base_url}/serving-endpoints/{self.model}/invocations"
        return f"{self.base_url}{_CHAT_PATH}"

    def headers(self) -> dict[str, str]:
        """Request headers, omitting ``Authorization`` when there is no key.

        Unauthenticated local providers (Ollama, a bare vLLM server) reject a
        ``Bearer`` header with no token, and ``Bearer `` against Databricks
        returns a 403 that gives no hint the token was simply absent.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def payload_extras(self) -> dict[str, str]:
        """Body fields the dialect requires beyond ``messages``.

        The Databricks invocations URL names the endpoint, so ``model`` is
        redundant there; an OpenAI-compatible POST without it is a 400.
        """
        if self.api_style == STYLE_OPENAI:
            return {"model": self.model}
        return {}

    def describe(self) -> str:
        """A log-safe identification of this target. Never includes the key."""
        return f"{self.api_style}:{self.base_url} model={self.model}"

    # --------------------------------------------------------------- factories

    @staticmethod
    def external_configured() -> bool:
        """True when an OpenAI-compatible provider is configured explicitly."""
        return bool(_env(ENV_BASE_URL))

    @classmethod
    def from_env(cls, model_hint: str = "") -> LLMTarget | None:
        """Build an external target from the environment, or ``None`` if unset.

        *model_hint* is the caller's ``endpoint_name``.  Using it as the model
        when :data:`ENV_MODEL` is unset keeps the existing Domain Settings LLM
        dropdown meaningful on an external deployment — it becomes a model
        picker instead of a serving-endpoint picker.
        """
        base = _env(ENV_BASE_URL)
        if not base:
            return None
        model = _env(ENV_MODEL) or (model_hint or "").strip()
        if not model:
            raise ValidationError(
                f"No LLM model configured. Set {ENV_MODEL}, or select one in "
                "Domain Settings."
            )
        return cls(
            base_url=_normalise_base(base),
            api_key=_env(ENV_API_KEY),
            model=model,
            api_style=STYLE_OPENAI,
        )

    @classmethod
    def for_databricks(cls, host: str, token: str, endpoint_name: str) -> LLMTarget:
        """Build the Databricks Foundation Model API preset."""
        clean_host = (host or "").strip().rstrip("/")
        if not clean_host:
            raise ValidationError(
                "Databricks credentials not configured, and no " f"{ENV_BASE_URL} set."
            )
        endpoint = (endpoint_name or "").strip()
        if not endpoint:
            raise ValidationError(
                "No LLM serving endpoint configured. Please set it in Domain "
                "Settings."
            )
        return cls(
            base_url=clean_host,
            api_key=(token or "").strip(),
            model=endpoint,
            api_style=STYLE_DATABRICKS,
        )

    @classmethod
    def resolve(
        cls, host: str = "", token: str = "", endpoint_name: str = ""
    ) -> LLMTarget:
        """Pick a target: an explicit external provider wins over Databricks.

        Databricks may still be fully configured for Unity Catalog reads and
        the Delta engine while the LLM lives elsewhere, so the presence of a
        workspace host must not drag the model call back onto the workspace.
        """
        external = cls.from_env(model_hint=endpoint_name)
        if external is not None:
            logger.debug("LLM target resolved to %s", external.describe())
            return external
        target = cls.for_databricks(host, token, endpoint_name)
        logger.debug("LLM target resolved to %s", target.describe())
        return target

    # ------------------------------------------------------------------- UI

    @staticmethod
    def picker_models() -> list[str]:
        """Models to offer in the Domain Settings dropdown on an external setup.

        Returns ``[]`` on a Databricks deployment, where the dropdown is
        populated from the workspace's serving-endpoints API instead.
        """
        if not LLMTarget.external_configured():
            return []
        listed = [m.strip() for m in _env(ENV_MODELS).split(",") if m.strip()]
        if listed:
            return listed
        single = _env(ENV_MODEL)
        return [single] if single else []
