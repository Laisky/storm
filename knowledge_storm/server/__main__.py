import asyncio
import contextlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from logging import Logger
from typing import Any, Dict, Optional

import aiohttp
import redis
from aiohttp import web

from kipp.redis.utils import RedisUtils
from kipp.utils import setup_logger

from knowledge_storm.collaborative_storm.engine import (
    CollaborativeStormLMConfigs,
    CoStormRunner,
    RunnerArgument,
)
from knowledge_storm.dataclass import ConversationTurn, KnowledgeBase
from knowledge_storm.logging_wrapper import LoggingWrapper
from knowledge_storm.lm import OpenAIModel
from knowledge_storm.rm import BingSearch
from knowledge_storm.server.sessions import (
    ConversationEvent,
    ConversationStorage,
    SessionManager,
    SessionState,
)
from knowledge_storm.server.tasks import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS,
    StormTask,
    get_llm_storm_task_blocking,
    upload_llm_storm_result,
)
from knowledge_storm.server.utils import utcnow
from knowledge_storm.storm_wiki.engine import (
    STORMWikiLMConfigs,
    STORMWikiRunner,
    STORMWikiRunnerArguments,
)

logger = setup_logger(__name__)
executor = ThreadPoolExecutor(max_workers=20)


# =====================================
# Environments
# =====================================
os.environ.setdefault("OPENAI_API_TYPE", "openai")
OPENAI_API_BASE = (
    os.getenv("OPENAI_API_BASE", "https://api.openai.com").rstrip("/").rstrip("/v1")
    + "/v1/"
)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1000"))
OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")

REDIS_HOST = os.getenv("REDIS_HOST", "")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

BING_SEARCH_API_KEY = os.getenv("BING_SEARCH_API_KEY", "")
assert BING_SEARCH_API_KEY, "BING_SEARCH_API_KEY is required"

# STORM wiki defaults (re-used from legacy server behaviour)
COSTORM_MAX_CONV_TURN = int(os.getenv("COSTORM_MAX_CONV_TURN", "3"))
COSTORM_MAX_PERSPECTIVE = int(os.getenv("COSTORM_MAX_PERSPECTIVE", "3"))
COSTORM_MAX_SEARCH_TOP_K = int(os.getenv("COSTORM_MAX_SEARCH_TOP_K", "3"))

# Conversational service defaults
COSTORM_RETRIEVE_TOP_K = int(os.getenv("COSTORM_RETRIEVE_TOP_K", "6"))
COSTORM_TOTAL_CONV_TURN = int(os.getenv("COSTORM_TOTAL_CONV_TURN", "20"))
COSTORM_MAX_SEARCH_QUERIES = int(os.getenv("COSTORM_MAX_SEARCH_QUERIES", "2"))
COSTORM_MAX_SEARCH_THREAD = int(os.getenv("COSTORM_MAX_SEARCH_THREAD", "5"))
COSTORM_MAX_SEARCH_QUERIES_PER_TURN = int(os.getenv("COSTORM_MAX_SEARCH_QUERIES_PER_TURN", "3"))
COSTORM_WARMSTART_MAX_NUM_EXPERTS = int(os.getenv("COSTORM_WARMSTART_MAX_NUM_EXPERTS", "3"))
COSTORM_WARMSTART_MAX_TURN_PER_EXPERT = int(os.getenv("COSTORM_WARMSTART_MAX_TURN_PER_EXPERT", "2"))
COSTORM_WARMSTART_MAX_THREAD = int(os.getenv("COSTORM_WARMSTART_MAX_THREAD", "3"))
COSTORM_MAX_THREAD_NUM = int(os.getenv("COSTORM_MAX_THREAD_NUM", "10"))
COSTORM_MAX_NUM_ROUND_TABLE_EXPERTS = int(os.getenv("COSTORM_MAX_NUM_ROUND_TABLE_EXPERTS", "2"))
COSTORM_MODERATOR_OVERRIDE = int(os.getenv("COSTORM_MODERATOR_OVERRIDE", "3"))
COSTORM_NODE_EXPANSION_TRIGGER = int(os.getenv("COSTORM_NODE_EXPANSION_TRIGGER", "10"))
COSTORM_MAX_EVENT_HISTORY = int(os.getenv("COSTORM_MAX_EVENT_HISTORY", "512"))
COSTORM_EVENT_TTL_SECONDS = int(os.getenv("COSTORM_EVENT_TTL_SECONDS", "3600"))
COSTORM_SESSION_TTL_SECONDS = int(os.getenv("COSTORM_SESSION_TTL_SECONDS", "86400"))
COSTORM_STREAM_HEARTBEAT_SECONDS = float(os.getenv("COSTORM_STREAM_HEARTBEAT_SECONDS", "30"))
# =====================================

