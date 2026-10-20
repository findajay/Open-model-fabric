"""Inference application service: routing, execution, streaming, cancellation, records.

Retry/fallback safety: a chat completion is not idempotent from a cost and audit
perspective, so the service only moves to another candidate when

* the error is ``retryable`` (connectivity/timeout/overload), **and**
* no output has been delivered to the caller yet (for streams: before the first delta),
* and the routing plan allows more than one attempt (cross-provider fallback enabled).

Cancellation: every in-flight request runs in its own asyncio task registered under its
request id. ``cancel(request_id)`` cancels that task (which closes the adapter's HTTP
stream) and additionally asks the provider for server-side cancellation when the
adapter supports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from omf.core.domain.capabilities import Capability
from omf.core.domain.errors import (
    ContextLengthExceeded,
    ConversationNotFound,
    OMFError,
    OperationNotSupported,
    ProviderError,
    ProviderTimeout,
    RequestCancelled,
)
from omf.core.domain.events import (
    ErrorBody,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    RequestFailed,
    StreamEvent,
    StreamEventType,
    UsageReported,
)
from omf.core.domain.models import (
    ChatMessage,
    Conversation,
    Embedding,
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationParams,
    InferenceRecord,
    InferenceRequest,
    InferenceResponse,
    ModelDeployment,
    RequestStatus,
    ResponseFormat,
    Role,
    RouteAttempt,
    RouteDecision,
    Sensitivity,
    Timings,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    new_id,
    utcnow,
)
from omf.core.ports.metadata import (
    ConversationRepository,
    InferenceRecordRepository,
    RequestCoordinationRepository,
)
from omf.core.ports.telemetry import TelemetryProvider
from omf.core.services.health import HealthMonitor
from omf.core.services.provider_registry import ProviderRegistry
from omf.core.services.routing import RoutePlan, Router, RouteRequest
from omf.core.services.settings import SettingsService

log = logging.getLogger(__name__)


@dataclass
class ChatCommand:
    messages: list[ChatMessage]
    provider_id: str | None = None
    model: str | None = None
    params: GenerationParams = field(default_factory=GenerationParams)
    conversation_id: str | None = None
    persist: bool = False
    sensitivity: Sensitivity = Sensitivity.NORMAL
    required_capabilities: set[Capability] = field(default_factory=set)
    tools: list[ToolDefinition] = field(default_factory=list)
    tool_choice: str = "auto"
    request_id: str = field(default_factory=lambda: new_id("req"))
    record: bool = True
    owner: str | None = None
    """Principal making the request; conversations are scoped to their owner."""
    owner_is_admin: bool = False


@dataclass
class EmbedCommand:
    inputs: list[str]
    provider_id: str | None = None
    model: str | None = None
    dimensions: int | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    request_id: str = field(default_factory=lambda: new_id("emb"))
    record: bool = True


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token). Always labelled ``estimated``."""
    return max(1, round(len(text) / 4)) if text else 0


def _tps(tokens: int | None, total_ms: float, ttft_ms: float | None) -> float | None:
    if not tokens:
        return None
    gen_ms = total_ms - (ttft_ms or 0.0)
    if gen_ms <= 0:
        gen_ms = total_ms
    return round(tokens / (gen_ms / 1000), 2) if gen_ms > 0 else None


