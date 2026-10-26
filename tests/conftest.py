"""Explicit contracts for legacy business regression, never kernel acceptance."""
import pytest


@pytest.fixture
def legacy_boundary_contract():
    # Separate patch scope: fault-injection tests may undo their own patches.
    # No environment/config switch enables this mock in a daemon subprocess.
    from dispatcher import containment
    with pytest.MonkeyPatch.context() as contract:
        contract.setattr(containment, 'require_control', lambda: None)
        yield
