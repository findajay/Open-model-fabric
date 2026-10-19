"""Routing engine: decides which provider/model serves a request.

Policies
--------
* ``explicit``                — the caller named ``provider_id``. Exactly one candidate;
                                never falls back; health is not consulted (the caller
                                asked for it, so they get a precise error instead).
* ``default``                 — the configured default provider.
* ``preferred_with_fallback`` — the first preferred provider, then (only if
                                ``allow_cross_provider_fallback``) the remaining preferred
                                and fallback providers in order.
* ``capability_based``        — every enabled provider that serves the model with the
                                required capabilities; local and healthy providers first.

Safeguards
----------
* Prompts marked ``sensitive`` are never routed to providers whose effective
  ``allow_sensitive`` is false (external providers by default).
* A candidate is only used if the model resolves on that provider and every required
  capability is claimed. Unknown capabilities are treated as unsupported.
* Fallback to another provider happens only for *retryable* errors and only before any
  output was delivered (enforced by the inference service).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omf.core.domain.capabilities import Capability
from omf.core.domain.errors import (
    CapabilityNotSupported,
    ModelUnavailable,
    NoRouteAvailable,
    OMFError,
    PolicyViolation,
    ProviderDisabled,
    ProviderUnavailable,
)
from omf.core.domain.models import (
    DeploymentState,
    HealthStatus,
    Locality,
    ModelDeployment,
    RouteAttempt,
    RoutingMode,
    RoutingPolicy,
    Sensitivity,
)
from omf.core.services.health import HealthMonitor
from omf.core.services.model_registry import ModelRegistryService
from omf.core.services.provider_registry import ProviderRegistry


@dataclass
class RouteRequest:
    provider_id: str | None
    model: str | None
    required: set[Capability]
    sensitivity: Sensitivity = Sensitivity.NORMAL
    default_model: str | None = None


@dataclass
class RoutePlan:
    mode: RoutingMode
    candidates: list[ModelDeployment]
    considered: list[RouteAttempt] = field(default_factory=list)
    max_attempts: int = 1


class Router:
    def __init__(
        self, providers: ProviderRegistry, models: ModelRegistryService, health: HealthMonitor
    ) -> None:
        self._providers = providers
        self._models = models
        self._health = health

    async def plan(self, req: RouteRequest, policy: RoutingPolicy) -> RoutePlan:
        if req.provider_id:
            deployment = await self._evaluate(req.provider_id, req, explicit=True)
            assert deployment is not None
            return RoutePlan(
                mode=RoutingMode.EXPLICIT,
                candidates=[deployment],
                considered=[
                    RouteAttempt(
                        provider_id=req.provider_id,
                        model=deployment.provider_model,
                        outcome="selected",
                    )
                ],
                max_attempts=1,
            )

        mode = policy.mode
        order = self._order(policy, req)
        considered: list[RouteAttempt] = []
        eligible: list[ModelDeployment] = []
        errors: list[OMFError] = []
        for provider_id in order:
            try:
                deployment = await self._evaluate(provider_id, req, explicit=False, policy=policy)
            except OMFError as exc:
                errors.append(exc)
                considered.append(
                    RouteAttempt(
                        provider_id=provider_id,
                        model=req.model or "",
                        outcome="skipped",
                        reason=exc.message,
                        error_code=exc.code,
                    )
                )
                continue
            assert deployment is not None
            eligible.append(deployment)
            considered.append(
                RouteAttempt(
                    provider_id=provider_id, model=deployment.provider_model, outcome="selected"
                )
            )

        if mode == RoutingMode.CAPABILITY_BASED and not policy.allow_cross_provider_fallback:
            eligible = eligible[:1]
        if not eligible:
            if len(order) == 1 and errors:
                raise errors[0]  # the precise reason is more useful than "no route"
            raise NoRouteAvailable(
                "no provider can serve this request",
                details={"policy": mode.value, "considered": [c.model_dump() for c in considered]},
            )
        max_attempts = policy.max_attempts if policy.allow_cross_provider_fallback else 1
        return RoutePlan(
            mode=mode, candidates=eligible, considered=considered, max_attempts=max_attempts
        )

    # ------------------------------------------------------------------ helpers

    def _order(self, policy: RoutingPolicy, req: RouteRequest) -> list[str]:
        fallback = policy.fallback_provider_ids if policy.allow_cross_provider_fallback else []
        if policy.mode in (RoutingMode.DEFAULT, RoutingMode.EXPLICIT):
            if not policy.default_provider_id:
                raise NoRouteAvailable(
                    "no provider specified and no default provider configured",
                    details={"hint": "set settings.default_provider_id or pass provider_id"},
                )
            order = [policy.default_provider_id, *fallback]
        elif policy.mode == RoutingMode.PREFERRED_WITH_FALLBACK:
            preferred = policy.preferred_provider_ids
            if not preferred:
                raise NoRouteAvailable("preferred_with_fallback policy has no preferred providers")
            rest = [*preferred[1:], *fallback] if policy.allow_cross_provider_fallback else []
            order = [preferred[0], *rest]
        else:  # capability based
            preferred_rank = {p: i for i, p in enumerate(policy.preferred_provider_ids)}

            def rank(pid: str) -> tuple[int, int, int]:
                health = self._health.get(pid).status
                eff = self._providers.effective(pid)
                return (
                    0 if health == HealthStatus.UP else 1 if health != HealthStatus.DOWN else 2,
                    0 if eff.locality == Locality.LOCAL else 1,
                    preferred_rank.get(pid, len(preferred_rank)),
                )

            order = sorted(self._providers.enabled_ids(), key=rank)
        return list(dict.fromkeys(order))  # de-duplicate, keep order

    async def _evaluate(
        self,
        provider_id: str,
        req: RouteRequest,
        *,
        explicit: bool,
        policy: RoutingPolicy | None = None,
    ) -> ModelDeployment:
        definition = self._providers.get(provider_id)  # ProviderNotFound
        if not definition.enabled:
            raise ProviderDisabled(
                f"provider {provider_id!r} is disabled", details={"provider_id": provider_id}
            )
        effective = self._providers.effective(provider_id)
        if req.sensitivity == Sensitivity.SENSITIVE and not effective.allow_sensitive:
            raise PolicyViolation(
                f"request is marked sensitive and provider {provider_id!r} "
                f"({effective.locality.value}) does not accept sensitive prompts",
                details={"provider_id": provider_id, "locality": effective.locality.value},
            )
        if not explicit and policy is not None and policy.respect_health:
            health = self._health.get(provider_id)
            if health.status == HealthStatus.DOWN:
                raise ProviderUnavailable(
                    f"provider {provider_id!r} is currently down ({health.message})",
                    details={"provider_id": provider_id, "health": health.status.value},
                )

        model = req.model or req.default_model
        if model:
            try:
                deployment = await self._models.resolve(provider_id, model)
            except OMFError:
                if req.model is None and req.default_model:
                    # The global default model is not served here; fall through to the
                    # provider's own first suitable model.
                    deployment = await self._first_suitable(provider_id, req)
                else:
                    raise
        else:
            deployment = await self._first_suitable(provider_id, req)

        if deployment.state == DeploymentState.UNAVAILABLE:
            raise ModelUnavailable(
                f"model {deployment.provider_model!r} is declared for {provider_id!r} but the "
                "provider does not currently serve it",
                details={"provider_id": provider_id, "model": deployment.provider_model},
            )
        missing = deployment.capabilities.missing(req.required)
        if missing:
            raise CapabilityNotSupported(
                f"model {deployment.provider_model!r} on {provider_id!r} does not declare: "
                + ", ".join(m.value for m in missing),
                details={
                    "provider_id": provider_id,
                    "model": deployment.provider_model,
                    "missing": [m.value for m in missing],
                    "declared": deployment.capabilities.names(),
                },
            )
        return deployment

    async def _first_suitable(self, provider_id: str, req: RouteRequest) -> ModelDeployment:
        deployments = await self._models.deployments(provider_id)
        for d in deployments:
            if d.state == DeploymentState.AVAILABLE and not d.capabilities.missing(req.required):
                return d
        error = await self._models.discovery_error(provider_id)
        if error is not None:
            raise error
        raise NoRouteAvailable(
            f"provider {provider_id!r} serves no model with the required capabilities",
            details={"provider_id": provider_id, "required": sorted(c.value for c in req.required)},
        )
