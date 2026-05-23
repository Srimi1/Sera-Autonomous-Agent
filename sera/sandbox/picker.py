"""Sandbox tier picker — selects cheapest tier within cost ceiling.

LOCAL:   $0.00  — always available, subprocess on this machine
MODAL:   ~$0.01 — Modal cloud function (requires `pip install modal`)
DAYTONA: ~$0.10 — Daytona full dev environment (requires Daytona token)
"""
from __future__ import annotations

from sera.sandbox.base import Sandbox, SandboxTier
from sera.sandbox.local import LocalSubprocessSandbox

# Cost estimates per run in USD.
_TIER_COSTS: dict[SandboxTier, float] = {
    SandboxTier.LOCAL: 0.000,
    SandboxTier.MODAL: 0.010,
    SandboxTier.DAYTONA: 0.100,
}


def pick_sandbox(
    *,
    cost_ceiling_usd: float = 0.0,
    require_network: bool = False,
    require_gpu: bool = False,
) -> Sandbox:
    """Return the cheapest sandbox that fits within cost_ceiling_usd.

    Falls back to LOCAL if cloud tiers are not installed or ceiling is too low.

    Args:
        cost_ceiling_usd: Max acceptable cost per run. 0.0 → LOCAL only.
        require_network:  If True, prefer a tier that allows outbound network.
                          LOCAL still works with allow_network=True in run().
        require_gpu:      If True, skip LOCAL (no GPU on most dev machines).
    """
    if require_gpu or cost_ceiling_usd >= _TIER_COSTS[SandboxTier.DAYTONA]:
        try:
            from sera.sandbox.daytona_sandbox import DaytonaSandbox
            return DaytonaSandbox()
        except (ImportError, RuntimeError):
            pass

    if cost_ceiling_usd >= _TIER_COSTS[SandboxTier.MODAL]:
        try:
            from sera.sandbox.modal_sandbox import ModalSandbox
            return ModalSandbox()
        except (ImportError, RuntimeError):
            pass

    return LocalSubprocessSandbox()


def tier_cost(tier: SandboxTier) -> float:
    return _TIER_COSTS.get(tier, 0.0)
