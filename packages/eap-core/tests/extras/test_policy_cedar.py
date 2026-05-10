import pytest

pytest.importorskip("cedarpy")
pytestmark = pytest.mark.extras


def test_cedar_adapter_module_exists_when_extra_installed():
    """Smoke test: the import path resolves when cedarpy is available."""
    import cedarpy  # noqa: F401
