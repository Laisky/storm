import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from knowledge_storm.server.sessions import ConversationStorage, SessionManager, SessionState


class _StubKnowledgeBase:
    """Minimal knowledge base stub for testing session persistence."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.turns = []

    def update_from_conv_turn(self, conv_turn: Any, **_: Any) -> None:
        self.turns.append(conv_turn.to_dict())

    def reorganize(self) -> None:  # pragma: no cover - no-op for stub
        return

    def to_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic, "turns": list(self.turns)}


class _StubTurn:
    """ConversationTurn stand-in with stable serialization."""

    def __init__(self, role: str, utterance: str) -> None:
        self.role = role
        self.role_description = ""
        self.raw_utterance = utterance
        self.utterance = utterance
        self.utterance_type = "Statement"
        self.claim_to_make = ""
        self.queries: list[str] = []
        self.raw_retrieved_info: list[Dict[str, Any]] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "role_description": self.role_description,
            "raw_utterance": self.raw_utterance,
            "utterance": self.utterance,
            "utterance_type": self.utterance_type,
            "claim_to_make": self.claim_to_make,
            "queries": list(self.queries),
            "raw_retrieved_info": list(self.raw_retrieved_info),
        }


class _StubRunnerArgument:
    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.retrieve_top_k = 3
        self.node_expansion_trigger_count = 2

    def to_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic}


class _StubDiscourseManager:
    def __init__(self) -> None:
        self.experts: list[Dict[str, Any]] = []

    def serialize_experts(self) -> list[Dict[str, Any]]:
        return list(self.experts)

    def deserialize_experts(self, experts: list[Dict[str, Any]]) -> None:
        self.experts = list(experts)


class _StubRunner:
    """Simplified Co-STORM runner for unit testing the session manager."""

    def __init__(self, topic: str) -> None:
        self.runner_argument = _StubRunnerArgument(topic)
        self.conversation_history: list[_StubTurn] = []
        self.warmstart_conv_archive: list[_StubTurn] = []
        self.discourse_manager = _StubDiscourseManager()
        self.knowledge_base = _StubKnowledgeBase(topic)
        self._system_turn_counter = 0

    def warm_start(self) -> None:
        turn = _StubTurn("Moderator", f"Warm start for {self.runner_argument.topic}")
        self.conversation_history.append(turn)
        self.knowledge_base.update_from_conv_turn(turn)

    def step(self, user_utterance: str = "") -> _StubTurn:
        if user_utterance:
            turn = _StubTurn("Guest", user_utterance)
            self.conversation_history.append(turn)
            return turn
        self._system_turn_counter += 1
        turn = _StubTurn("Expert", f"Response {self._system_turn_counter}")
        self.conversation_history.append(turn)
        self.knowledge_base.update_from_conv_turn(turn)
        return turn

    def generate_report(self) -> str:
        return " | ".join(turn.raw_utterance for turn in self.conversation_history)


def _stub_runner_factory(
    api_key: str,
    topic: str,
    overrides: Optional[Dict[str, Any]],
    restored_state: Optional[SessionState],
) -> _StubRunner:
    del api_key, overrides
    runner = _StubRunner(topic)
    if restored_state:
        if restored_state.conversation_history:
            runner.conversation_history = [
                _StubTurn(turn.get("role", ""), turn.get("raw_utterance", ""))
                for turn in restored_state.conversation_history
            ]
        if restored_state.warmstart_conv_archive:
            runner.warmstart_conv_archive = [
                _StubTurn(turn.get("role", ""), turn.get("raw_utterance", ""))
                for turn in restored_state.warmstart_conv_archive
            ]
        if restored_state.experts:
            runner.discourse_manager.deserialize_experts(restored_state.experts)
        if restored_state.knowledge_base:
            kb_turns = restored_state.knowledge_base.get("turns", [])
            runner.knowledge_base.turns = kb_turns  # type: ignore[attr-defined]
    return runner


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the asynchronous session manager using stubbed dependencies."""

    async def asyncSetUp(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.storage = ConversationStorage()
        self.manager = SessionManager(
            runner_factory=_stub_runner_factory,
            executor=self.executor,
            storage=self.storage,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.executor.shutdown(wait=True)

    async def test_create_session_emits_warm_start_events(self) -> None:
        session = await self.manager.create_session(
            api_key="key",
            topic="Test Topic",
            user_id="user",
            runner_overrides=None,
        )
        await asyncio.wait_for(session._warm_start_complete.wait(), timeout=2.0)  # type: ignore[attr-defined]
        events = await self.manager.get_updates(session.session_id, "key")
        event_types = {event.event_type for event in events}
        self.assertIn("session.created", event_types)
        self.assertIn("conversation.turn", event_types)
        self.assertIn("knowledge_base.updated", event_types)

    async def test_enqueue_message_generates_system_turn(self) -> None:
        session = await self.manager.create_session(
            api_key="key",
            topic="Interactive",
            user_id="user",
            runner_overrides=None,
        )
        await asyncio.wait_for(session._warm_start_complete.wait(), timeout=2.0)  # type: ignore[attr-defined]
        initial_events = await self.manager.get_updates(session.session_id, "key")
        last_event_id = initial_events[-1].event_id if initial_events else None

        await self.manager.enqueue_message(session.session_id, "key", "Hello")
        new_events = await self.manager.get_updates(
            session.session_id,
            "key",
            after_event_id=last_event_id,
            timeout=1.0,
        )
        turn_events = [event for event in new_events if event.event_type == "conversation.turn"]
        self.assertGreaterEqual(len(turn_events), 2)
        payload_roles = [event.payload.get("role") for event in turn_events]
        self.assertIn("Guest", payload_roles)
        self.assertIn("Expert", payload_roles)

    async def test_session_state_persist_and_restore(self) -> None:
        session = await self.manager.create_session(
            api_key="key",
            topic="Persistence",
            user_id="user",
            runner_overrides=None,
        )
        await asyncio.wait_for(session._warm_start_complete.wait(), timeout=2.0)  # type: ignore[attr-defined]
        await self.manager.enqueue_message(session.session_id, "key", "Follow up")
        await asyncio.sleep(0.1)

        # Spin up a fresh manager sharing the same storage to validate restoration.
        await self.manager.shutdown()
        self.executor.shutdown(wait=True)

        new_executor = ThreadPoolExecutor(max_workers=2)
        new_manager = SessionManager(
            runner_factory=_stub_runner_factory,
            executor=new_executor,
            storage=self.storage,
        )
        try:
            restored_session = await new_manager.get_session(session.session_id, "key")
            self.assertGreaterEqual(len(restored_session.runner.conversation_history), 2)  # type: ignore[attr-defined]
        finally:
            await new_manager.shutdown()
            new_executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
