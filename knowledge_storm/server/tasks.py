import uuid
import json
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass

from kipp.redis.utils import RedisUtils
from kipp.redis.consts import KEY_PREFIX_TASK

from knowledge_storm.server.utils import utcnow

KEY_TASK_LLM_STORM = KEY_PREFIX_TASK + "llm_storm/pending"  # Fixed pending list key
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
    failed_reason: Optional[str] = None
    finished_at: Optional[str] = None
    result_article: Optional[str] = None
    result_references: Optional[Dict[str, Dict]] = None
    runner: str = ""

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
                "runner": self.runner,
            }
        )

    @classmethod
    def from_string(cls, task_str: str):
        try:
            task_dict = json.loads(task_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid task string: {e}")
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
        created_at=utcnow(),
    )
    # Push the task to the pending list (always the same key)
    rutils.rpush(KEY_TASK_LLM_STORM, task.to_string())
    return task_id


def get_llm_storm_task_blocking(rutils: RedisUtils) -> StormTask:
    """Retrieve and remove a task from LLM Storm pending list.

    Args:
        rutils (RedisUtils): Redis utils instance.

    Returns:
        StormTask: The retrieved StormTask object.

    Raises:
        ValueError: If no task is found.
    """
    # rutils.lpop_keys_blocking returns a tuple (key, value) from one of the given keys.
    key, value = rutils.lpop_keys_blocking([KEY_TASK_LLM_STORM])
    if not value:
        raise ValueError("Task not found from key: " + key)
    return StormTask.from_string(value)


def upload_llm_storm_result(rutils: RedisUtils, task: StormTask) -> None:
    """Upload the result of an LLM Storm task to Redis.

    Args:
        rutils (RedisUtils): Redis utils instance.
        task (StormTask): The StormTask with updated result.
    """
    key = KEY_PREFIX_TASK_LLM_STORM_RESULT + task.task_id
    rutils.set_item(key, task.to_string(), 3600 * 24 * 30)
