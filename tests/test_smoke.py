"""Setup must succeed and expose the sustained-load configuration."""

from tests.conftest import ARM_LEVEL, balancer_of


async def test_setup_exposes_effective_budget(hass, world, setup_balancer):
    entry = await setup_balancer()
    balancer = balancer_of(hass, entry)
    assert balancer.effective_budget == ARM_LEVEL
    snapshot = balancer.get_diagnostics_snapshot()
    assert snapshot["sustained_shedding"]["enabled"] is True
    assert snapshot["sustained_shedding"]["armed"] is False
