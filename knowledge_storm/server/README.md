# Co-STORM Web API Manual

## Contents

- [Co-STORM Web API Manual](#co-storm-web-api-manual)
  - [Contents](#contents)
  - [Overview](#overview)
  - [Authentication](#authentication)
  - [Deployment Options](#deployment-options)
    - [SaaS](#saas)
    - [Self Deploy](#self-deploy)
  - [Deep-Research Endpoint](#deep-research-endpoint)
    - [Request](#request)
    - [Response](#response)
  - [Conversational Co-STORM API](#conversational-co-storm-api)
    - [Session Lifecycle](#session-lifecycle)
    - [Create Session](#create-session)
    - [Post Message](#post-message)
    - [Poll Updates](#poll-updates)
    - [Stream Updates](#stream-updates)
    - [Get Session View](#get-session-view)
    - [Delete Session](#delete-session)
    - [Event Types](#event-types)
    - [Error Responses](#error-responses)
  - [Configuration Reference](#configuration-reference)
  - [Redis and Persistence](#redis-and-persistence)
  - [FAQ](#faq)


## Overview

The Co-STORM service exposes two LLM-driven workflows:

- **Deep Research** (`POST /deep-research`): runs a full research pipeline and returns a polished article plus citation metadata in one response.
- **Conversational Co-STORM** (`/conversation/...`): maintains a multi-turn collaborative research session with incremental updates, streaming output, and resumable state.

All timestamps are returned in UTC, and every response is JSON unless an endpoint explicitly uses Server-Sent Events (SSE).

## Authentication

- Supply `Authorization: Bearer <API_KEY>` on every request.
- Keys are hashed before storage; you can safely reuse the same key across endpoints.
- Requests missing the header receive `401 Unauthorized`.

## Deployment Options

### SaaS

Existing customers can call the hosted gateway immediately:

```sh
curl --location --globoff 'https://chat.laisky.com/deepresearch' \
    --header 'Content-Type: application/json' \
    --header 'Authorization: Bearer YOUR_ONEAPI_API_KEY' \
    --data '{"prompt": "YOUR QUESTION"}'
```

### Self Deploy

Run the published image to host your own gateway:

```yaml
llmstorm:
  image: ppcelery/llm-storm:latest
  restart: unless-stopped
  logging:
    driver: json-file
    options:
      max-size: 10m
  environment:
    OPENAI_API_TYPE: openai
    OPENAI_MAX_TOKENS: 4000
    OPENAI_MODEL_NAME: gemini-2.5-flash
    OPENAI_API_BASE: https://oneapi.laisky.com/v1/
    REDIS_HOST: 100.122.41.16 # optional, enables persistence
```

> Set `BING_SEARCH_API_KEY` inside the referenced environment file. Without Redis, the conversation APIs keep state only in memory and will reset on restart.

## Deep-Research Endpoint

### Request

`POST /deep-research`

```sh
curl --location 'https://YOUR_STORM_HOST/deep-research' \
    --header 'Content-Type: application/json' \
    --header 'Authorization: Bearer YOUR_API_KEY' \
    --data '{"prompt": "Explain the history of the Silk Road"}'
```

### Response

```json
{
  "article": "# Summary\n\n...",

  "references": {
    "url_to_unified_index": {},
    "url_to_info": {
      "https://example.com/resource": {
        "snippets": ["Excerpt 1"],
        "citation_uuid": -1
      }
    }
  }
}
```

The article is Markdown formatted. Reference maps provide metadata keyed by the canonical URL.

## Conversational Co-STORM API

### Session Lifecycle

1. **Create a session** with `POST /conversation/sessions`; warm-start tasks run asynchronously.
2. **Listen for warm-start events** via polling or streaming.
3. **Send user turns** using `POST /conversation/sessions/{id}/messages`.
4. **Consume updates** through `/updates` (long poll) or `/stream` (SSE).
5. **Inspect the session** using `GET /conversation/sessions/{id}` as needed.
6. **Close the session** with `DELETE /conversation/sessions/{id}` when finished.

Sessions expire automatically based on `COSTORM_SESSION_TTL_SECONDS`.

### Create Session

`POST /conversation/sessions`

| Body field         | Type   | Required | Description                                                             |
| ------------------ | ------ | -------- | ----------------------------------------------------------------------- |
| `topic`            | string | yes      | Research brief or problem statement.                                    |
| `user_id`          | string | no       | External user handle; defaults to `anonymous`.                          |
| `runner_overrides` | object | no       | Override fields accepted by `RunnerArgument` (model names, caps, etc.). |

Success (`201 Created`):

```json
{
  "session_id": "2f9b9699-4b0f-4d77-a62c-4d7c7c6b7bb0",
  "topic": "Next-gen battery materials",
  "created_at": "2025-10-18T13:25:40.123456Z",
  "events": [
    {
      "event_id": "1",
      "event_type": "session.created",
      "payload": {
        "session_id": "2f9b9699-4b0f-4d77-a62c-4d7c7c6b7bb0",
        "topic": "Next-gen battery materials",
        "user_id": "product-team"
      },
      "created_at": "2025-10-18T13:25:40.123456Z"
    }
  ]
}
```

Warm-start events continue streaming until the initial knowledge base and report scaffolding are prepared.

### Post Message

`POST /conversation/sessions/{session_id}/messages`

| Body field             | Type   | Required    | Description                                                                    |
| ---------------------- | ------ | ----------- | ------------------------------------------------------------------------------ |
| `message`              | string | yes         | User utterance.                                                                |
| `wait_for_response`    | bool   | no          | Block until new events arrive; default `false`.                                |
| `after_event_id`       | string | conditional | Required when `wait_for_response` is true; acts as cursor.                     |
| `wait_timeout_seconds` | number | no          | Max wait duration in seconds (defaults to `COSTORM_STREAM_HEARTBEAT_SECONDS`). |

Response:

```json
{
  "session_id": "2f9b9699-4b0f-4d77-a62c-4d7c7c6b7bb0",
  "status": "completed",
  "events": [
    {
      "event_id": "8",
      "event_type": "conversation.turn",
      "payload": {
        "role": "Guest",
        "utterance": "What are the latest silicon anode breakthroughs?"
      },
      "created_at": "2025-10-18T13:28:14.982341Z"
    }
  ]
}
```

If `wait_for_response` is false the call returns immediately with `status: "queued"` and an empty `events` list.

### Poll Updates

`GET /conversation/sessions/{session_id}/updates`

| Query param | Type   | Description                                                             |
| ----------- | ------ | ----------------------------------------------------------------------- |
| `after`     | string | Optional cursor (exclusive). When omitted, returns all buffered events. |
| `timeout`   | number | Optional long-poll timeout in seconds.                                  |

```sh
curl --get 'https://YOUR_STORM_HOST/conversation/sessions/SESSION_ID/updates' \
    --header 'Authorization: Bearer YOUR_API_KEY' \
    --data-urlencode 'after=12' \
    --data-urlencode 'timeout=10'
```

### Stream Updates

`GET /conversation/sessions/{session_id}/stream`

- Returns an SSE stream (`Content-Type: text/event-stream`).
- Heartbeat comments (`: heartbeat`) are sent every `COSTORM_STREAM_HEARTBEAT_SECONDS` seconds.
- Use `Last-Event-ID` or append `?after=EVENT_ID` to resume a dropped stream.

### Get Session View

`GET /conversation/sessions/{session_id}`

- Returns a snapshot of session metadata, latest knowledge base, and most recent report draft.
- Add `?include_events=true` to receive any buffered events in the same response.

```json
{
    "session": {
        "session_id": "2f9b9699-4b0f-4d77-a62c-4d7c7c6b7bb0",
        "topic": "Next-gen battery materials",
        "user_id": "product-team",
        "created_at": "2025-10-18T13:25:40.123456Z",
        "updated_at": "2025-10-18T13:28:15.410002Z",
        "warm_start_complete": true,
        "knowledge_base": {...},
        "report": "# Summary\n\n..."
    }
}
```

### Delete Session

`DELETE /conversation/sessions/{session_id}`

```json
{
  "session_id": "2f9b9699-4b0f-4d77-a62c-4d7c7c6b7bb0",
  "status": "deleted"
}
```

All cached state, including Redis entries, is purged.

### Event Types

| Event type               | Payload keys                                    | Description                                   |
| ------------------------ | ----------------------------------------------- | --------------------------------------------- |
| `session.created`        | `session_id`, `topic`, `user_id`                | Session bootstrap metadata.                   |
| `session.error`          | `message`, `retryable`                          | Background error; session may still continue. |
| `session.closed`         | `session_id`                                    | Session closed by client or TTL.              |
| `knowledge_base.updated` | Knowledge base tree                             | Latest knowledge base snapshot.               |
| `conversation.turn`      | `role`, `utterance`, `raw_utterance`, `sources` | Any turn authored by agents or users.         |

### Error Responses

| Status | Body                                                            | Meaning                                   |
| ------ | --------------------------------------------------------------- | ----------------------------------------- |
| `400`  | `{ "error": "invalid_request", "message": "details" }`          | Missing or malformed input.               |
| `401`  | `{ "error": "unauthorized" }`                                   | Missing or invalid API key.               |
| `403`  | `{ "error": "forbidden" }`                                      | API key does not match the session owner. |
| `404`  | `{ "error": "not_found" }`                                      | Unknown session id.                       |
| `409`  | `{ "error": "conflict", "message": "session already warming" }` | Duplicate warm-start attempt.             |
| `500`  | `{ "error": "internal", "message": "details" }`                 | Unhandled server failure.                 |

## Configuration Reference

| Key                                | Default                      | Description                                    |
| ---------------------------------- | ---------------------------- | ---------------------------------------------- |
| `OPENAI_API_TYPE`                  | `openai`                     | Provider name forwarded to LiteLLM/dspy.       |
| `OPENAI_API_BASE`                  | `https://api.openai.com/v1/` | Base URL for compatible REST calls.            |
| `OPENAI_MODEL_NAME`                | `gpt-4o-mini`                | Default chat model identifier.                 |
| `OPENAI_MAX_TOKENS`                | `1000`                       | Per-call generation cap for the primary model. |
| `BING_SEARCH_API_KEY`              | none                         | Required for retrieval-augmented search.       |
| `REDIS_HOST`                       | empty                        | Enables Redis-backed state when set.           |
| `REDIS_PORT`                       | `6379`                       | Redis port number.                             |
| `REDIS_DB`                         | `0`                          | Redis database index.                          |
| `COSTORM_RETRIEVE_TOP_K`           | `6`                          | Web results fetched per query.                 |
| `COSTORM_TOTAL_CONV_TURN`          | `20`                         | Maximum number of conversation turns.          |
| `COSTORM_MAX_SEARCH_QUERIES`       | `2`                          | Parallel search jobs per step.                 |
| `COSTORM_MAX_SEARCH_THREAD`        | `5`                          | Thread pool size for retrieval.                |
| `COSTORM_MAX_EVENT_HISTORY`        | `512`                        | Events stored per session in memory/Redis.     |
| `COSTORM_EVENT_TTL_SECONDS`        | `3600`                       | TTL for event lists when using Redis.          |
| `COSTORM_SESSION_TTL_SECONDS`      | `86400`                      | TTL before session metadata expires.           |
| `COSTORM_STREAM_HEARTBEAT_SECONDS` | `30`                         | SSE heartbeat interval.                        |
| `COSTORM_REPORT_MAX_TOKENS`        | `1500`                       | Token budget for article/report generation.    |

Defaults are defined in `knowledge_storm/server/__main__.py`; override them via environment variables or container configuration.

## Redis and Persistence

- When `REDIS_HOST` is configured, the service stores:
  - Session metadata (`costorm:sessions:{id}:meta`).
  - Serialized session state (`...:state`).
  - Ordered event streams (`...:events`).
- Redis also drives the background task queue for warm-start and long-running generation jobs.
- Without Redis, all state is held in-process; restarting the server clears every active session but `/deep-research` remains unaffected.

## FAQ

- **How do I resume a session after a client crash?** Fetch `GET /conversation/sessions/{id}` to confirm the session exists, then resume polling or streaming with the last processed `event_id`.
- **Can I change runner parameters mid-session?** No. Supply overrides when creating the session. To change them later, start a new session.
- **How do I detect warm-start completion?** Watch for a `knowledge_base.updated` event or check `warm_start_complete` in the session snapshot.
