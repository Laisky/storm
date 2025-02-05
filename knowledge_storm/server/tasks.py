import uuid
import json
from datetime import datetime
from typing import NamedTuple, Dict
from dataclasses import dataclass

from kipp.redis.utils import RedisUtils
from kipp.redis.consts import KEY_PREFIX_TASK

KEY_TASK_LLM_STORM = KEY_PREFIX_TASK + "llm_storm/pending"
KEY_PREFIX_TASK_LLM_STORM_RESULT = KEY_PREFIX_TASK + "llm_storm/result/"

TASK_STATUS_PENDING: str = "pending"
TASK_STATUS_RUNNING: str = "running"
TASK_STATUS_SUCCESS: str = "success"
TASK_STATUS_FAILED: str = "failed"


@dataclass
class StormTask:
    task_id: str
    prompt: str
    api_key: str
    created_at: str
    status: str = TASK_STATUS_PENDING
    failed_reason: str = None
    finished_at: str = None
    result_article: str = None
    result_references: Dict[str, Dict] = None

    def to_string(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "prompt": self.prompt,
                "api_key": self.api_key,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "status": self.status,
                "result_article": self.result_article,
                "result_references": self.result_references,
                "failed_reason": self.failed_reason,
            }
        )

    @classmethod
    def from_string(cls, task_str: str):
        task_dict = json.loads(task_str)
        return cls(**task_dict)


def add_llm_storm_task(rutils: RedisUtils, prompt: str, api_key: str) -> str:
    """Add a task to LLM Storm.

    Args:
        rutils (RedisUtils): Redis utils instance.
        prompt (str): The prompt to process.
        api_key (str): The API key for authentication.

    Returns:
        str: The task ID.
    """
    task_id = str(uuid.uuid4())

    task = StormTask(
        task_id=task_id,
        prompt=prompt,
        api_key=api_key,
        created_at=datetime.now().isoformat(),
    )
    key = KEY_TASK_LLM_STORM + task_id
    rutils.rpush(key, task.to_string())
    return task_id


def get_llm_storm_task_blocking(rutils: RedisUtils) -> StormTask:
    """Retrieve and remove a task from LLM Storm by task ID.

    Args:
        rutils (RedisUtils): Redis utils instance.
        task_id (str): The task ID.

    Returns:
        StormTask: The retrieved StormTask object.

    Raises:
        ValueError: If the task is not found.
    """
    _, value = rutils.lpop_keys_blocking([KEY_TASK_LLM_STORM])
    if not value:
        raise ValueError("Task not found")

    return StormTask.from_string(value)


def upload_llm_storm_result(rutils: RedisUtils, task: StormTask) -> None:
    """Upload the result of an LLM Storm task to Redis.

    Args:
        rutils (RedisUtils): Redis utils instance.
        task (StormTask): The StormTask with updated result.
    """
    key = KEY_PREFIX_TASK_LLM_STORM_RESULT + task.task_id
    rutils.set_item(key, task.to_string(), 3600 * 24 * 30)