redis_client: Optional[redis.Redis] = None
if REDIS_HOST:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)


def create_storm_wiki_runner(apikey: str) -> STORMWikiRunner:
    """Instantiate the legacy STORM Wiki runner for deep research requests."""
    engine_args = STORMWikiRunnerArguments(
        output_dir="temp",
        max_conv_turn=COSTORM_MAX_CONV_TURN,
        max_perspective=COSTORM_MAX_PERSPECTIVE,
        search_top_k=COSTORM_MAX_SEARCH_TOP_K,
        max_thread_num=10,
    )
    openai_kwargs = {
        "model_type": "chat",
        "api_key": apikey,
        "api_provider": "openai",
        "temperature": 1.0,
        "top_p": 0.9,
        "api_base": OPENAI_API_BASE,
    }

    ModelClass = OpenAIModel

    question_answering_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    lm_config = CollaborativeStormLMConfigs()
    lm_config.set_question_answering_lm(question_answering_lm)

    lm_configs = STORMWikiLMConfigs()
    conv_simulator_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    question_asker_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    outline_gen_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    article_gen_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    article_polish_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    lm_configs.set_conv_simulator_lm(conv_simulator_lm)
    lm_configs.set_question_asker_lm(question_asker_lm)
    lm_configs.set_outline_gen_lm(outline_gen_lm)
    lm_configs.set_article_gen_lm(article_gen_lm)
    lm_configs.set_article_polish_lm(article_polish_lm)

    rm = BingSearch(bing_search_api=BING_SEARCH_API_KEY, k=3, safe_search="Off")
    return STORMWikiRunner(engine_args, lm_configs, rm)


def _build_runner_argument(topic: str, overrides: Optional[Dict[str, Any]]) -> RunnerArgument:
    """Create RunnerArgument from defaults merged with user overrides."""
    base_kwargs: Dict[str, Any] = {
        "topic": topic,
        "retrieve_top_k": COSTORM_RETRIEVE_TOP_K,
        "max_search_queries": COSTORM_MAX_SEARCH_QUERIES,
        "total_conv_turn": COSTORM_TOTAL_CONV_TURN,
        "max_search_thread": COSTORM_MAX_SEARCH_THREAD,
        "max_search_queries_per_turn": COSTORM_MAX_SEARCH_QUERIES_PER_TURN,
        "warmstart_max_num_experts": COSTORM_WARMSTART_MAX_NUM_EXPERTS,
        "warmstart_max_turn_per_experts": COSTORM_WARMSTART_MAX_TURN_PER_EXPERT,
        "warmstart_max_thread": COSTORM_WARMSTART_MAX_THREAD,
        "max_thread_num": COSTORM_MAX_THREAD_NUM,
        "max_num_round_table_experts": COSTORM_MAX_NUM_ROUND_TABLE_EXPERTS,
        "moderator_override_N_consecutive_answering_turn": COSTORM_MODERATOR_OVERRIDE,
        "node_expansion_trigger_count": COSTORM_NODE_EXPANSION_TRIGGER,
    }
    if overrides:
        for key, value in overrides.items():
            if key == "topic":
                continue
            if key in RunnerArgument.__dataclass_fields__:
                base_kwargs[key] = value
    return RunnerArgument(**base_kwargs)