class InferenceService:
    def __init__(
        self,
        providers: ProviderRegistry,
        router: Router,
        health: HealthMonitor,
        settings: SettingsService,
        conversations: ConversationRepository,
        records: InferenceRecordRepository,
        telemetry: TelemetryProvider,
        *,
        log_prompt_content: bool = False,
    ) -> None:
        self._log_prompts = log_prompt_content
        self._providers = providers
        self._router = router
        self._health = health
        self._settings = settings
        self._conversations = conversations
        self._records = records
        self._telemetry = telemetry
        self._inflight: dict[str, tuple[asyncio.Task[Any], list[str]]] = {}
        self._owners: dict[str, str | None] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._coordinator: RequestCoordinationRepository | None = None
        self._replica = new_id("replica")
        self._poller: asyncio.Task[None] | None = None

    # ================================================================ coordination

    def enable_shared_cancellation(self, coordinator: RequestCoordinationRepository) -> None:
        """Record in-flight requests in shared storage so cancels reach any replica."""
        self._coordinator = coordinator

    def start(self, *, poll_s: float = 0.5) -> None:
        if self._coordinator is not None and self._poller is None:
            self._poller = asyncio.create_task(self._poll_cancels(poll_s))

    async def aclose(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None

    async def _poll_cancels(self, poll_s: float) -> None:
        assert self._coordinator is not None
        last_prune = 0.0
        while True:
            await asyncio.sleep(poll_s)
            try:
                for request_id in await self._coordinator.take_cancels(list(self._inflight)):
                    await self.cancel(request_id)  # authorised when the cancel was recorded
                if time.monotonic() - last_prune > 300:
                    stale_after = max(3600.0, 2 * self._settings.current.request_timeout_s)
                    await self._coordinator.prune_inflight(stale_after)
                    last_prune = time.monotonic()
            except OMFError as exc:
                log.warning("cancellation poll failed: %s", exc.message)

    async def _register(self, cmd: ChatCommand) -> None:
        if self._coordinator is not None:
            try:
                await self._coordinator.register_inflight(cmd.request_id, cmd.owner, self._replica)
            except OMFError as exc:  # cancellation degrades to local; the request proceeds
                log.warning("could not register request %s: %s", cmd.request_id, exc.message)

    async def _unregister(self, request_id: str) -> None:
        if self._coordinator is not None:
            with contextlib.suppress(OMFError):
                await self._coordinator.unregister_inflight(request_id)

    # ================================================================ public API

    async def complete(self, cmd: ChatCommand) -> InferenceResponse:
        task: asyncio.Task[InferenceResponse] = asyncio.create_task(self._complete(cmd))
        self._inflight[cmd.request_id] = (task, [])
        self._owners[cmd.request_id] = cmd.owner
        try:
            await self._register(cmd)
            return await task
        except asyncio.CancelledError:
            if task.cancelled() and not _current_task_cancelling():
                raise RequestCancelled(
                    "request was cancelled", details={"request_id": cmd.request_id}
                ) from None
            _cancel_once(task)
            raise
        finally:
            self._inflight.pop(cmd.request_id, None)
            self._owners.pop(cmd.request_id, None)
            await self._unregister(cmd.request_id)

    async def stream(self, cmd: ChatCommand) -> AsyncGenerator[StreamEvent, None]:
        """Yield normalised events. Closing the iterator cancels the request."""
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        task: asyncio.Task[None] = asyncio.create_task(self._produce(cmd, queue))
        self._inflight[cmd.request_id] = (task, [])
        self._owners[cmd.request_id] = cmd.owner
        finished = False
        try:
            await self._register(cmd)
            while True:
                item = await queue.get()
                if item is None:
                    finished = True
                    break
                if item.terminal:
                    finished = True
                yield item
                if finished:
                    break
        finally:
            # After a terminal event the producer only has bookkeeping left: let it finish.
            # Otherwise the consumer went away (client disconnect): cancel the request.
            if not task.done():
                if not finished:
                    _cancel_once(task)
                # Shield: if *we* are being cancelled (e.g. the HTTP response task), the
                # producer must still finish its bookkeeping (record, partial message).
                self._background.add(task)
                task.add_done_callback(self._background.discard)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(task)
            self._inflight.pop(cmd.request_id, None)
            self._owners.pop(cmd.request_id, None)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self._unregister(cmd.request_id))

    async def preflight(self, cmd: ChatCommand, *, streaming: bool) -> RoutePlan:
        """Validate and route without side effects, so that streaming endpoints can return a
        proper HTTP error before the event stream starts."""
        if cmd.conversation_id:
            self._check_owner(
                cmd,
                await self._conversations.get_conversation(
                    cmd.conversation_id, include_messages=False
                ),
            )
        return await self._router.plan(
            self._route_request(cmd, streaming=streaming), self._settings.routing_policy()
        )

    def _route_request(self, cmd: ChatCommand, *, streaming: bool) -> RouteRequest:
        required = {Capability.CHAT, *cmd.required_capabilities}
        if any(m.images() for m in cmd.messages):
            required.add(Capability.VISION_INPUT)
        if cmd.params.response_format == ResponseFormat.JSON:
            required.add(Capability.STRUCTURED_OUTPUT)
        if (cmd.tools and cmd.tool_choice != "none") or any(
            m.role == Role.TOOL or m.tool_calls for m in cmd.messages
        ):
            required.add(Capability.TOOL_CALLING)
        if streaming:
            required.add(Capability.STREAMING)
        return RouteRequest(
            provider_id=cmd.provider_id,
            model=cmd.model,
            required=required,
            sensitivity=cmd.sensitivity,
            default_model=self._settings.current.default_model,
        )

    async def embed(self, cmd: EmbedCommand) -> EmbeddingResponse:
        """Embed ``cmd.inputs`` with a model that declares the ``embeddings`` capability.

        Embeddings have no side effects, so retryable failures may move to the next
        candidate whenever the routing policy allows fallback.
        """
        started = time.perf_counter()
        plan: RoutePlan | None = None
        attempts: list[RouteAttempt] = []
        try:
            plan = await self._router.plan(
                RouteRequest(
                    provider_id=cmd.provider_id,
                    model=cmd.model,
                    required={Capability.EMBEDDINGS},
                    sensitivity=cmd.sensitivity,
                    default_model=None,  # the default model is a chat model
                ),
                self._settings.routing_policy(),
            )
            attempts = list(plan.considered)
            last_error: OMFError | None = None
            for index, deployment in enumerate(plan.candidates[: plan.max_attempts]):
                request = EmbeddingRequest(
                    request_id=cmd.request_id,
                    model=deployment.provider_model,
                    inputs=cmd.inputs,
                    dimensions=cmd.dimensions,
                )
                call_started = time.perf_counter()
                try:
                    timeout = self._timeout(deployment.provider_id)
                    async with self._providers.lease(deployment.provider_id) as adapter:
                        async with asyncio.timeout(timeout):
                            result = await adapter.embed(request)
                except TimeoutError:
                    last_error = ProviderTimeout(
                        f"provider {deployment.provider_id!r} exceeded {timeout}s",
                        details={"provider_id": deployment.provider_id},
                    )
                except OMFError as exc:
                    last_error = exc
                else:
                    total_ms = (time.perf_counter() - call_started) * 1000
                    self._health.record_success(deployment.provider_id, total_ms)
                    attempts.append(
                        RouteAttempt(
                            provider_id=deployment.provider_id,
                            model=deployment.provider_model,
                            outcome="succeeded",
                        )
                    )
                    usage = result.usage or TokenUsage(
                        prompt_tokens=sum(estimate_tokens(t) for t in cmd.inputs),
                        source="estimated",
                    )
                    timings = Timings(total_ms=round(total_ms, 2))
                    await self._record(
                        cmd,
                        RequestStatus.COMPLETED,
                        deployment,
                        plan,
                        len(attempts),
                        timings,
                        usage,
                        None,
                        streaming=False,
                    )
                    return EmbeddingResponse(
                        request_id=cmd.request_id,
                        provider_id=deployment.provider_id,
                        model=deployment.provider_model,
                        model_id=deployment.model_id,
                        data=[
                            Embedding(index=i, embedding=v) for i, v in enumerate(result.vectors)
                        ],
                        dimensions=len(result.vectors[0]),
                        usage=usage,
                        timings=timings,
                        route=RouteDecision(
                            policy=plan.mode.value,
                            provider_id=deployment.provider_id,
                            model=deployment.provider_model,
                            model_id=deployment.model_id,
                            attempts=attempts,
                        ),
                    )
                assert last_error is not None
                if not isinstance(last_error, OperationNotSupported):
                    self._health.record_failure(deployment.provider_id, last_error)
                attempts.append(
                    RouteAttempt(
                        provider_id=deployment.provider_id,
                        model=deployment.provider_model,
                        outcome="failed",
                        reason=last_error.message,
                        error_code=last_error.code,
                    )
                )
                is_last = index >= min(len(plan.candidates), plan.max_attempts) - 1
                if not last_error.retryable or is_last:
                    break
            assert last_error is not None
            last_error.details.setdefault("attempts", [a.model_dump() for a in attempts])
            raise last_error
        except OMFError as exc:
            await self._record(
                cmd,
                RequestStatus.FAILED,
                None,
                plan,
                len(attempts),
                Timings(total_ms=(time.perf_counter() - started) * 1000),
                None,
                exc.code,
                streaming=False,
            )
            raise

    async def cancel(
        self, request_id: str, *, owner: str | None = None, is_admin: bool = True
    ) -> bool:
        """Cancel an in-flight request. Non-admin callers can only cancel their own.

        With shared cancellation, a request served by another replica is cancelled by
        recording a cancel request that its replica picks up within ``poll_s``.
        """
        entry = self._inflight.get(request_id)
        if entry is None and self._coordinator is not None:
            found, request_owner = await self._coordinator.inflight_owner(request_id)
            if not found or (not is_admin and request_owner != owner):
                return False
            await self._coordinator.request_cancel(request_id)
            return True
        if entry is None or (not is_admin and self._owners.get(request_id) != owner):
            return False
        task, provider_ids = entry
        for pid in provider_ids[-1:]:
            with contextlib.suppress(OperationNotSupported, OMFError):
                async with self._providers.lease(pid) as adapter:
                    await adapter.cancel(request_id)
        _cancel_once(task)
        return True

    async def inflight(self, *, owner: str | None = None, is_admin: bool = True) -> list[str]:
        local = [r for r in self._inflight if is_admin or self._owners.get(r) == owner]
        if self._coordinator is None:
            return local
        shared = await self._coordinator.list_inflight(None if is_admin else owner)
        return sorted(set(local) | set(shared))

    # ================================================================ preparation

    async def _prepare(
        self, cmd: ChatCommand, *, streaming: bool
    ) -> tuple[RoutePlan, list[ChatMessage], Conversation | None]:
        conversation: Conversation | None = None
        history: list[ChatMessage] = []
        if cmd.conversation_id:
            conversation = self._check_owner(
                cmd, await self._conversations.get_conversation(cmd.conversation_id)
            )
            history = conversation.messages
        # Route before creating anything, so a rejected request leaves no empty conversation.
        plan = await self._router.plan(
            self._route_request(cmd, streaming=streaming), self._settings.routing_policy()
        )
        if self._log_prompts:
            # Opt-in only (security.log_prompt_content); off by default.
            self._telemetry.record_event(
                "omf.chat.prompt",
                {"request_id": cmd.request_id, "prompt": "\n".join(m.text() for m in cmd.messages)},
            )
        if conversation is None and cmd.persist:
            first_user = next((m.text() for m in cmd.messages if m.role == Role.USER), "")
            conversation = Conversation(
                title=(first_user[:60] or "New conversation"), owner=cmd.owner
            )
            await self._conversations.create_conversation(conversation)
            cmd.conversation_id = conversation.id
        return plan, [*history, *cmd.messages], conversation

    @staticmethod
    def _check_owner(cmd: ChatCommand, conversation: Conversation | None) -> Conversation:
        visible = conversation is not None and (
            cmd.owner_is_admin or conversation.owner is None or conversation.owner == cmd.owner
        )
        if not visible or conversation is None:
            # Same error for "missing" and "not yours": do not reveal other users' ids.
            raise ConversationNotFound(f"conversation {cmd.conversation_id!r} not found")
        return conversation

    def _check_context(
        self,
        deployment: ModelDeployment,
        messages: list[ChatMessage],
        params: GenerationParams,
        tools: list[ToolDefinition] = (),  # type: ignore[assignment]
    ) -> None:
        if not deployment.context_window:
            return
        prompt_tokens = sum(
            estimate_tokens(m.text()) + sum(estimate_tokens(c.arguments) for c in m.tool_calls)
            for m in messages
        ) + sum(estimate_tokens(t.model_dump_json()) for t in tools)
        needed = prompt_tokens + (params.max_tokens or 0)
        if needed > deployment.context_window:
            raise ContextLengthExceeded(
                f"estimated {needed} tokens exceed the model's context window of "
                f"{deployment.context_window}",
                details={"estimated_tokens": needed, "context_window": deployment.context_window},
            )

    # ================================================================ non-streaming

    async def _complete(self, cmd: ChatCommand) -> InferenceResponse:
        started = time.perf_counter()
        attempts: list[RouteAttempt] = []
        plan: RoutePlan | None = None
        try:
            plan, messages, conversation = await self._prepare(cmd, streaming=False)
            attempts = list(plan.considered)
            last_error: OMFError | None = None
            for index, deployment in enumerate(plan.candidates[: plan.max_attempts]):
                self._check_context(deployment, messages, cmd.params, cmd.tools)
                self._track(cmd.request_id, deployment.provider_id)
                request = InferenceRequest(
                    request_id=cmd.request_id,
                    model=deployment.provider_model,
                    messages=messages,
                    params=cmd.params,
                    tools=cmd.tools,
                    tool_choice=cmd.tool_choice,
                )
                call_started = time.perf_counter()
                try:
                    timeout = self._timeout(deployment.provider_id)
                    async with self._providers.lease(deployment.provider_id) as adapter:
                        async with asyncio.timeout(timeout):
                            result = await adapter.generate(request)
                except TimeoutError:
                    last_error = ProviderTimeout(
                        f"provider {deployment.provider_id!r} exceeded {timeout}s",
                        details={"provider_id": deployment.provider_id},
                    )
                except OMFError as exc:
                    last_error = exc
                else:
                    total_ms = (time.perf_counter() - call_started) * 1000
                    self._health.record_success(deployment.provider_id, total_ms)
                    attempts.append(
                        RouteAttempt(
                            provider_id=deployment.provider_id,
                            model=deployment.provider_model,
                            outcome="succeeded",
                        )
                    )
                    finish_reason = "tool_calls" if result.tool_calls else result.finish_reason
                    usage = result.usage or TokenUsage(
                        completion_tokens=estimate_tokens(result.content)
                        + sum(estimate_tokens(c.arguments) for c in result.tool_calls),
                        source="estimated",
                    )
                    timings = Timings(
                        total_ms=round(total_ms, 2),
                        tokens_per_second=_tps(usage.completion_tokens, total_ms, None),
                    )
                    route = RouteDecision(
                        policy=plan.mode.value,
                        provider_id=deployment.provider_id,
                        model=deployment.provider_model,
                        model_id=deployment.model_id,
                        attempts=attempts,
                    )
                    reply = ChatMessage(
                        role=Role.ASSISTANT,
                        content=result.content,
                        tool_calls=result.tool_calls,
                        metadata={
                            "request_id": cmd.request_id,
                            "provider_id": deployment.provider_id,
                            "model": deployment.provider_model,
                            "finish_reason": finish_reason,
                            "usage": usage.model_dump(),
                            "timings": timings.model_dump(),
                        },
                    )
                    if conversation is not None:
                        await self._conversations.append_messages(
                            conversation.id, [*cmd.messages, reply]
                        )
                    await self._record(
                        cmd,
                        RequestStatus.COMPLETED,
                        deployment,
                        plan,
                        len(attempts),
                        timings,
                        usage,
                        None,
                        streaming=False,
                    )
                    return InferenceResponse(
                        request_id=cmd.request_id,
                        provider_id=deployment.provider_id,
                        model=deployment.provider_model,
                        model_id=deployment.model_id,
                        message=reply,
                        finish_reason=finish_reason,
                        usage=usage,
                        timings=timings,
                        route=route,
                        conversation_id=conversation.id if conversation else None,
                    )
                assert last_error is not None
                self._health.record_failure(deployment.provider_id, last_error)
                attempts.append(
                    RouteAttempt(
                        provider_id=deployment.provider_id,
                        model=deployment.provider_model,
                        outcome="failed",
                        reason=last_error.message,
                        error_code=last_error.code,
                    )
                )
                is_last = index >= min(len(plan.candidates), plan.max_attempts) - 1
                if not last_error.retryable or is_last:
                    break
            assert last_error is not None
            last_error.details.setdefault("attempts", [a.model_dump() for a in attempts])
            raise last_error
        except asyncio.CancelledError:
            await self._record(
                cmd,
                RequestStatus.CANCELLED,
                None,
                plan,
                len(attempts),
                Timings(total_ms=(time.perf_counter() - started) * 1000),
                None,
                "request_cancelled",
                streaming=False,
            )
            raise
        except OMFError as exc:
            await self._record(
                cmd,
                RequestStatus.FAILED,
                None,
                plan,
                len(attempts),
                Timings(total_ms=(time.perf_counter() - started) * 1000),
                None,
                exc.code,
                streaming=False,
            )
            raise

    # ================================================================ streaming

    async def _produce(self, cmd: ChatCommand, queue: asyncio.Queue[StreamEvent | None]) -> None:
        started = time.perf_counter()
        delivered = 0
        content: list[str] = []
        tool_calls: list[ToolCall] = []
        plan: RoutePlan | None = None
        deployment: ModelDeployment | None = None
        attempts: list[RouteAttempt] = []
        conversation: Conversation | None = None

        async def emit(event: StreamEventType, data: object) -> None:
            await queue.put(StreamEvent.model_validate({"event": event, "data": data}))

        async def fail(exc: OMFError, status: RequestStatus) -> None:
            await self._record(
                cmd,
                status,
                deployment,
                plan,
                len(attempts),
                Timings(total_ms=(time.perf_counter() - started) * 1000),
                None,
                exc.code,
                streaming=True,
            )
            if conversation is not None:
                partial = (
                    [
                        ChatMessage(
                            role=Role.ASSISTANT,
                            content="".join(content),
                            metadata={
                                "request_id": cmd.request_id,
                                "status": status.value,
                                "error_code": exc.code,
                            },
                        )
                    ]
                    if content
                    else []
                )
                await self._conversations.append_messages(
                    conversation.id, [*cmd.messages, *partial]
                )
            await emit(
                StreamEventType.FAILED,
                RequestFailed(
                    request_id=cmd.request_id,
                    partial=delivered > 0,
                    error=ErrorBody(
                        code=exc.code,
                        message=exc.message,
                        retryable=exc.retryable,
                        details=_safe_details(exc.details),
                    ),
                ),
            )

        try:
            plan, messages, conversation = await self._prepare(cmd, streaming=True)
            attempts = list(plan.considered)
            last_error: OMFError | None = None
            max_attempts = min(len(plan.candidates), plan.max_attempts)
            for index, deployment in enumerate(plan.candidates[:max_attempts]):
                self._check_context(deployment, messages, cmd.params, cmd.tools)
                self._track(cmd.request_id, deployment.provider_id)
                request = InferenceRequest(
                    request_id=cmd.request_id,
                    model=deployment.provider_model,
                    messages=messages,
                    params=cmd.params,
                    tools=cmd.tools,
                    tool_choice=cmd.tool_choice,
                )
                attempt_started = time.perf_counter()
                ttft_ms: float | None = None
                usage: TokenUsage | None = None
                finish_reason: str | None = None
                announced = False  # message.started sent for this attempt
                tool_calls.clear()  # a failed attempt's calls never reach the next one
                timeout = self._timeout(deployment.provider_id)
                try:
                    async with self._providers.lease(deployment.provider_id) as adapter:
                        async with asyncio.timeout(timeout):
                            async with contextlib.aclosing(
                                adapter.stream_generate(request)
                            ) as chunks:
                                async for chunk in chunks:
                                    if not announced and (
                                        chunk.delta or chunk.finish_reason or chunk.tool_calls
                                    ):
                                        announced = True
                                        attempts.append(
                                            RouteAttempt(
                                                provider_id=deployment.provider_id,
                                                model=deployment.provider_model,
                                                outcome="succeeded",
                                            )
                                        )
                                        await emit(
                                            StreamEventType.STARTED,
                                            MessageStarted(
                                                request_id=cmd.request_id,
                                                conversation_id=conversation.id
                                                if conversation
                                                else None,
                                                provider_id=deployment.provider_id,
                                                model=deployment.provider_model,
                                                model_id=deployment.model_id,
                                                route=RouteDecision(
                                                    policy=plan.mode.value,
                                                    provider_id=deployment.provider_id,
                                                    model=deployment.provider_model,
                                                    model_id=deployment.model_id,
                                                    attempts=attempts,
                                                ),
                                            ),
                                        )
                                    if chunk.delta:
                                        if ttft_ms is None:
                                            ttft_ms = (time.perf_counter() - attempt_started) * 1000
                                        content.append(chunk.delta)
                                        await emit(
                                            StreamEventType.DELTA,
                                            MessageDelta(
                                                request_id=cmd.request_id,
                                                index=delivered,
                                                delta=chunk.delta,
                                            ),
                                        )
                                        delivered += 1
                                    tool_calls.extend(chunk.tool_calls)
                                    if chunk.usage is not None:
                                        usage = chunk.usage
                                    if chunk.finish_reason:
                                        finish_reason = chunk.finish_reason
                    if delivered == 0 and not finish_reason and not tool_calls:
                        raise ProviderError(
                            "provider returned an empty stream",
                            details={"provider_id": deployment.provider_id},
                        )
                except TimeoutError:
                    last_error = ProviderTimeout(
                        f"provider {deployment.provider_id!r} exceeded {timeout}s",
                        details={"provider_id": deployment.provider_id},
                    )
                except OMFError as exc:
                    last_error = exc
                else:
                    total_ms = (time.perf_counter() - attempt_started) * 1000
                    self._health.record_success(deployment.provider_id, total_ms)
                    text = "".join(content)
                    if tool_calls:
                        finish_reason = "tool_calls"
                    if usage is None:
                        usage = TokenUsage(
                            completion_tokens=estimate_tokens(text)
                            + sum(estimate_tokens(c.arguments) for c in tool_calls),
                            source="estimated",
                        )
                    timings = Timings(
                        total_ms=round(total_ms, 2),
                        ttft_ms=round(ttft_ms, 2) if ttft_ms is not None else None,
                        tokens_per_second=_tps(usage.completion_tokens, total_ms, ttft_ms),
                    )
                    reply = ChatMessage(
                        role=Role.ASSISTANT,
                        content=text,
                        tool_calls=tool_calls,
                        metadata={
                            "request_id": cmd.request_id,
                            "provider_id": deployment.provider_id,
                            "model": deployment.provider_model,
                            "finish_reason": finish_reason,
                            "usage": usage.model_dump(),
                            "timings": timings.model_dump(),
                        },
                    )
                    if conversation is not None:
                        await self._conversations.append_messages(
                            conversation.id, [*cmd.messages, reply]
                        )
                    await self._record(
                        cmd,
                        RequestStatus.COMPLETED,
                        deployment,
                        plan,
                        len(attempts),
                        timings,
                        usage,
                        None,
                        streaming=True,
                    )
                    await emit(
                        StreamEventType.USAGE, UsageReported(request_id=cmd.request_id, usage=usage)
                    )
                    await emit(
                        StreamEventType.COMPLETED,
                        MessageCompleted(
                            request_id=cmd.request_id,
                            message_id=reply.id,
                            conversation_id=conversation.id if conversation else None,
                            finish_reason=finish_reason,
                            timings=timings,
                            usage=usage,
                            tool_calls=tool_calls,
                        ),
                    )
                    return
                assert last_error is not None
                self._health.record_failure(deployment.provider_id, last_error)
                attempts.append(
                    RouteAttempt(
                        provider_id=deployment.provider_id,
                        model=deployment.provider_model,
                        outcome="failed",
                        reason=last_error.message,
                        error_code=last_error.code,
                    )
                )
                # Never fall back once output reached the caller: the client would see
                # two different answers spliced together.
                if delivered > 0 or not last_error.retryable or index >= max_attempts - 1:
                    break
            assert last_error is not None
            last_error.details.setdefault("attempts", [a.model_dump() for a in attempts])
            await fail(last_error, RequestStatus.FAILED)
        except asyncio.CancelledError:
            await fail(
                RequestCancelled("request was cancelled", details={"request_id": cmd.request_id}),
                RequestStatus.CANCELLED,
            )
        except OMFError as exc:
            await fail(exc, RequestStatus.FAILED)
        except Exception as exc:  # defensive: never leave a stream without a terminal event
            log.exception("unexpected error in stream %s", cmd.request_id)
            await fail(OMFError(f"internal error: {type(exc).__name__}"), RequestStatus.FAILED)
        finally:
            await queue.put(None)

    # ================================================================ helpers

    def _track(self, request_id: str, provider_id: str) -> None:
        entry = self._inflight.get(request_id)
        if entry is not None:
            entry[1].append(provider_id)

    def _timeout(self, provider_id: str) -> float:
        provider_total = self._providers.get(provider_id).timeouts.total_s
        return min(provider_total, self._settings.current.request_timeout_s)

    async def _record(
        self,
        cmd: ChatCommand | EmbedCommand,
        status: RequestStatus,
        deployment: ModelDeployment | None,
        plan: RoutePlan | None,
        attempts: int,
        timings: Timings,
        usage: TokenUsage | None,
        error_code: str | None,
        *,
        streaming: bool,
    ) -> None:
        provider_id = deployment.provider_id if deployment else (cmd.provider_id or None)
        model = deployment.provider_model if deployment else cmd.model
        operation = "embeddings" if isinstance(cmd, EmbedCommand) else "chat"
        attrs = {
            "operation": operation,
            "provider_id": provider_id or "none",
            "model": model or "none",
            "status": status.value,
            "streaming": streaming,
            "routing_policy": plan.mode.value if plan else "none",
        }
        self._telemetry.record_metric("omf.requests", 1, kind="counter", attributes=attrs)
        if status == RequestStatus.COMPLETED:
            self._telemetry.record_metric(
                "omf.provider.latency", timings.total_ms, unit="ms", attributes=attrs
            )
            if timings.ttft_ms is not None:
                self._telemetry.record_metric(
                    "omf.time_to_first_token", timings.ttft_ms, unit="ms", attributes=attrs
                )
            if timings.tokens_per_second is not None:
                self._telemetry.record_metric(
                    "omf.tokens_per_second",
                    timings.tokens_per_second,
                    unit="{token}/s",
                    attributes=attrs,
                )
        else:
            self._telemetry.record_metric(
                "omf.request.failures",
                1,
                kind="counter",
                attributes={**attrs, "error_code": error_code or "unknown"},
            )
        self._telemetry.record_trace(
            f"omf.{operation}",
            duration_ms=timings.total_ms,
            status="ok" if status == RequestStatus.COMPLETED else "error",
            attributes={**attrs, "request_id": cmd.request_id},
        )
        if not cmd.record:
            return
        try:
            await self._records.add_record(
                InferenceRecord(
                    request_id=cmd.request_id,
                    operation=operation,
                    provider_id=provider_id,
                    model=model,
                    status=status,
                    streaming=streaming,
                    routing_policy=plan.mode.value if plan else None,
                    attempts=attempts,
                    latency_ms=round(timings.total_ms, 2),
                    ttft_ms=timings.ttft_ms,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    tokens_per_second=timings.tokens_per_second,
                    error_code=error_code,
                    created_at=utcnow(),
                )
            )
        except Exception:  # recording must never break a request
            log.exception("failed to store inference record %s", cmd.request_id)


def _cancel_once(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` unless a cancellation is already pending.

    A second native ``task.cancel()`` would interrupt cleanup that is already running in
    response to the first one — e.g. httpcore closing the upstream connection inside an
    anyio shield, which does not block native asyncio cancellation — and leave the
    provider generating for a client that is gone.
    """
    if not task.done() and task.cancelling() == 0:
        task.cancel()


def _current_task_cancelling() -> bool:
    task = asyncio.current_task()
    return bool(task and task.cancelling())


def _safe_details(details: dict[str, object]) -> dict[str, object]:
    """Only pass through JSON-friendly scalars/lists/dicts (never exceptions or objects)."""
    import json

    try:
        json.dumps(details)
        return details
    except (TypeError, ValueError):
        return {k: str(v) for k, v in details.items()}
