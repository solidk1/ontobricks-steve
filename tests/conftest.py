"""Pytest configuration and shared fixtures for OntoBricks."""

import importlib.util
import os
import warnings
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def pytest_configure(config):
    """Runs before collection; filters noisy third-party warnings."""
    config.addinivalue_line(
        "markers",
        "allow_cli_resolve: allow real DatabricksAuth._resolve_cli_config in test",
    )
    config.addinivalue_line(
        "markers",
        "allow_network: permit outbound sockets (see the no_network fixture)",
    )
    try:
        from urllib3.exceptions import NotOpenSSLWarning

        warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
    except ImportError:
        pass
    warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch):
    """Set up test environment variables."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://test.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("DATABRICKS_SQL_WAREHOUSE_ID", "test-warehouse")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    # auth_enabled() now defaults to True (fail closed). The suite was
    # written against the previous "off unless in Apps" behaviour, so pin it
    # off here; the tests that care about enforcement set it themselves.
    monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "false")
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    # Force CloudFetch off in tests so probes that build SQL connection
    # params do not trigger live ``databricks.sql.connect`` calls with
    # ``use_cloud_fetch=True`` against the unreachable test host.
    monkeypatch.setenv("DATABRICKS_DISABLE_CLOUD_FETCH", "1")
    monkeypatch.delenv("DATABRICKS_FORCE_CLOUD_FETCH", raising=False)
    monkeypatch.setenv("CSRF_DISABLED", "1")
    # Prevent a developer's .env Lakebase coordinates from leaking into unit
    # tests — otherwise RegistryCfg / graph-engine probes hit a real (or
    # mis-matched) workspace and block the suite.
    for _lakebase_var in (
        "LAKEBASE_PROJECT",
        "LAKEBASE_BRANCH",
        "LAKEBASE_DATABASE",
        "PGHOST",
        "PGDATABASE",
    ):
        monkeypatch.delenv(_lakebase_var, raising=False)


@pytest.fixture(autouse=True)
def isolate_databricks_cli_auth(request):
    """Prevent accidental ``~/.databrickscfg`` resolution in unit tests."""
    if request.node.get_closest_marker("allow_cli_resolve"):
        yield
        return
    from back.core.databricks.DatabricksAuth import DatabricksAuth

    with patch.object(DatabricksAuth, "_resolve_cli_config", return_value=None):
        yield


@pytest.fixture
def client():
    """Create FastAPI test client."""
    from shared.fastapi.main import app

    return TestClient(app)


@pytest.fixture
def mock_session_mgr():
    """Dict-backed session manager for DomainSession tests."""
    store = {}

    class _Mgr:
        def get(self, key, default=None):
            return store.get(key, default)

        def set(self, key, value):
            store[key] = value

        def delete(self, key):
            store.pop(key, None)

    return _Mgr()


@pytest.fixture
def domain_session(mock_session_mgr):
    """Create a DomainSession with a mock session manager."""
    from back.objects.session.DomainSession import DomainSession

    return DomainSession(mock_session_mgr)


@pytest.fixture
def sample_ontology_config():
    """Sample ontology configuration for testing."""
    return {
        "name": "TestOntology",
        "base_uri": "http://test.org/ontology#",
        "description": "Test ontology",
        "classes": [
            {
                "uri": "http://test.org/ontology#Customer",
                "name": "Customer",
                "label": "Customer",
                "comment": "A customer entity",
                "emoji": "👤",
                "parent": "",
                "dataProperties": [
                    {
                        "name": "firstName",
                        "localName": "firstName",
                        "label": "First Name",
                    },
                    {"name": "lastName", "localName": "lastName", "label": "Last Name"},
                ],
            },
            {
                "uri": "http://test.org/ontology#Order",
                "name": "Order",
                "label": "Order",
                "comment": "A sales order",
                "emoji": "📦",
                "parent": "",
                "dataProperties": [
                    {
                        "name": "orderDate",
                        "localName": "orderDate",
                        "label": "Order Date",
                    },
                ],
            },
        ],
        "properties": [
            {
                "uri": "http://test.org/ontology#hasOrder",
                "name": "hasOrder",
                "label": "has Order",
                "comment": "Links customer to order",
                "type": "ObjectProperty",
                "domain": "Customer",
                "range": "Order",
            },
        ],
        "constraints": [],
        "swrl_rules": [],
        "axioms": [],
        "expressions": [],
    }


@pytest.fixture
def sample_mapping_config():
    """Sample mapping configuration for testing."""
    return {
        "entities": [
            {
                "ontology_class": "http://test.org/ontology#Customer",
                "ontology_class_label": "Customer",
                "sql_query": "SELECT * FROM catalog.schema.customers",
                "id_column": "customer_id",
                "label_column": "name",
                "catalog": "catalog",
                "schema": "schema",
                "table": "customers",
                "attribute_mappings": {
                    "firstName": "first_name",
                    "lastName": "last_name",
                },
            },
            {
                "ontology_class": "http://test.org/ontology#Order",
                "ontology_class_label": "Order",
                "sql_query": "SELECT * FROM catalog.schema.orders",
                "id_column": "order_id",
                "label_column": "order_name",
                "catalog": "catalog",
                "schema": "schema",
                "table": "orders",
                "attribute_mappings": {"orderDate": "order_date"},
            },
        ],
        "relationships": [
            {
                "property": "http://test.org/ontology#hasOrder",
                "property_label": "hasOrder",
                "sql_query": "SELECT c.customer_id, o.order_id FROM customers c JOIN orders o ON c.id = o.customer_id",
                "source_class": "http://test.org/ontology#Customer",
                "source_class_label": "Customer",
                "target_class": "http://test.org/ontology#Order",
                "target_class_label": "Order",
                "source_id_column": "customer_id",
                "target_id_column": "order_id",
                "direction": "forward",
            },
        ],
    }


@pytest.fixture
def sample_owl_content():
    """Minimal valid OWL Turtle content for testing."""
    return """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix : <http://test.org/ontology#> .

