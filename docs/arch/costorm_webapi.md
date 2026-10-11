# Co-STORM Web API Architecture

## Overview

- Co-STORM extends the STORM research pipeline with a collaborative discourse loop that alternates between simulated experts, a moderator, and a human participant to build grounded knowledge artifacts.
- The `knowledge_storm` Python package bundles the core engines (STORM Wiki + Co-STORM), retrievers, language-model abstractions, and logging utilities.
- `knowledge_storm/server` exposes the deep-research capabilities as an HTTP service, packaging the STORM Wiki pipeline into a stateless request/response API, and optionally coordinating asynchronous workloads via Redis.

## Existing Architecture

### Engine Layer (`knowledge_storm/`)

- **Language model orchestration**: `CollaborativeStormLMConfigs` and `STORMWikiLMConfigs` in `knowledge_storm/collaborative_storm/engine.py` and `knowledge_storm/storm_wiki/engine.py` define role-specific LLM instances (conversation simulators, question askers, outline/article generators, discourse managers, etc.). Litellm-backed wrappers (`knowledge_storm/lm.py`) provide a unified interface for OpenAI, Azure, Together, or other providers.
- **Retrieval layer**: Retriever implementations (e.g., `BingSearch` in `knowledge_storm/rm.py`) encapsulate search providers. The engines call retrievers during research turns to fetch grounded references.
- **Pipelines**: `STORMWikiRunner` drives the research→outline→article pipeline; `CoStormRunner` coordinates the collaborative conversation, manages expert agents, and maintains a knowledge base/mind map (`knowledge_storm/collaborative_storm/modules/*`). Both runners accept configuration dataclasses (`STORMWikiRunnerArguments`, `RunnerArgument`) to control search breadth, turn counts, threading, and optional modules.

### Web Service Layer (`knowledge_storm/server/`)

- **Entry point**: `__main__.py` bootstraps an `aiohttp` app with a `POST /deep-research` endpoint. It configures environment defaults (OpenAI host/model, Redis location, Bing API key) and launches a shared `ThreadPoolExecutor` for blocking workloads.
- **Request handling**: `handle_deep_research` extracts the JSON payload (`prompt`) and bearer token, defers heavy computation to `run_storm_wiki`, then returns the serialized article and citations.
- **Runner creation**: `create_storm_wiki_runner` assembles `STORMWikiRunnerArguments`, wires LLM instances (all currently `OpenAIModel` with identical temperature/top-p), and injects `BingSearch` as the retrieval module. The runner executes the full STORM Wiki pipeline (`run`, `post_run`, `summary`, `clean`).
- **Background task queue**: `run_task_redis_subscribers` (optional) registers ten executor workers that poll Redis for `StormTask` objects (`tasks.py`). Each task moves through `pending→running→success|failed`, with results stored back under a per-task Redis key. This enables deferred processing decoupled from HTTP requests.
- **Utilities**: `utils.py` provides timestamp helpers; `README.md` documents SaaS usage, curl examples, and Docker deployment hints.

### Data Flow

1. Client issues `POST /deep-research` with JSON `{ "prompt": "..." }` and `Authorization: Bearer <LM_API_KEY>` header.
2. Handler schedules `run_storm_wiki(prompt, api_key)` on the executor to avoid blocking the event loop.
3. Runner instantiation configures LMs (per role) and retriever, then executes research (perspective discovery, simulated interviews), outline generation, article drafting, and polishing.
4. Upon completion, the server returns `{ "article": str, "references": Dict[str, Dict], ... }`. Any configured Redis workers perform similar steps for queued background jobs.

## Usage Guide

- **Prerequisites**: Python environment with dependencies from `requirements.txt`; valid LM credentials (`OPENAI_API_KEY` or compatible) and a search key (`BING_SEARCH_API_KEY`). Optional Redis credentials enable task queue workers.
- **Configuration**: Environment variables accepted by `__main__.py` include `OPENAI_API_BASE`, `OPENAI_MODEL_NAME`, `OPENAI_MAX_TOKENS`, `BING_SEARCH_API_KEY`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, plus Co-STORM defaults (`COSTORM_MAX_*`).
- **Running the service**: `python -m knowledge_storm.server` launches the HTTP server on port 8080 and (if Redis host is set) starts task subscribers.
- **Synchronous usage**:
  ```bash
  curl --location 'http://localhost:8080/deep-research' \
  	--header 'Authorization: Bearer YOUR_OPENAI_KEY' \
  	--header 'Content-Type: application/json' \
  	--data '{"prompt": "History of electric aviation"}'
  ```
  The response includes a polished article plus structured references suitable for downstream presentation.
