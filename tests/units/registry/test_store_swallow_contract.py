"""Pin what the real registry store does on failure, so fakes cannot drift from it.

Three bugs reached a live deployment in a row, and all three shared one cause: the
fakes in the unit suite encoded a mental model of the store instead of its actual
contract. The clearest case was ``list_app_roles``. A fake written as::

    def list_app_roles(self):
        raise RuntimeError('relation "app_roles" does not exist')

made eight tests pass on a fix that could never fire in production, because
``PostgresRegistryStore.list_app_roles`` catches its own exceptions and returns
``[]``. The fix handled a raise; production returned empty. The tests proved the fix
against a fake designed to make it work.

This module asserts the real behaviour directly, so:

* a change from "swallow and return a default" to "raise" fails here loudly, rather
  than silently invalidating every fake elsewhere; and
* anyone writing a fake has one place to read the contract from.

``EMPTY_ON_FAILURE`` is the list of read methods that report failure as an empty
result. There are ~25 such sites in the store, which makes this a systemic property
of the class worth stating once rather than rediscovering per bug.
"""

import inspect
import re

import pytest

from back.objects.registry.store.postgres import store as store_mod

pytestmark = pytest.mark.unit

#: Read methods whose documented behaviour is "return a default, never raise".
EMPTY_ON_FAILURE = ["list_app_roles"]


def _method_source(name: str) -> str:
    return inspect.getsource(getattr(store_mod.PostgresRegistryStore, name))


class TestSwallowingIsDeliberateAndDocumented:
    @pytest.mark.parametrize("name", EMPTY_ON_FAILURE)
    def test_catches_broadly_and_returns_a_default(self, name):
        src = _method_source(name)
        assert "except Exception" in src, (
            f"{name} no longer swallows. If that is intended, every fake that "
            f"returns a default for it is now wrong -- update them and this list."
        )
        assert re.search(r"return \[\]|return \{\}|return None", src), (
            f"{name} catches but does not obviously return a default"
        )

    @pytest.mark.parametrize("name", EMPTY_ON_FAILURE)
    def test_a_missing_table_is_reported_as_empty_not_an_error(self, name):
        """The exact production condition: the table does not exist yet."""
        inst = store_mod.PostgresRegistryStore.__new__(
            store_mod.PostgresRegistryStore
        )

        def boom(*a, **k):
            raise RuntimeError('relation "ontobricks_registry.app_roles" does not exist')

        object.__setattr__(inst, "_connect", boom)
        object.__setattr__(inst, "_schema", "ontobricks_registry")
        object.__setattr__(inst, "_q", lambda s: f'"{s}"')
        assert getattr(inst, name)() == [], (
            f"{name} raised on a missing table. Fakes across the suite assume it "
            f"returns [] -- if this changes, they all need revisiting."
        )


class TestTheContractMatchesWhatCallersAssume:
    def test_resolve_role_handles_the_empty_case_not_only_the_raising_one(self):
        """The regression that shipped: the fix only covered a raise."""
        from back.objects.registry.AppRoleService import AppRoleService

        src = inspect.getsource(AppRoleService.resolve_role)
        assert "admin_exists" in src, (
            "resolve_role must decide the bootstrap grant from whether an admin is "
            "recorded, which covers both an empty list and a raise. Keying it on "
            "the exception alone is what let the deadlock ship."
        )
