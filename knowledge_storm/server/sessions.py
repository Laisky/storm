"""Session management utilities for Co-STORM conversational API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Coroutine, Dict, Iterable, List, Optional

from knowledge_storm.server.utils import utcnow


def _hash_api_key(session_id: str, api_key: str) -> str:
    """Compute a salted digest for validating the user's API key without persisting it in plain text."""
    digest = hashlib.sha256()
    digest.update(f"{session_id}:{api_key}".encode("utf-8"))
    return digest.hexdigest()


@dataclass
class ConversationEvent:
    """Represents a single event emitted by a conversation session."""

    event_id: str
    session_id: str
    event_type: str
    payload: Dict[str, Any]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the conversation event to a JSON-friendly dictionary."""
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationEvent":
        """Rehydrate a conversation event from a dictionary payload."""
        return cls(
            event_id=data["event_id"],
            session_id=data["session_id"],
            event_type=data["event_type"],
            payload=data.get("payload", {}),
            created_at=data["created_at"],
        )


class ConversationStorage:
    """Persistence layer for conversation events, metadata, and serialized session state."""

    META_KEY_TEMPLATE = "costorm:sessions:{session_id}:meta"
    STATE_KEY_TEMPLATE = "costorm:sessions:{session_id}:state"
    EVENTS_KEY_TEMPLATE = "costorm:sessions:{session_id}:events"
    INDEX_KEY = "costorm:sessions:index"

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        *,
        event_ttl_seconds: int = 3600,
        session_ttl_seconds: int = 86400,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize the storage backend.

        Args:
            redis_client: Optional Redis client instance. When omitted, an in-memory store is used.
            event_ttl_seconds: Time-to-live for persisted event streams.
            session_ttl_seconds: Time-to-live for session metadata and state snapshots.
            logger: Logger to record storage errors.
        """
        self._redis = redis_client
        self._event_ttl_seconds = event_ttl_seconds
        self._session_ttl_seconds = session_ttl_seconds
        self._logger = logger or logging.getLogger(__name__)
        if self._redis is None:
            self._meta_store: Dict[str, Dict[str, Any]] = {}
            self._state_store: Dict[str, Dict[str, Any]] = {}
            self._events_store: Dict[str, List[Dict[str, Any]]] = {}

    # -----------------------------
    # Helper utilities
    # -----------------------------
    @staticmethod
    def _meta_key(session_id: str) -> str:
        return ConversationStorage.META_KEY_TEMPLATE.format(session_id=session_id)

    @staticmethod
    def _state_key(session_id: str) -> str:
        return ConversationStorage.STATE_KEY_TEMPLATE.format(session_id=session_id)

    @staticmethod
    def _events_key(session_id: str) -> str:
        return ConversationStorage.EVENTS_KEY_TEMPLATE.format(session_id=session_id)

    # -----------------------------
    # Metadata operations
    # -----------------------------
    def store_meta(self, session_id: str, meta: Dict[str, Any]) -> None:
        """Persist session metadata."""
        if self._redis is None:
            self._meta_store[session_id] = meta
            return
        try:
            key = self._meta_key(session_id)
            self._redis.set(key, json.dumps(meta), ex=self._session_ttl_seconds)
            self._redis.sadd(self.INDEX_KEY, session_id)
            self._redis.expire(self.INDEX_KEY, self._session_ttl_seconds)
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to store session meta", exc_info=exc)

    def load_meta(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load session metadata if available."""
        if self._redis is None:
            return self._meta_store.get(session_id)
        try:
            data = self._redis.get(self._meta_key(session_id))
            if data is None:
                return None
            return json.loads(data)
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to load session meta", exc_info=exc)
            return None

    def delete_meta(self, session_id: str) -> None:
        """Remove stored metadata for a session."""
        if self._redis is None:
            self._meta_store.pop(session_id, None)
            return
        try:
            self._redis.delete(self._meta_key(session_id))
            self._redis.srem(self.INDEX_KEY, session_id)
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to delete session meta", exc_info=exc)

    def list_session_ids(self) -> List[str]:
        """Return all session identifiers tracked by the storage layer."""
        if self._redis is None:
            return list(self._meta_store.keys())
        try:
            members = self._redis.smembers(self.INDEX_KEY)
            if members is None:
                return []
            return [member.decode("utf-8") if isinstance(member, bytes) else member for member in members]
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to list session ids", exc_info=exc)
            return []

    # -----------------------------
    # State operations
    # -----------------------------
    def store_state(self, session_id: str, state: Dict[str, Any]) -> None:
        """Persist serialized runner state for the session."""
        if self._redis is None:
            self._state_store[session_id] = state
            return
        try:
            key = self._state_key(session_id)
            self._redis.set(key, json.dumps(state), ex=self._session_ttl_seconds)
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to store session state", exc_info=exc)

    def load_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load serialized runner state for the session."""
        if self._redis is None:
            return self._state_store.get(session_id)
        try:
            data = self._redis.get(self._state_key(session_id))
            if data is None:
                return None
            return json.loads(data)
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to load session state", exc_info=exc)
            return None

    def delete_state(self, session_id: str) -> None:
        """Remove stored runner state for the session."""
        if self._redis is None:
            self._state_store.pop(session_id, None)
            return
        try:
            self._redis.delete(self._state_key(session_id))
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to delete session state", exc_info=exc)

    # -----------------------------
    # Event operations
    # -----------------------------
    def append_event(self, session_id: str, event: ConversationEvent) -> None:
        """Append an event to the persisted event stream."""
        if self._redis is None:
            self._events_store.setdefault(session_id, []).append(event.to_dict())
            return
        try:
            key = self._events_key(session_id)
            self._redis.rpush(key, json.dumps(event.to_dict()))
            self._redis.expire(key, self._event_ttl_seconds)
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to append session event", exc_info=exc)

    def load_events(self, session_id: str) -> List[ConversationEvent]:
        """Load the full event stream for a session."""
        if self._redis is None:
            raw_events = self._events_store.get(session_id, [])
        else:
            try:
                raw_events = self._redis.lrange(self._events_key(session_id), 0, -1) or []
            except Exception as exc:  # pragma: no cover - logging only
                self._logger.exception("Failed to load session events", exc_info=exc)
                raw_events = []
        events: List[ConversationEvent] = []
        for item in raw_events:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            if isinstance(item, str):
                try:
                    data = json.loads(item)
                except json.JSONDecodeError:
                    continue
            else:
                data = item
            if isinstance(data, dict):
                events.append(ConversationEvent.from_dict(data))
        return events

    def delete_events(self, session_id: str) -> None:
        """Delete persisted events for the session."""
        if self._redis is None:
            self._events_store.pop(session_id, None)
            return
        try:
            self._redis.delete(self._events_key(session_id))
        except Exception as exc:  # pragma: no cover - logging only
            self._logger.exception("Failed to delete session events", exc_info=exc)


@dataclass
class SessionState:
    """Serializable snapshot of a conversation session."""

    runner_argument: Dict[str, Any]
    conversation_history: List[Dict[str, Any]]
    warmstart_conv_archive: List[Dict[str, Any]]
    experts: List[Dict[str, Any]]
    knowledge_base: Dict[str, Any]
    event_sequence: int
    warm_start_complete: bool

    def to_dict(self) -> Dict[str, Any]:
        """Export the session state as a dictionary."""
        return {
            "runner_argument": self.runner_argument,
            "conversation_history": self.conversation_history,
            "warmstart_conv_archive": self.warmstart_conv_archive,
            "experts": self.experts,
            "knowledge_base": self.knowledge_base,
            "event_sequence": self.event_sequence,
            "warm_start_complete": self.warm_start_complete,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        """Create a session state instance from dictionary data."""
        return cls(
            runner_argument=data.get("runner_argument", {}),
            conversation_history=data.get("conversation_history", []),
            warmstart_conv_archive=data.get("warmstart_conv_archive", []),
            experts=data.get("experts", []),
            knowledge_base=data.get("knowledge_base", {}),
            event_sequence=int(data.get("event_sequence", 0)),
            warm_start_complete=bool(data.get("warm_start_complete", False)),
        )


class ConversationSession:
    """In-memory representation of a conversational Co-STORM session."""

    def __init__(
        self,
        *,
        session_id: str,
        api_key: str,
        user_id: str,
        topic: str,
        runner: Any,
        runner_overrides: Optional[Dict[str, Any]],
        storage: ConversationStorage,
        executor: Any,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        max_event_history: int = 512,
        restored_state: Optional[SessionState] = None,
        auth_digest: Optional[str] = None,
        initial_events: Optional[List[ConversationEvent]] = None,
    ) -> None:
        """Initialize the conversation session wrapper."""
        self.session_id = session_id
        self.user_id = user_id
        self.topic = topic
        self._runner = runner
        self._runner_overrides = runner_overrides or {}
        self._storage = storage
        self._executor = executor
        self._loop = loop
        self._logger = logger
        self._max_event_history = max_event_history
        self._event_sequence = 0
        self._events: List[ConversationEvent] = []
        self._event_cv = asyncio.Condition()
        self._lock = asyncio.Lock()
        self._background_tasks: List[asyncio.Task] = []
        self._warm_start_complete = asyncio.Event()
        self._closed = False
        self.created_at = utcnow()
        self.updated_at = self.created_at
        self._auth_digest = auth_digest or _hash_api_key(session_id, api_key)
        if restored_state is not None:
            self._event_sequence = restored_state.event_sequence
            if restored_state.warm_start_complete:
                self._warm_start_complete.set()
        if initial_events:
            self._events.extend(initial_events)
            if initial_events:
                try:
                    self._event_sequence = max(
                        self._event_sequence,
                        max(int(event.event_id) for event in initial_events),
                    )
                except ValueError:
                    pass
                self.updated_at = initial_events[-1].created_at
        if restored_state is None:
            self._publish_system_event(
                "session.created",
                {
                    "session_id": self.session_id,
                    "topic": self.topic,
                    "user_id": self.user_id,
                },
            )
            self._schedule_background(self._run_warm_start())
        else:
            self._persist_meta()

    # -----------------------------
    # Properties and helpers
    # -----------------------------
    @property
    def runner(self) -> Any:
        """Return the underlying Co-STORM runner."""
        return self._runner

    @property
    def auth_digest(self) -> str:
        """Return the stored authentication digest."""
        return self._auth_digest

    def _next_event_id(self) -> str:
        self._event_sequence += 1
        return str(self._event_sequence)

    def _schedule_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule a background coroutine for the session."""
        task = self._loop.create_task(coro)
        self._background_tasks.append(task)
        def _discard(completed: asyncio.Task) -> None:
            try:
                self._background_tasks.remove(completed)
            except ValueError:
                pass
        task.add_done_callback(_discard)

    # -----------------------------
    # Event publishing
    # -----------------------------
    def _publish_event(self, event_type: str, payload: Dict[str, Any]) -> ConversationEvent:
        """Create, persist, and broadcast a session event."""
        event = ConversationEvent(
            event_id=self._next_event_id(),
            session_id=self.session_id,
            event_type=event_type,
            payload=payload,
            created_at=utcnow(),
        )
        self._events.append(event)
        if len(self._events) > self._max_event_history:
            self._events.pop(0)
        self.updated_at = event.created_at
        self._storage.append_event(self.session_id, event)
        self._persist_meta()
        async def notify() -> None:
            async with self._event_cv:
                self._event_cv.notify_all()
        self._loop.create_task(notify())
        return event

    def _publish_system_event(self, event_type: str, payload: Dict[str, Any]) -> ConversationEvent:
        """Publish an event emitted by the system."""
        return self._publish_event(event_type, payload)

    def _publish_turn_event(self, turn: Any, role: str) -> ConversationEvent:
        """Publish a conversation turn event."""
        payload = self._turn_to_payload(turn, role)
        return self._publish_event("conversation.turn", payload)

    # -----------------------------
    # State serialization
    # -----------------------------
    def snapshot(self) -> SessionState:
        """Capture the current session state for persistence."""
        runner = self._runner
        runner_argument = getattr(runner, "runner_argument", None)
        if runner_argument is not None and hasattr(runner_argument, "to_dict"):
            runner_argument_dict = runner_argument.to_dict()
        elif isinstance(runner_argument, dict):
            runner_argument_dict = runner_argument
        else:
            runner_argument_dict = {"topic": self.topic}
        conversation_history = [
            self._turn_to_payload(
                turn, getattr(turn, "role", "")
            )
            for turn in getattr(runner, "conversation_history", [])
        ]
        warmstart_archive = [
            self._turn_to_payload(
                turn, getattr(turn, "role", "")
            )
            for turn in getattr(runner, "warmstart_conv_archive", [])
        ]
        experts = []
        discourse_manager = getattr(runner, "discourse_manager", None)
        if discourse_manager and hasattr(discourse_manager, "serialize_experts"):
            experts = discourse_manager.serialize_experts()
        knowledge_base_dict: Dict[str, Any] = {}
        knowledge_base = getattr(runner, "knowledge_base", None)
        if knowledge_base and hasattr(knowledge_base, "to_dict"):
            knowledge_base_dict = knowledge_base.to_dict()
        return SessionState(
            runner_argument=runner_argument_dict,
            conversation_history=conversation_history,
            warmstart_conv_archive=warmstart_archive,
            experts=experts,
            knowledge_base=knowledge_base_dict,
            event_sequence=self._event_sequence,
            warm_start_complete=self._warm_start_complete.is_set(),
        )

    # -----------------------------
    # Event helpers
    # -----------------------------
    def get_events_after(self, event_id: Optional[str]) -> List[ConversationEvent]:
        """Return events published after the provided event identifier."""
        if event_id is None:
            return list(self._events)
        events: List[ConversationEvent] = []
        seen = False
        for event in self._events:
            if seen:
                events.append(event)
            elif event.event_id == event_id:
                seen = True
        if not seen:
            return list(self._events)
        return events

    async def wait_for_events(self, current_event_id: Optional[str], timeout: Optional[float]) -> None:
        """Block until new events arrive or the timeout elapses."""
        _ = current_event_id  # parameter retained for future logic and to mirror interface
        if timeout is None or timeout <= 0:
            return
        async with self._event_cv:
            try:
                await asyncio.wait_for(self._event_cv.wait(), timeout)
            except asyncio.TimeoutError:
                return

    # -----------------------------
    # Warm start and conversation flow
    # -----------------------------
    async def _run_in_executor(self, func, *args, **kwargs):
        return await self._loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))

    async def _run_warm_start(self) -> None:
        if self._closed:
            return
        try:
            await self._run_in_executor(self._runner.warm_start)
            history = getattr(self._runner, "conversation_history", [])
            for turn in history:
                self._publish_turn_event(turn, getattr(turn, "role", ""))
            self._publish_knowledge_event()
            self._warm_start_complete.set()
            self._storage.store_state(self.session_id, self.snapshot().to_dict())
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - logging only
            self._logger.exception("Warm start failed", exc_info=exc)
            self._publish_system_event("session.error", {"message": str(exc)})
            self._warm_start_complete.set()

    async def enqueue_user_message(self, message: str) -> None:
        if self._closed:
            raise RuntimeError("Session is closed")
        await self._warm_start_complete.wait()
        async with self._lock:
            await self._run_user_flow(message)

    async def _run_user_flow(self, message: str) -> None:
        """Process a user utterance and emit resulting events."""
        user_turn = await self._run_in_executor(self._runner.step, message)
        self._publish_turn_event(user_turn, getattr(user_turn, "role", "Guest"))
        system_turn = await self._run_in_executor(self._runner.step)
        self._publish_turn_event(system_turn, getattr(system_turn, "role", ""))
        knowledge_base = getattr(self._runner, "knowledge_base", None)
        if knowledge_base and hasattr(knowledge_base, "reorganize"):
            await self._run_in_executor(knowledge_base.reorganize)
        self._publish_knowledge_event()
        self._storage.store_state(self.session_id, self.snapshot().to_dict())

    def _publish_knowledge_event(self) -> None:
        knowledge_base = getattr(self._runner, "knowledge_base", None)
        payload = knowledge_base.to_dict() if knowledge_base and hasattr(knowledge_base, "to_dict") else {}
        self._publish_system_event("knowledge_base.updated", payload)

    # -----------------------------
    # API helpers
    # -----------------------------
    def validate_api_key(self, api_key: str) -> None:
        """Ensure the provided API key matches the stored digest."""
        if _hash_api_key(self.session_id, api_key) != self._auth_digest:
            raise PermissionError("Invalid API key for session")

    def to_view(self) -> Dict[str, Any]:
        """Return a JSON-friendly snapshot for API responses."""
        knowledge_base = getattr(self._runner, "knowledge_base", None)
        report = None
        if hasattr(self._runner, "generate_report"):
            try:
                report = self._runner.generate_report()
            except Exception:  # noqa: BLE001  # pragma: no cover - report generation is optional in tests
                report = None
        return {
            "session_id": self.session_id,
            "topic": self.topic,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "knowledge_base": knowledge_base.to_dict() if knowledge_base and hasattr(knowledge_base, "to_dict") else {},
            "report": report,
        }

    def _persist_meta(self) -> None:
        """Store the latest session metadata."""
        meta = {
            "session_id": self.session_id,
            "topic": self.topic,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "auth_digest": self._auth_digest,
            "runner_overrides": self._runner_overrides,
        }
        self._storage.store_meta(self.session_id, meta)

    async def close(self) -> None:
        """Terminate the session and cancel background tasks."""
        if self._closed:
            return
        self._closed = True
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self._publish_system_event("session.closed", {"session_id": self.session_id})

    # -----------------------------
    # Utility conversions
    # -----------------------------
    @staticmethod
    def _turn_to_payload(turn: Any, role: str) -> Dict[str, Any]:
        if hasattr(turn, "to_dict"):
            payload = turn.to_dict()
        elif isinstance(turn, dict):
            payload = dict(turn)
        else:
            payload = {
                "role": role,
                "utterance": getattr(turn, "utterance", getattr(turn, "raw_utterance", "")),
            }
        payload.setdefault("role", role)
        payload.setdefault("raw_utterance", payload.get("utterance", ""))
        return payload


class SessionManager:
    """Coordinates creation, access, and teardown of conversation sessions."""

    def __init__(
        self,
        *,
        runner_factory,
        executor,
        storage: ConversationStorage,
        logger: Optional[logging.Logger] = None,
        max_event_history: int = 512,
    ) -> None:
        """Initialize the manager."""
        self._runner_factory = runner_factory
        self._executor = executor
        self._storage = storage
        self._logger = logger or logging.getLogger(__name__)
        self._max_event_history = max_event_history
        self._sessions: Dict[str, ConversationSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        *,
        api_key: str,
        topic: str,
        user_id: str,
        runner_overrides: Optional[Dict[str, Any]] = None,
    ) -> ConversationSession:
        """Create a new conversational session and kick off warm start asynchronously."""
        session_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        runner = self._runner_factory(api_key, topic, runner_overrides, None)
        session_logger = self._logger.getChild(session_id)
        session = ConversationSession(
            session_id=session_id,
            api_key=api_key,
            user_id=user_id,
            topic=topic,
            runner=runner,
            runner_overrides=runner_overrides,
            storage=self._storage,
            executor=self._executor,
            loop=loop,
            logger=session_logger,
            max_event_history=self._max_event_history,
        )
        self._sessions[session_id] = session
        meta = {
            "session_id": session_id,
            "topic": topic,
            "user_id": user_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "auth_digest": session.auth_digest,
            "runner_overrides": runner_overrides or {},
        }
        self._storage.store_meta(session_id, meta)
        self._storage.store_state(session_id, session.snapshot().to_dict())
        return session

    async def get_session(self, session_id: str, api_key: str) -> ConversationSession:
        """Retrieve an existing session, restoring it from storage if needed."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.validate_api_key(api_key)
            return session
        meta = self._storage.load_meta(session_id)
        if meta is None:
            raise KeyError("Session not found")
        if meta.get("auth_digest") != _hash_api_key(session_id, api_key):
            raise PermissionError("Invalid API key for session")
        loop = asyncio.get_event_loop()
        runner_overrides = meta.get("runner_overrides") or {}
        restored_state_dict = self._storage.load_state(session_id)
        restored_state = (
            SessionState.from_dict(restored_state_dict)
            if restored_state_dict
            else None
        )
        runner = self._runner_factory(
            api_key,
            meta.get("topic", ""),
            runner_overrides,
            restored_state,
        )
        initial_events = self._storage.load_events(session_id)
        session_logger = self._logger.getChild(session_id)
        session = ConversationSession(
            session_id=session_id,
            api_key=api_key,
            user_id=meta.get("user_id", ""),
            topic=meta.get("topic", ""),
            runner=runner,
            runner_overrides=runner_overrides,
            storage=self._storage,
            executor=self._executor,
            loop=loop,
            logger=session_logger,
            max_event_history=self._max_event_history,
            restored_state=restored_state,
            auth_digest=meta.get("auth_digest"),
            initial_events=initial_events,
        )
        self._sessions[session_id] = session
        return session

    async def enqueue_message(
        self, session_id: str, api_key: str, message: str
    ) -> None:
        """Append a user message to the conversation."""
        session = await self.get_session(session_id, api_key)
        await session.enqueue_user_message(message)
        self._storage.store_state(session_id, session.snapshot().to_dict())

    async def get_updates(
        self,
        session_id: str,
        api_key: str,
        *,
        after_event_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> List[ConversationEvent]:
        """Fetch events after a given event identifier, optionally waiting for new updates."""
        session = await self.get_session(session_id, api_key)
        events = session.get_events_after(after_event_id)
        if events:
            return events
        await session.wait_for_events(after_event_id, timeout)
        return session.get_events_after(after_event_id)

    async def get_view(self, session_id: str, api_key: str) -> Dict[str, Any]:
        """Fetch a compact view of the session state."""
        session = await self.get_session(session_id, api_key)
        return session.to_view()

    async def delete_session(self, session_id: str, api_key: str) -> None:
        """Remove a session from memory and storage."""
        session = await self.get_session(session_id, api_key)
        await session.close()
        self._sessions.pop(session_id, None)
        self._storage.delete_events(session_id)
        self._storage.delete_meta(session_id)
        self._storage.delete_state(session_id)

    async def shutdown(self) -> None:
        """Terminate all tracked sessions."""
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
