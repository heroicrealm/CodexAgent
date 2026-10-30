"""Compatibility fixture for business regressions; production code is unpatched."""
import pytest


@pytest.fixture
def legacy_boundary_contract():
    yield