def create_costorm_runner(
    api_key: str,
    topic: str,
    runner_overrides: Optional[Dict[str, Any]],
    restored_state: Optional[SessionState],
) -> CoStormRunner:
    """Instantiate a Co-STORM runner with optional restored state."""
    runner_argument = _build_runner_argument(topic, runner_overrides)

    openai_kwargs = {
        "model_type": "chat",
        "api_key": api_key,
        "api_provider": "openai",
        "temperature": 1.0,
        "top_p": 0.9,
        "api_base": OPENAI_API_BASE,
    }
    ModelClass = OpenAIModel

    question_answering_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    discourse_manage_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    utterance_polishing_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    warmstart_outline_gen_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    question_asking_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )
    knowledge_base_lm = ModelClass(
        model=OPENAI_MODEL_NAME, max_tokens=OPENAI_MAX_TOKENS, **openai_kwargs
    )

    lm_config = CollaborativeStormLMConfigs()
    lm_config.set_question_answering_lm(question_answering_lm)
    lm_config.set_discourse_manage_lm(discourse_manage_lm)
    lm_config.set_utterance_polishing_lm(utterance_polishing_lm)
    lm_config.set_warmstart_outline_gen_lm(warmstart_outline_gen_lm)
    lm_config.set_question_asking_lm(question_asking_lm)
    lm_config.set_knowledge_base_lm(knowledge_base_lm)

    logging_wrapper = LoggingWrapper(lm_config)
    rm = BingSearch(
        bing_search_api=BING_SEARCH_API_KEY,
        k=runner_argument.retrieve_top_k,
        safe_search="Off",
    )

    runner = CoStormRunner(
        lm_config=lm_config,
        runner_argument=runner_argument,
        logging_wrapper=logging_wrapper,
        rm=rm,
    )

    if restored_state:
        if restored_state.conversation_history:
            runner.conversation_history = [
                ConversationTurn.from_dict(turn)
                for turn in restored_state.conversation_history
            ]
        if restored_state.warmstart_conv_archive:
            runner.warmstart_conv_archive = [
                ConversationTurn.from_dict(turn)
                for turn in restored_state.warmstart_conv_archive
            ]
        if restored_state.experts:
            runner.discourse_manager.deserialize_experts(restored_state.experts)
        if restored_state.knowledge_base:
            try:
                runner.knowledge_base = KnowledgeBase.from_dict(
                    data=restored_state.knowledge_base,
                    knowledge_base_lm=lm_config.knowledge_base_lm,
                    node_expansion_trigger_count=runner_argument.node_expansion_trigger_count,
                    encoder=runner.encoder,
                )
            except Exception:  # noqa: BLE001 - fall back to fresh mind map
                pass

    return runner


conversation_storage = ConversationStorage(
    redis_client,
    event_ttl_seconds=COSTORM_EVENT_TTL_SECONDS,
    session_ttl_seconds=COSTORM_SESSION_TTL_SECONDS,
    logger=logger.getChild("session_storage"),
)
session_manager = SessionManager(
    runner_factory=create_costorm_runner,
    executor=executor,
    storage=conversation_storage,
    logger=logger.getChild("sessions"),
    max_event_history=COSTORM_MAX_EVENT_HISTORY,
)


def extract_api_key(request: aiohttp.web.Request) -> str:
    """Extract and validate the Authorization bearer token."""
    header = request.headers.get("Authorization", "").strip()
    api_key = header.removeprefix("Bearer ").strip()
    if not api_key:
        raise web.HTTPUnauthorized(reason="Authorization header with Bearer token is required")
    return api_key


def _event_to_json(event: ConversationEvent) -> Dict[str, Any]:
    """Convert a ConversationEvent to a JSON-serializable dictionary."""
    return event.to_dict()


async def _write_sse_event(response: web.StreamResponse, event: ConversationEvent) -> None:
    """Send a single event over an SSE stream."""
    payload = json.dumps(event.to_dict())
    chunk = f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n".encode("utf-8")
    await response.write(chunk)
    await response.drain()


