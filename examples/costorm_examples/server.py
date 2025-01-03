import os

from aiohttp import web
from kipp.utils import setup_logger

from knowledge_storm.collaborative_storm.engine import (
    CollaborativeStormLMConfigs,
)
from knowledge_storm.lm import OpenAIModel
from knowledge_storm.rm import BingSearch, DuckDuckGoSearchRM
from knowledge_storm.storm_wiki.engine import (
    STORMWikiLMConfigs,
    STORMWikiRunner,
    STORMWikiRunnerArguments,
)

logger = setup_logger(__name__)

engine_args = STORMWikiRunnerArguments(
    output_dir="temp",
    max_conv_turn=3,
    max_perspective=3,
    search_top_k=3,
    max_thread_num=10,
)


openai_kwargs = {
    "api_key": os.getenv("OPENAI_API_KEY"),
    "api_provider": "openai",
    "temperature": 1.0,
    "top_p": 0.9,
    "api_base": os.getenv("OPENAI_API_BASE"),
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
conv_simulator_lm = ModelClass(model="gpt-4o-mini", max_tokens=500, **openai_kwargs)
question_asker_lm = ModelClass(model="gpt-4o-mini", max_tokens=500, **openai_kwargs)
outline_gen_lm = ModelClass(model="gpt-4o-mini", max_tokens=400, **openai_kwargs)
article_gen_lm = ModelClass(model="gpt-4o-mini", max_tokens=700, **openai_kwargs)
article_polish_lm = ModelClass(
    model="gpt-4o-mini", max_tokens=max_tokens, **openai_kwargs
)
lm_configs.set_conv_simulator_lm(conv_simulator_lm)
lm_configs.set_question_asker_lm(question_asker_lm)
lm_configs.set_outline_gen_lm(outline_gen_lm)
lm_configs.set_article_gen_lm(article_gen_lm)
lm_configs.set_article_polish_lm(article_polish_lm)

rm = BingSearch(bing_search_api=os.getenv("BING_SEARCH_API_KEY"), k=3, safe_search="Off")
# rm = DuckDuckGoSearchRM(k=5, safe_search="Off", region="us-en")

runner = STORMWikiRunner(engine_args, lm_configs, rm)


async def handle_enhance(request):
    """Enhance the response of a given prompt.

    Args:
        request: aiohttp request object. It should contain a JSON object with a "prompt" key.

    Returns:
        aiohttp response object.
    """
    data = await request.json()
    prompt = data.get("prompt", "")
    logger.info(f"received prompt: {prompt}")

    runner.run(
        topic=prompt,
        do_research=True,
        do_generate_outline=True,
        do_generate_article=True,
        do_polish_article=True,
    )
    runner.post_run()
    runner.summary()

    return web.json_response({"response": "ok"})


def create_app():
    app = web.Application()
    app.router.add_post("/enhance", handle_enhance)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=8080)