<http://test.org/ontology> a owl:Ontology ;
    rdfs:label "TestOntology" .

:Customer a owl:Class ;
    rdfs:label "Customer" ;
    rdfs:comment "A customer entity" .

:Order a owl:Class ;
    rdfs:label "Order" ;
    rdfs:comment "A sales order" .

:Product a owl:Class ;
    rdfs:label "Product" ;
    rdfs:subClassOf :Order .

:hasOrder a owl:ObjectProperty ;
    rdfs:label "has Order" ;
    rdfs:domain :Customer ;
    rdfs:range :Order .

:firstName a owl:DatatypeProperty ;
    rdfs:label "firstName" ;
    rdfs:domain :Customer ;
    rdfs:range xsd:string .

:lastName a owl:DatatypeProperty ;
    rdfs:label "lastName" ;
    rdfs:domain :Customer ;
    rdfs:range xsd:string .

:orderDate a owl:DatatypeProperty ;
    rdfs:label "orderDate" ;
    rdfs:domain :Order ;
    rdfs:range xsd:string .
"""


@pytest.fixture
def mock_databricks_client():
    """Create a mock DatabricksClient."""
    client = MagicMock()
    client.host = "https://test.databricks.com"
    client.token = "test-token"
    client.warehouse_id = "test-warehouse"
    client.has_sp_credentials = False
    client.has_valid_auth.return_value = True
    client.test_connection.return_value = (
        True,
        "Connection successful (Personal Access Token)",
    )
    client.get_catalogs.return_value = ["catalog1", "catalog2"]
    client.get_schemas.return_value = ["schema1", "schema2"]
    client.get_tables.return_value = ["table1", "table2"]
    client.get_table_columns.return_value = [
        {"name": "id", "type": "int", "comment": "Primary key"},
        {"name": "name", "type": "string", "comment": "Name field"},
    ]
    return client


def pytest_collection_modifyitems(config, items):
    """Skip Playwright e2e tests when the optional dev dependency is not installed."""
    if importlib.util.find_spec("playwright") is not None:
        return
    skip_e2e = pytest.mark.skip(
        reason="playwright not installed; for e2e: pip install playwright && playwright install chromium",
    )
    for item in items:
        if "tests/e2e" in str(item.path).replace("\\", "/"):
            item.add_marker(skip_e2e)


# ---------------------------------------------------------------------
# No outbound network in unit tests
# ---------------------------------------------------------------------

#: Markers whose tests legitimately talk to something remote.
_NETWORK_MARKERS = frozenset(
    {"db", "external", "live_integration", "e2e", "eval", "spark", "allow_network"}
)


def _is_local(address) -> bool:
    """True for loopback / unix-socket destinations."""
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX and friends
    host = str(address[0])
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "") or host.startswith(
        "127."
    )


@pytest.fixture(autouse=True)
def no_network(request):
    """Fail fast instead of dialing out from a unit test.

    Three health tests used to be deselected because they built a real
    ``DatabricksClient`` and blocked for minutes against an unreachable host —
    the suite could not run clean without ``--deselect``. Rather than mock each
    call site, this makes the *category* of mistake impossible: a unit test that
    reaches the network raises immediately, naming itself.

    Loopback is allowed so ``TestClient`` and testcontainers still work. Tests
    marked ``db`` / ``external`` / ``live_integration`` / ``e2e`` / ``eval`` /
    ``spark``, or explicitly ``allow_network``, are exempt.
    """
    import socket

    if _NETWORK_MARKERS & {m.name for m in request.node.iter_markers()}:
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _blocked(self, address, *a, **kw):
        if _is_local(address):
            return real_connect(self, address, *a, **kw)
        raise RuntimeError(
            f"{request.node.nodeid} attempted an outbound connection to "
            f"{address!r}. Unit tests must not use the network — mock the "
            f"client, or mark the test with one of: "
            f"{', '.join(sorted(_NETWORK_MARKERS))}."
        )

    def _blocked_ex(self, address, *a, **kw):
        if _is_local(address):
            return real_connect_ex(self, address, *a, **kw)
        return 111  # ECONNREFUSED — for callers that check the return code

    def _blocked_getaddrinfo(host, port, *a, **kw):
        # DNS is where an unreachable host actually stalls: resolution blocks
        # long before connect() is reached, which is why blocking connect alone
        # did not stop the hang.
        if _is_local((host, port)):
            return real_getaddrinfo(host, port, *a, **kw)
        raise socket.gaierror(
            f"{request.node.nodeid} attempted to resolve {host!r}. Unit tests "
            f"must not use the network — mock the client, or mark the test with "
            f"one of: {', '.join(sorted(_NETWORK_MARKERS))}."
        )

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked_ex
    socket.getaddrinfo = _blocked_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.getaddrinfo = real_getaddrinfo
