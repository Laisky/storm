import os
from concurrent.futures import ThreadPoolExecutor
from asyncio import get_event_loop
from typing import Dict, Optional
import datetime
from logging import Logger

from aiohttp import web
import aiohttp
from kipp.utils import setup_logger
from kipp.redis.utils import RedisUtils
import redis

from knowledge_storm.collaborative_storm.engine import (
    CollaborativeStormLMConfigs,
)
from knowledge_storm.lm import OpenAIModel
from knowledge_storm.rm import BingSearch
from knowledge_storm.storm_wiki.engine import (
    STORMWikiLMConfigs,
    STORMWikiRunner,
    STORMWikiRunnerArguments,
)
from knowledge_storm.server.tasks import (
    get_llm_storm_task_blocking,
    TASK_STATUS_SUCCESS,
    TASK_STATUS_FAILED,
    StormTask,
    upload_llm_storm_result,
)
from knowledge_storm.server.prd import REDIS_HOST, REDIS_PORT, REDIS_DB

logger = setup_logger(__name__)
executor = ThreadPoolExecutor(max_workers=20)


def create_storm_wiki_runner(apikey: str):
    api_base = os.getenv("OPENAI_API_BASE", "https://oneapi.laisky.com")
    api_base = api_base.removesuffix("/").removesuffix("/v1") + "/v1/"

    engine_args = STORMWikiRunnerArguments(
        output_dir="temp",
        max_conv_turn=3,
        max_perspective=3,
        search_top_k=3,
        max_thread_num=10,
    )
    openai_kwargs = {
        "api_key": apikey,
        "api_provider": "openai",
        "temperature": 1.0,
        "top_p": 0.9,
        "api_base": api_base,
    }
    ModelClass = OpenAIModel
    model_name = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
    max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "1000"))

    question_answering_lm = ModelClass(
        model=model_name, max_tokens=max_tokens, **openai_kwargs
    )
    lm_config = CollaborativeStormLMConfigs()
    lm_config.set_question_answering_lm(question_answering_lm)

    lm_configs = STORMWikiLMConfigs()
    conv_simulator_lm = ModelClass(
        model="gpt-4o-mini", max_tokens=1000, **openai_kwargs
    )
    question_asker_lm = ModelClass(
        model="gpt-4o-mini", max_tokens=1000, **openai_kwargs
    )
    outline_gen_lm = ModelClass(model="gpt-4o-mini", max_tokens=1000, **openai_kwargs)
    article_gen_lm = ModelClass(model="gpt-4o-mini", max_tokens=3000, **openai_kwargs)
    article_polish_lm = ModelClass(
        model="gpt-4o-mini", max_tokens=4000, **openai_kwargs
    )
    lm_configs.set_conv_simulator_lm(conv_simulator_lm)
    lm_configs.set_question_asker_lm(question_asker_lm)
    lm_configs.set_outline_gen_lm(outline_gen_lm)
    lm_configs.set_article_gen_lm(article_gen_lm)
    lm_configs.set_article_polish_lm(article_polish_lm)

    rm = BingSearch(
        bing_search_api=os.getenv("BING_SEARCH_API_KEY"), k=3, safe_search="Off"
    )
    # rm = DuckDuckGoSearchRM(k=5, safe_search="Off", region="us-en")

    runner = STORMWikiRunner(engine_args, lm_configs, rm)
    return runner


async def handle_enhance(request: aiohttp.web.Request) -> aiohttp.web.Response:
    data = await request.json()
    prompt = data.get("prompt", "")
    logger.info(f"received prompt: {prompt}")

    auth_header = request.headers.get("Authorization", "")
    apikey = auth_header.removeprefix("Bearer ")

    assert apikey, "apikey is required"

    loop = get_event_loop()
    result = await loop.run_in_executor(executor, run_storm_wiki, apikey, prompt)
    return web.json_response(result)


def run_task_worker():
    rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    rutils = RedisUtils(rdb, logger.getChild("redis_utils"))

    for i in range(10):
        executor.submit(_task_worker, logger.getChild(f"worker_{i}"), rutils)


def _task_worker(logger: Logger, rutils: RedisUtils):
    logger.info(f"task worker started")

    task: Optional[StormTask] = None
    while True:
        try:
            task = get_llm_storm_task_blocking(rutils)
            logger.info(f"get task from rdb, {task.task_id=}")
            result = run_storm_wiki(task.api_key, task.prompt)

            task.status = TASK_STATUS_SUCCESS
            task.finished_at = datetime.datetime.now().isoformat()
            task.result_article = result["article"]
            task.result_references = result["references"]
        except Exception as err:
            logger.exception("Error in task worker")

            if task:
                task.status = TASK_STATUS_FAILED
                task.finished_at = datetime.datetime.now().isoformat()
                task.failed_reason = str(err)

        try:
            if task:
                upload_llm_storm_result(rutils, task)
        except Exception:
            logger.exception("Error in uploading task result")


def run_storm_wiki(apikey: str, prompt: str) -> Dict:
    runner = create_storm_wiki_runner(apikey)
    result = runner.run(
        topic=prompt,
        do_research=True,
        do_generate_outline=True,
        do_generate_article=True,
        do_polish_article=True,
    )

    runner.post_run()
    runner.summary()
    runner.clean()

    return result


def create_app():
    app = web.Application()
    app.router.add_post("/enhance", handle_enhance)
    return app


if __name__ == "__main__":
    run_task_worker()
    logger.info(f"server started at :8080")
    web.run_app(create_app(), port=8080)
