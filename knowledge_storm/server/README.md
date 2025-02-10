# Deep-Research Web Service

You can quickly deploy the co-storm deep-research HTTP web service using docker-compose.

```yml
  llmstorm:
    image: ppcelery/llm-storm:latest
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
    ports:
      # HTTP POST /deep-research {"prompt": "PUT YOUR QUERY HERE"}
      # with HEADER "Authorization: Bearer YOUR_API_KEY"
      - "100.97.108.34:22083:8080"
    env_file:
      # set BING_SEARCH_API_KEY in env file
      - /opt/configs/env/home/storm.env
    environment:
      OPENAI_API_TYPE: openai
      OPENAI_MAX_TOKENS: 4000
      OPENAI_MODEL_NAME: gemini-2.0-flash-001
      OPENAI_API_BASE: https://oneapi.laisky.com/v1/
      # (optional) set REDIS_HOST to activate the Redis-based async task worker
      REDIS_HOST: 100.122.41.16
```