async def handle_deep_research(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Handle synchronous deep research article generation requests."""
    data = await request.json()
    prompt = data.get("prompt", "")
    logger.info("received deep research prompt: %s", prompt)

    apikey = extract_api_key(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, run_storm_wiki, apikey, prompt)
    return web.json_response(result)


async def handle_create_session(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Create a new conversational Co-STORM session."""
    api_key = extract_api_key(request)
    payload = await request.json()
    topic = payload.get("topic", "").strip()
    if not topic:
        raise web.HTTPBadRequest(reason="topic is required")
    user_id = payload.get("user_id", "anonymous")
    runner_overrides = payload.get("runner_overrides") or payload.get("runner") or {}

    session = await session_manager.create_session(
        api_key=api_key,
        topic=topic,
        user_id=user_id,
        runner_overrides=runner_overrides,
    )
    events = [_event_to_json(event) for event in session.get_events_after(None)]
    return web.json_response(
        {
            "session_id": session.session_id,
            "topic": session.topic,
            "created_at": session.created_at,
            "events": events,
        },
        status=201,
    )


async def handle_conversation_message(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Append a user utterance to an existing session."""
    api_key = extract_api_key(request)
    session_id = request.match_info["session_id"]
    payload = await request.json()
    message = payload.get("message", "").strip()
    if not message:
        raise web.HTTPBadRequest(reason="message is required")

    wait_for_response = bool(payload.get("wait_for_response", False))
    after_event_id = payload.get("after_event_id")
    if wait_for_response and after_event_id is None:
        raise web.HTTPBadRequest(reason="after_event_id is required when wait_for_response is true")
    wait_timeout = float(payload.get("wait_timeout_seconds", COSTORM_STREAM_HEARTBEAT_SECONDS))

    try:
        await session_manager.enqueue_message(session_id, api_key, message)
    except PermissionError as err:
        raise web.HTTPForbidden(reason=str(err)) from err
    except KeyError as err:
        raise web.HTTPNotFound(reason="session not found") from err

    events: list[ConversationEvent] = []
    if wait_for_response:
        events = await session_manager.get_updates(
            session_id,
            api_key,
            after_event_id=after_event_id,
            timeout=wait_timeout,
        )
    return web.json_response(
        {
            "session_id": session_id,
            "status": "completed" if wait_for_response else "queued",
            "events": [_event_to_json(event) for event in events],
        }
    )


async def handle_get_session(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Return session metadata and optionally its historical events."""
    api_key = extract_api_key(request)
    session_id = request.match_info["session_id"]
    include_events = request.rel_url.query.get("include_events", "false").lower() == "true"

    try:
        view = await session_manager.get_view(session_id, api_key)
    except PermissionError as err:
        raise web.HTTPForbidden(reason=str(err)) from err
    except KeyError as err:
        raise web.HTTPNotFound(reason="session not found") from err

    response: Dict[str, Any] = {"session": view}
    if include_events:
        events = await session_manager.get_updates(session_id, api_key)
        response["events"] = [_event_to_json(event) for event in events]
    return web.json_response(response)


async def handle_get_session_updates(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Fetch conversation events produced after a given cursor."""
    api_key = extract_api_key(request)
    session_id = request.match_info["session_id"]
    after_event_id = request.rel_url.query.get("after")
    timeout_param = request.rel_url.query.get("timeout")
    timeout = float(timeout_param) if timeout_param is not None else 0.0

    try:
        events = await session_manager.get_updates(
            session_id,
            api_key,
            after_event_id=after_event_id,
            timeout=timeout,
        )
    except PermissionError as err:
        raise web.HTTPForbidden(reason=str(err)) from err
    except KeyError as err:
        raise web.HTTPNotFound(reason="session not found") from err

    return web.json_response({"events": [_event_to_json(event) for event in events]})


async def handle_stream_session(request: aiohttp.web.Request) -> web.StreamResponse:
    """Stream live conversation events over Server-Sent Events."""
    api_key = extract_api_key(request)
    session_id = request.match_info["session_id"]

    try:
        await session_manager.get_session(session_id, api_key)
    except PermissionError as err:
        raise web.HTTPForbidden(reason=str(err)) from err
    except KeyError as err:
        raise web.HTTPNotFound(reason="session not found") from err

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    last_event_id = request.headers.get("Last-Event-ID") or request.rel_url.query.get("after")
    last_sent = last_event_id

    try:
        initial_events = await session_manager.get_updates(session_id, api_key, after_event_id=last_event_id)
        for event in initial_events:
            await _write_sse_event(response, event)
            last_sent = event.event_id
        while True:
            new_events = await session_manager.get_updates(
                session_id,
                api_key,
                after_event_id=last_sent,
                timeout=COSTORM_STREAM_HEARTBEAT_SECONDS,
            )
            if not new_events:
                await response.write(b": heartbeat\n\n")
                await response.drain()
                continue
            for event in new_events:
                await _write_sse_event(response, event)
                last_sent = event.event_id
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await response.write_eof()
    return response


async def handle_delete_session(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Tear down a session and release its resources."""
    api_key = extract_api_key(request)
    session_id = request.match_info["session_id"]
    try:
        await session_manager.delete_session(session_id, api_key)
    except PermissionError as err:
        raise web.HTTPForbidden(reason=str(err)) from err
    except KeyError as err:
        raise web.HTTPNotFound(reason="session not found") from err
    return web.json_response({"session_id": session_id, "status": "deleted"})


def run_task_redis_subscribers() -> None:
    """Run background workers that process queued deep-research tasks."""
    if not REDIS_HOST:
        logger.warning("REDIS_HOST is not set, task redis subscribers will not start")
        return

    rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    rutils = RedisUtils(rdb, logger.getChild("redis_utils"))

    for i in range(10):
        executor.submit(_redis_task_worker, logger.getChild(f"worker_{i}"), rutils)

    logger.info(
        "task redis subscribers started, listening to redis at %s:%s/%s",
        REDIS_HOST,
        REDIS_PORT,
        REDIS_DB,
    )


def _redis_task_worker(logger: Logger, rutils: RedisUtils) -> None:
    """Background worker loop that executes queued STORM wiki tasks."""
    logger.info("task worker started")

    task: Optional[StormTask] = None
    while True:
        try:
            task = get_llm_storm_task_blocking(rutils)
            assert task.status == TASK_STATUS_PENDING, "task status is not pending"
            logger.info("get task from rdb, task_id=%s", task.task_id)

            task.runner = "Co-STORM"

            task.status = TASK_STATUS_RUNNING
            upload_llm_storm_result(rutils, task)

            result = run_storm_wiki(task.api_key, task.prompt)

            task.status = TASK_STATUS_SUCCESS
            task.finished_at = utcnow()
            task.result_article = result["article"]
            task.result_references = result["references"]
        except Exception as err:  # noqa: BLE001 - worker must trap all failures
            logger.exception("Error in task worker")

            if task:
                task.status = TASK_STATUS_FAILED
                task.finished_at = utcnow()
                task.failed_reason = str(err)

        try:
            if task:
                upload_llm_storm_result(rutils, task)
        except Exception:  # noqa: BLE001 - logging already handled
            logger.exception("Error in uploading task result")


def run_storm_wiki(apikey: str, prompt: str) -> Dict[str, Any]:
    """Run the end-to-end STORM Wiki pipeline and return its results."""
    runner = create_storm_wiki_runner(apikey)
    try:
        result = runner.run(
            topic=prompt,
            do_research=True,
            do_generate_outline=True,
            do_generate_article=True,
            do_polish_article=True,
        )
    finally:
        runner.post_run()
        runner.summary()
        runner.clean()

    return result


def create_app() -> web.Application:
    """Build the aiohttp application exposing deep research and conversation APIs."""
    app = web.Application()
    app["session_manager"] = session_manager
    app.router.add_post("/deep-research", handle_deep_research)
    app.router.add_post("/conversation/sessions", handle_create_session)
    app.router.add_post("/conversation/sessions/{session_id}/messages", handle_conversation_message)
    app.router.add_get("/conversation/sessions/{session_id}", handle_get_session)
    app.router.add_get("/conversation/sessions/{session_id}/updates", handle_get_session_updates)
    app.router.add_get("/conversation/sessions/{session_id}/stream", handle_stream_session)
    app.router.add_delete("/conversation/sessions/{session_id}", handle_delete_session)

    async def _on_cleanup(application: web.Application) -> None:
        await application["session_manager"].shutdown()

    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    run_task_redis_subscribers()
    logger.info("server started at :8080")
    web.run_app(create_app(), port=8080)
