"""Tracing setup must not block startup when nothing is configured.

Why this file exists
--------------------
``setup_tracing`` called ``mlflow.set_experiment`` unconditionally. With no
``MLFLOW_TRACKING_URI``, MLflow falls back to a local store and tries to open a
SQLite file — which on a container with a read-only root filesystem fails inside
**MLflow's own** retry loop: roughly 100 seconds of exponential backoff (1.5s,
3.1s, 6.3s … 51s) before the exception reaches the handler that logs a warning and
carries on.

uvicorn does not serve until that returns, so it presented as a slow start. On AKS
it cost seven SIGKILLs: the startup probe could not get a response and the kubelet
killed the container each time.

Skipping is also the correct behaviour rather than a workaround — traces written to
a file inside an ephemeral pod are discarded with the pod, so continuing gained
nothing.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from agents import tracing

pytestmark = pytest.mark.unit


class TestUnconfigured:
    def test_returns_false_without_a_tracking_uri(self, monkeypatch):
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        assert tracing.setup_tracing() is False

    def test_mlflow_is_never_touched(self, monkeypatch):
        """The point: not "handle the failure faster" but "do not start it"."""
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        with patch.dict("sys.modules", {"mlflow": MagicMock()}) as mods:
            tracing.setup_tracing()
            mlflow = mods["mlflow"]
        mlflow.set_experiment.assert_not_called()

    def test_it_is_immediate(self, monkeypatch):
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        start = time.time()
        tracing.setup_tracing()
        assert time.time() - start < 1.0, "startup must not wait on tracing"

    def test_whitespace_is_not_a_destination(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "   ")
        assert tracing.setup_tracing() is False

    def test_the_log_says_how_to_enable_it(self, monkeypatch, caplog):
        """Silently disabled tracing is its own support ticket."""
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        with caplog.at_level("INFO"):
            tracing.setup_tracing()
        assert "MLFLOW_TRACKING_URI" in caplog.text


class TestConfigured:
    def test_a_tracking_uri_still_sets_the_experiment(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
        with patch.dict("sys.modules", {"mlflow": MagicMock()}) as mods:
            ok = tracing.setup_tracing("my_experiment")
            mlflow = mods["mlflow"]
        assert ok is True
        mlflow.set_experiment.assert_called_once()
        mlflow.tracing.enable.assert_called_once()

    def test_a_failure_is_still_survivable(self, monkeypatch):
        """A broken tracking server must not stop the app from serving."""
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://unreachable.invalid")
        broken = MagicMock()
        broken.set_experiment.side_effect = OSError("connection refused")
        with patch.dict("sys.modules", {"mlflow": broken}):
            assert tracing.setup_tracing() is False
