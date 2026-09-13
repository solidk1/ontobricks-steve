"""The AKS manifests encode constraints that are easy to break silently.

Why this file exists
--------------------
Two of these are data-integrity constraints, not preferences:

* **`replicas: 1`.** APScheduler runs in-process, so a second replica runs a
  second scheduler and every scheduled build happens twice, concurrently, against
  the same graph tables. There is no leader election. `kubectl scale --replicas=3`
  looks like a capacity decision and is actually a corruption bug, and nothing at
  runtime would complain.
* **`strategy: Recreate`.** A `RollingUpdate` briefly runs two pods, which is the
  same state for the duration of every deploy.

* **The workload-identity label.** Without `azure.workload.identity/use: "true"`
  the AKS webhook projects no token, `DefaultAzureCredential` finds nothing, and
  every PostgreSQL connection fails on authentication — at runtime, on a cluster,
  which is an expensive place to discover a missing label.

The rest guard the container contract the image actually needs: it runs as uid
10001 with a read-only root filesystem and writes to `/tmp`, so `/tmp` must be a
writable volume or the app cannot create a session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit

_K8S = Path(__file__).resolve().parents[3] / "deploy" / "azure" / "k8s"


def _load(name: str) -> dict:
    return yaml.safe_load((_K8S / name).read_text())


@pytest.fixture(scope="module")
def deployment() -> dict:
    return _load("deployment.yaml")


@pytest.fixture(scope="module")
def pod_spec(deployment) -> dict:
    return deployment["spec"]["template"]["spec"]


@pytest.fixture(scope="module")
def container(pod_spec) -> dict:
    containers = pod_spec["containers"]
    assert len(containers) == 1, "one container; a sidecar would need its own review"
    return containers[0]


class TestSingleScheduler:
    def test_exactly_one_replica(self, deployment):
        assert deployment["spec"]["replicas"] == 1, (
            "APScheduler runs in-process: a second replica duplicates every "
            "scheduled build against the same tables, with no leader election"
        )

    def test_recreate_strategy(self, deployment):
        assert deployment["spec"]["strategy"]["type"] == "Recreate", (
            "RollingUpdate runs two pods briefly, which is two schedulers"
        )

    def test_no_autoscaler_is_shipped(self):
        """An HPA on this Deployment would be a correctness bug."""
        kinds = {
            doc.get("kind")
            for f in _K8S.glob("*.yaml")
            for doc in yaml.safe_load_all(f.read_text())
            if doc
        }
        assert "HorizontalPodAutoscaler" not in kinds

    def test_the_reason_is_written_down(self):
        """A bare `replicas: 1` invites someone to change it."""
        text = (_K8S / "deployment.yaml").read_text().lower()
        assert "apscheduler" in text and "leader election" in text


class TestWorkloadIdentity:
    def test_pod_is_labelled_for_the_webhook(self, deployment):
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert labels.get("azure.workload.identity/use") == "true"

    def test_pod_uses_the_annotated_service_account(self, pod_spec):
        sa = _load("serviceaccount.yaml")
        assert pod_spec["serviceAccountName"] == sa["metadata"]["name"]

    def test_service_account_carries_a_client_id_annotation(self):
        ann = _load("serviceaccount.yaml")["metadata"]["annotations"]
        assert "azure.workload.identity/client-id" in ann

    def test_no_postgres_password_is_configured(self):
        """Entra auth means no password anywhere. A PGPASSWORD in the ConfigMap
        would be both a leak and a sign the identity path was abandoned."""
        cm = _load("configmap.yaml")["data"]
        assert "PGPASSWORD" not in cm
        assert cm.get("ONTOBRICKS_PG_AUTH") == "entra"


class TestContainerContract:
    def test_writable_tmp_because_root_is_read_only(self, container, pod_spec):
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        assert "/tmp" in mounts, (
            "sessions, logs and scratch files go to /tmp when containerized"
        )
        assert any(v["name"] == "tmp" for v in pod_spec["volumes"])

    def test_runs_as_the_image_uid(self, pod_spec):
        assert pod_spec["securityContext"]["runAsUser"] == 10001
        assert pod_spec["securityContext"]["runAsNonRoot"] is True

    def test_no_privilege_escalation(self, container):
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]

    def test_container_port_matches_the_image(self, container):
        assert container["ports"][0]["containerPort"] == 8000

    def test_service_targets_the_named_port(self, container):
        svc = _load("service.yaml")
        assert svc["spec"]["ports"][0]["targetPort"] == container["ports"][0]["name"]

    def test_service_selector_matches_the_pod(self, deployment):
        svc_selector = _load("service.yaml")["spec"]["selector"]
        pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert svc_selector.items() <= pod_labels.items(), (
            "a selector that matches nothing yields a Service with no endpoints"
        )


class TestProbes:
    @pytest.mark.parametrize("probe", ["startupProbe", "livenessProbe", "readinessProbe"])
    def test_probe_hits_health(self, container, probe):
        assert container[probe]["httpGet"]["path"] == "/health"

    def test_startup_probe_gives_the_app_time(self, container):
        p = container["startupProbe"]
        budget = p["periodSeconds"] * p["failureThreshold"]
        assert budget >= 90, (
            f"only {budget}s to start; the graph stack import is slow and liveness "
            "would restart it into a loop"
        )

    def test_no_cpu_limit(self, container):
        """RDF parsing and reasoning are bursty; throttling turns a slow build
        into a timed-out one. Memory is capped on purpose."""
        limits = container["resources"]["limits"]
        assert "cpu" not in limits
        assert "memory" in limits


class TestSecretsAreNotInTheRepo:
    def test_no_secret_manifest_is_shipped(self):
        kinds = [
            doc.get("kind")
            for f in _K8S.glob("*.yaml")
            for doc in yaml.safe_load_all(f.read_text())
            if doc
        ]
        assert "Secret" not in kinds, (
            "a Secret template invites someone to commit it filled in; the README "
            "shows kubectl create secret instead"
        )

    def test_secret_is_referenced_and_optional(self, container):
        refs = container["envFrom"]
        secret = next(r for r in refs if "secretRef" in r)["secretRef"]
        assert secret["name"] == "ontobricks"
        assert secret["optional"] is True, (
            "a minimal install has no Databricks or LLM credentials and must start"
        )

    def test_configmap_holds_no_obvious_credential(self):
        data = _load("configmap.yaml")["data"]
        for key in data:
            assert not any(
                marker in key.upper() for marker in ("SECRET", "PASSWORD", "TOKEN", "API_KEY")
            ), f"{key} looks like a credential and belongs in the Secret"


class TestKustomization:
    def test_every_manifest_is_included(self):
        kust = _load("kustomization.yaml")
        listed = set(kust["resources"])
        on_disk = {
            f.name
            for f in _K8S.glob("*.yaml")
            if f.name != "kustomization.yaml"
        }
        assert on_disk == listed, (
            f"kustomization.yaml is out of sync: missing {on_disk - listed}, "
            f"stale {listed - on_disk}"
        )

    def test_namespace_is_pinned(self):
        assert _load("kustomization.yaml")["namespace"] == "ontobricks"


class TestChinaOverlay:
    """Azure China is an overlay, not a second copy of the manifests.

    The structural constraints (single replica, Recreate, workload identity,
    probes, security context) are correctness properties of the *application*, not
    of a cloud. Forking the manifests per cloud would mean the next person fixing
    one of those has two places to fix and will find one.
    """

    _CHINA = _K8S / "overlays" / "china"

    def test_the_overlay_builds_on_the_base(self):
        kust = yaml.safe_load((self._CHINA / "kustomization.yaml").read_text())
        assert kust["resources"] == ["../.."], (
            "the overlay must reference the base, not restate it"
        )

    def test_it_patches_rather_than_replaces(self):
        kust = yaml.safe_load((self._CHINA / "kustomization.yaml").read_text())
        assert "patches" in kust
        assert "namespace.yaml" not in str(kust), "no base resource is re-declared"

    def test_no_structural_field_is_overridden(self):
        """Replicas, strategy and the workload-identity label must come from the
        base. Overriding them here is how the constraint gets lost for one cloud."""
        patch = yaml.safe_load((self._CHINA / "deployment-china.yaml").read_text())
        spec = patch["spec"]
        assert "replicas" not in spec
        assert "strategy" not in spec
        labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
        assert "azure.workload.identity/use" not in labels

    def test_postgres_host_is_the_china_domain(self):
        cm = yaml.safe_load((self._CHINA / "configmap-china.yaml").read_text())["data"]
        assert cm["PGHOST"].endswith(".postgres.database.chinacloudapi.cn")

    def test_the_host_drives_the_right_token_audience(self):
        """The whole point of deriving the audience from PGHOST: this overlay sets
        no scope variable, and must still get the China audience."""
        from back.core.postgres.EntraCredential import (
            OSSRDBMS_SCOPE,
            resolve_pg_token_scope,
        )

        cm = yaml.safe_load((self._CHINA / "configmap-china.yaml").read_text())["data"]
        assert "ONTOBRICKS_PG_TOKEN_SCOPE" not in cm
        scope = resolve_pg_token_scope(cm["PGHOST"])
        assert scope != OSSRDBMS_SCOPE
        assert "chinacloudapi.cn" in scope

    def test_the_host_drives_the_right_auth_mode(self, monkeypatch):
        cm = yaml.safe_load((self._CHINA / "configmap-china.yaml").read_text())["data"]
        monkeypatch.setenv("PGHOST", cm["PGHOST"])
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        from back.core.databricks.lakebase.LakebaseAuth import resolve_pg_auth_mode

        assert resolve_pg_auth_mode() == "entra"

    def test_entra_authority_is_the_china_one(self):
        cm = yaml.safe_load((self._CHINA / "configmap-china.yaml").read_text())["data"]
        assert cm["AZURE_AUTHORITY_HOST"] == "https://login.chinacloudapi.cn/"

    def test_registry_is_a_china_registry(self):
        patch = (self._CHINA / "deployment-china.yaml").read_text()
        assert ".azurecr.cn/" in patch
        assert ".azurecr.io/" not in patch

    def test_no_global_cloud_endpoint_is_configured(self):
        """Checks configured *values*, not prose.

        An earlier version grepped the raw file and failed on a comment that read
        "Databricks in China is *.databricks.azure.cn, **not**
        *.azuredatabricks.net" — a comment saying "not X" is exactly what should
        be there, so matching text was the wrong test.
        """
        values: list[str] = []
        for f in self._CHINA.glob("*.yaml"):
            for doc in yaml.safe_load_all(f.read_text()):
                if not doc:
                    continue
                if doc.get("kind") == "ConfigMap":
                    values += [str(v) for v in doc["data"].values()]
                elif doc.get("kind") == "Deployment":
                    for c in doc["spec"]["template"]["spec"]["containers"]:
                        values.append(str(c.get("image", "")))

        assert values, "nothing was inspected; the parse found no values"
        for global_only in (
            "postgres.database.azure.com",
            "login.microsoftonline.com",
            ".azurecr.io/",
            "azuredatabricks.net",
        ):
            offenders = [v for v in values if global_only in v]
            assert not offenders, (
                f"{global_only} is not reachable from Azure China: {offenders}"
            )

    def test_still_ships_no_secret(self):
        kinds = [
            doc.get("kind")
            for f in self._CHINA.glob("*.yaml")
            for doc in yaml.safe_load_all(f.read_text())
            if doc
        ]
        assert "Secret" not in kinds

    def test_the_managed_identity_is_not_in_the_shared_base(self):
        """A base carrying one cloud's client id hands it to every deployment.

        I made exactly this mistake while wiring China: filling the real client
        id into the shared ``serviceaccount.yaml`` meant a global-cloud deploy
        from the same base would have authenticated as the China identity.
        """
        base = yaml.safe_load((_K8S / "serviceaccount.yaml").read_text())
        client_id = base["metadata"]["annotations"]["azure.workload.identity/client-id"]
        assert "REPLACE" in client_id, (
            "the base must keep a placeholder; the identity belongs in an overlay"
        )

    def test_the_overlay_supplies_its_own_identity(self):
        sa = yaml.safe_load((self._CHINA / "serviceaccount-china.yaml").read_text())
        client_id = sa["metadata"]["annotations"]["azure.workload.identity/client-id"]
        assert "REPLACE" not in client_id and len(client_id) == 36, (
            "the China overlay should carry a real client id (a GUID)"
        )

    def test_every_overlay_patch_is_listed(self):
        """A patch file on disk but absent from the patch list is silently
        ignored: the deployment comes up with base values and no error."""
        kust = yaml.safe_load((self._CHINA / "kustomization.yaml").read_text())
        listed = {p["path"] for p in kust["patches"]}
        on_disk = {f.name for f in self._CHINA.glob("*.yaml")} - {"kustomization.yaml"}
        assert listed == on_disk, (
            f"missing {on_disk - listed}, stale {listed - on_disk}"
        )