- **Queued usage**: When Redis is configured, clients can enqueue jobs via helper endpoints (to be added) or external producers using `add_llm_storm_task`. Workers call `upload_llm_storm_result` to persist outputs for later retrieval.

## Planned Enhancements: Conversational Web API

### Goals

- **Conversational co-pilot**: Allow clients to open long-lived conversations that leverage `CoStormRunner` for iterative question answering, grounded web search, and mind-map updates.
- **Session isolation**: Support concurrent multi-user dialogues. Each session must maintain independent history, knowledge base, and agent state without leaking sensitive data.
- **Asynchronous interaction**: Users can send prompts and receive streamed or polling-based updates as the discourse evolves (expert turns, moderator interventions, knowledge base growth).

### Proposed Architecture Extensions

- **Session Manager** (`knowledge_storm/server/sessions.py`):
  - Holds active session metadata (`session_id`, `user_id`, `topic`, timestamps, status flags).
  - Stores serialized `CoStormRunner` state (conversation history, knowledge base snapshot, LM usage) in Redis to enable horizontal scaling and crash recovery.
  - Provides lifecycle hooks: `create_session`, `append_message`, `get_updates`, `end_session`.
- **Conversation Workers**:
  - Extend the-existing `ThreadPoolExecutor` or spawn `asyncio.Task`s dedicated to conversation loops.
  - Each worker owns a `CoStormRunner`, processes inbound user utterances, advances the discourse (moderator + expert turns), and pushes incremental events to a queue (Redis stream or in-memory async queue) keyed by `session_id`.
  - Utilize `CollaborativeStormLMConfigs` and `RunnerArgument` tailored for interactive cadence (shorter `total_conv_turn`, explicit warm-start parameters).
- **Event Delivery**:
  - **Option A (recommended)**: Implement Server-Sent Events (`aiohttp_sse`) or WebSocket endpoint (`/conversation/{session_id}/stream`) to push turn-by-turn updates to the client in real time.
  - **Option B**: Provide polling endpoints (`GET /conversation/{session_id}/updates?since=<cursor>`) returning batched events persisted in Redis (`XADD`/`XRANGE`).
- **API Surface**:
  1.  `POST /conversation/sessions` → create session (inputs: topic, optional config, user id). Returns `session_id` and initial warm-start outline.
  2.  `POST /conversation/sessions/{session_id}/messages` → submit user utterance; enqueues work item for associated worker.
  3.  `GET /conversation/sessions/{session_id}` → fetch session metadata, latest mind map, article drafts.
  4.  Streaming/polling endpoint (per event delivery design) for incremental updates.
  5.  `DELETE /conversation/sessions/{session_id}` → teardown session, release resources, purge cached state.

### Concurrency & Isolation Strategy

- **Per-session locks**: Guard session state with asyncio locks or Redis distributed locks to avoid race conditions when multiple HTTP requests modify the same `session_id` in quick succession.
- **Context encapsulation**: `ConversationSession` objects should contain their own `CoStormRunner`, LM configs, retriever instances, and loggers; these objects must not be shared across sessions.
- **Resource management**: Limit concurrent sessions via semaphore or configurable quotas. When a worker finishes, call `runner.collect_and_reset_lm_usage()` to record tokens and `runner.clean()` to release caches.
- **Persistence**: Store minimal session snapshot (conversation turns, knowledge base nodes, outstanding tasks) in Redis hashes keyed by `session_id` for continuity across server restarts.

### Implementation Roadmap

- **Phase 1 – Infrastructure**: Introduce session management module, expand configuration to choose between synchronous article generation and conversational mode.
- **Phase 2 – API Endpoints**: Add REST + streaming endpoints, including validation, error responses, and OpenAPI-style documentation in `docs/`.
- **Phase 3 – Worker Integration**: Reuse existing executor/Redis queue to process conversational turns asynchronously; ensure background workers distinguish between report-generation tasks and conversation turn tasks (different Redis namespaces).
- **Phase 4 – Testing & Monitoring**: Add integration tests covering concurrent sessions, persistence, and failure recovery. Instrument with logging/metrics (per-session latency, LM usage) to monitor load.

With these enhancements, the web API will deliver both single-shot deep research reports and ongoing Co-STORM conversations while maintaining strict isolation and scalability.
