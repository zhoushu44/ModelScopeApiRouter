import sys
import httpx
import logging
import json
import re
import asyncio
import time
from pathlib import Path
from typing import Tuple, Dict, Optional

current_dir = Path(__file__).parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

from settings import config

logger = logging.getLogger(__name__)


class RetryableResponseError(Exception):
    pass


class _PreStreamError(Exception):
    pass


class APIClient:
    def __init__(self):
        self.client = None
        self.max_request_retries = 3
        self.upstream_concurrency = 5
        self.upstream_semaphore = asyncio.Semaphore(self.upstream_concurrency)
        self.key_state_lock = asyncio.Lock()
        self.model_state_lock = asyncio.Lock()
        self.key_base_cooldown_seconds = 15
        self.key_max_cooldown_seconds = 120
        self.key_fail_threshold = 2
        self.model_base_cooldown_seconds = 20
        self.model_max_cooldown_seconds = 180
        self.model_fail_threshold = 2
        self.key_states: Dict[str, Dict] = {}
        self.model_states: Dict[str, Dict] = {}
    
    @staticmethod
    def _normalize_stream_chunk(chunk_data: dict) -> dict:
        """规范化流式响应 chunk，移除非标准字段"""
        if not isinstance(chunk_data, dict):
            return chunk_data
        
        choices = chunk_data.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if "function_calls" in delta:
                        del delta["function_calls"]
        
        return chunk_data
    
    def _update_quota_from_headers(self, key_id: str, headers: dict):
        quota = {}

        for h in headers:
            hl = h.lower()
            if hl == "modelscope-ratelimit-requests-limit":
                quota["daily_limit"] = int(headers[h])
            elif hl == "modelscope-ratelimit-requests-remaining":
                quota["daily_remaining"] = int(headers[h])
            elif hl == "modelscope-ratelimit-model-requests-limit":
                quota["model_limit"] = int(headers[h])
            elif hl == "modelscope-ratelimit-model-requests-remaining":
                quota["model_remaining"] = int(headers[h])

        if quota:
            quota["updated_at"] = time.time()
            config.update_quota(key_id, quota)
        return quota
    
    def is_key_exhausted(self, key_id: str, quota: Dict) -> bool:
        daily_exhausted = (
            quota.get("daily_limit", 0) > 0 and 
            quota.get("daily_remaining", 0) <= 0
        )
        model_exhausted = (
            quota.get("model_limit", 0) > 0 and 
            quota.get("model_remaining", 0) <= 0
        )
        return daily_exhausted or model_exhausted
    
    def should_try_next_key(self, quota: Dict) -> bool:
        model_quota_exhausted = (
            quota.get("model_limit", 0) > 0 and 
            quota.get("model_remaining", 0) < quota.get("model_limit", 0) and
            quota.get("model_remaining", 0) > 0
        )
        return model_quota_exhausted

    def _is_non_empty_string(self, value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _looks_like_image_url(self, value) -> bool:
        # 这里简单的这样判断，是否合理？后续考虑改进。
        return self._is_non_empty_string(value) and (
            value.startswith("http://") or
            value.startswith("https://") or
            value.startswith("data:image")
        )

    def _extract_text_content(self, result: dict) -> Optional[str]:
        if not isinstance(result, dict):
            return None

        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            return None

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return None

        message = first_choice.get("message")
        if not isinstance(message, dict):
            return None

        content = message.get("content")
        if self._is_non_empty_string(content):
            return content

        return None

    def _is_empty_text_response(self, result: dict) -> bool:
        if not isinstance(result, dict) or not result:
            return True
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message")
                if isinstance(message, dict):
                    tool_calls = message.get("tool_calls")
                    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                        return False
                    reasoning_content = message.get("reasoning_content")
                    if self._is_non_empty_string(reasoning_content):
                        return False
        content = self._extract_text_content(result)
        if not self._is_non_empty_string(content):
            return True
        normalized = content.strip().lower()
        return normalized in {"null", "none", "未知", "处理失败"}

    def _retry_delay(self, attempt: int, status_code: Optional[int] = None) -> float:
        if status_code == 429:
            return min(8, 2 ** attempt)
        return min(3, attempt)

    def _clamp_score(self, value: int) -> int:
        return max(-20, min(20, value))

    async def _refresh_key_states(self, keys: list[dict]) -> None:
        async with self.key_state_lock:
            active_ids = {key["id"] for key in keys}
            for key in keys:
                state = self.key_states.setdefault(key["id"], {
                    "health_score": 0,
                    "consecutive_failures": 0,
                    "cooldown_until": 0.0,
                    "last_used_at": 0.0,
                    "last_success_at": 0.0,
                    "last_failure_at": 0.0,
                    "success_count": 0,
                    "failure_count": 0,
                    "last_error": ""
                })
                state["health_score"] = self._clamp_score(state.get("health_score", 0))
            stale_ids = [key_id for key_id in self.key_states if key_id not in active_ids]
            for key_id in stale_ids:
                del self.key_states[key_id]

    async def _mark_key_selected(self, key_id: str) -> None:
        async with self.key_state_lock:
            state = self.key_states.setdefault(key_id, {})
            state["last_used_at"] = time.time()

    async def _mark_key_success(self, key_id: str) -> None:
        async with self.key_state_lock:
            state = self.key_states.setdefault(key_id, {})
            state["health_score"] = self._clamp_score(state.get("health_score", 0) + 2)
            state["consecutive_failures"] = 0
            state["cooldown_until"] = 0.0
            state["last_success_at"] = time.time()
            state["success_count"] = state.get("success_count", 0) + 1
            state["last_error"] = ""

    async def _mark_key_retryable_failure(self, key_id: str, reason: str, status_code: Optional[int] = None) -> None:
        async with self.key_state_lock:
            state = self.key_states.setdefault(key_id, {})
            consecutive_failures = state.get("consecutive_failures", 0) + 1
            penalty = 3 if status_code == 429 else 2
            state["health_score"] = self._clamp_score(state.get("health_score", 0) - penalty)
            state["consecutive_failures"] = consecutive_failures
            state["failure_count"] = state.get("failure_count", 0) + 1
            state["last_failure_at"] = time.time()
            state["last_error"] = reason
            if status_code == 429 or consecutive_failures >= self.key_fail_threshold:
                cooldown = min(
                    self.key_max_cooldown_seconds,
                    self.key_base_cooldown_seconds * (2 ** max(0, consecutive_failures - self.key_fail_threshold))
                )
                state["cooldown_until"] = time.time() + cooldown

    async def _mark_key_fatal_failure(self, key_id: str, reason: str) -> None:
        async with self.key_state_lock:
            state = self.key_states.setdefault(key_id, {})
            consecutive_failures = state.get("consecutive_failures", 0) + 1
            state["health_score"] = self._clamp_score(state.get("health_score", 0) - 4)
            state["consecutive_failures"] = consecutive_failures
            state["failure_count"] = state.get("failure_count", 0) + 1
            state["last_failure_at"] = time.time()
            state["last_error"] = reason
            cooldown = min(
                self.key_max_cooldown_seconds,
                self.key_base_cooldown_seconds * (2 ** max(0, consecutive_failures - 1))
            )
            state["cooldown_until"] = time.time() + cooldown

    async def _rank_available_keys(self, all_keys: list[dict], excluded_ids: set[str]) -> list[dict]:
        await self._refresh_key_states(all_keys)
        while True:
            async with self.key_state_lock:
                now = time.time()
                active_keys = []
                cooling_keys = []
                for key in all_keys:
                    if key["id"] in excluded_ids:
                        continue
                    state = self.key_states.get(key["id"], {})
                    cooldown_until = state.get("cooldown_until", 0.0)
                    key_info = {
                        "key": key,
                        "health_score": state.get("health_score", 0),
                        "consecutive_failures": state.get("consecutive_failures", 0),
                        "cooldown_until": cooldown_until,
                        "last_used_at": state.get("last_used_at", 0.0),
                        "success_count": state.get("success_count", 0),
                        "failure_count": state.get("failure_count", 0),
                    }
                    if cooldown_until <= now:
                        active_keys.append(key_info)
                    else:
                        cooling_keys.append(key_info)

            if active_keys:
                active_keys.sort(
                    key=lambda item: (
                        -item["health_score"],
                        item["consecutive_failures"],
                        item["last_used_at"],
                        -(item["success_count"] - item["failure_count"]),
                    )
                )
                return [item["key"] for item in active_keys]

            if not cooling_keys:
                return []

            earliest_cooldown_until = min(item["cooldown_until"] for item in cooling_keys)
            wait_seconds = max(0.1, min(earliest_cooldown_until - time.time(), 2.0))
            logger.warning(f"所有 Key 均处于熔断冷却中，排队等待 {wait_seconds:.2f} 秒")
            await asyncio.sleep(wait_seconds)

    async def _refresh_model_states(self, models: list[dict]) -> None:
        async with self.model_state_lock:
            active_ids = {model["id"] for model in models if model.get("id")}
            for model in models:
                model_id = model.get("id") or model.get("name") or model.get("model_id")
                if not model_id:
                    continue
                state = self.model_states.setdefault(model_id, {
                    "health_score": 0,
                    "consecutive_failures": 0,
                    "cooldown_until": 0.0,
                    "last_used_at": 0.0,
                    "last_success_at": 0.0,
                    "last_failure_at": 0.0,
                    "success_count": 0,
                    "failure_count": 0,
                    "last_error": ""
                })
                state["health_score"] = self._clamp_score(state.get("health_score", 0))
            stale_ids = [model_id for model_id in self.model_states if model_id not in active_ids]
            for model_id in stale_ids:
                del self.model_states[model_id]

    def _get_model_state_id(self, model: dict) -> str:
        return model.get("id") or model.get("name") or model.get("model_id")

    async def _mark_model_selected(self, model_id: str) -> None:
        async with self.model_state_lock:
            state = self.model_states.setdefault(model_id, {})
            state["last_used_at"] = time.time()

    async def _mark_model_success(self, model_id: str) -> None:
        async with self.model_state_lock:
            state = self.model_states.setdefault(model_id, {})
            state["health_score"] = self._clamp_score(state.get("health_score", 0) + 2)
            state["consecutive_failures"] = 0
            state["cooldown_until"] = 0.0
            state["last_success_at"] = time.time()
            state["success_count"] = state.get("success_count", 0) + 1
            state["last_error"] = ""

    async def _mark_model_retryable_failure(self, model_id: str, reason: str, status_code: Optional[int] = None) -> None:
        async with self.model_state_lock:
            state = self.model_states.setdefault(model_id, {})
            consecutive_failures = state.get("consecutive_failures", 0) + 1
            penalty = 3 if status_code == 429 else 2
            state["health_score"] = self._clamp_score(state.get("health_score", 0) - penalty)
            state["consecutive_failures"] = consecutive_failures
            state["failure_count"] = state.get("failure_count", 0) + 1
            state["last_failure_at"] = time.time()
            state["last_error"] = reason
            if status_code == 429 or consecutive_failures >= self.model_fail_threshold:
                cooldown = min(
                    self.model_max_cooldown_seconds,
                    self.model_base_cooldown_seconds * (2 ** max(0, consecutive_failures - self.model_fail_threshold))
                )
                state["cooldown_until"] = time.time() + cooldown

    async def _mark_model_fatal_failure(self, model_id: str, reason: str) -> None:
        async with self.model_state_lock:
            state = self.model_states.setdefault(model_id, {})
            consecutive_failures = state.get("consecutive_failures", 0) + 1
            state["health_score"] = self._clamp_score(state.get("health_score", 0) - 4)
            state["consecutive_failures"] = consecutive_failures
            state["failure_count"] = state.get("failure_count", 0) + 1
            state["last_failure_at"] = time.time()
            state["last_error"] = reason
            cooldown = min(
                self.model_max_cooldown_seconds,
                self.model_base_cooldown_seconds * (2 ** max(0, consecutive_failures - 1))
            )
            state["cooldown_until"] = time.time() + cooldown

    async def _rank_available_models(self, models: list[dict], excluded_ids: set[str]) -> list[dict]:
        await self._refresh_model_states(models)
        while True:
            async with self.model_state_lock:
                now = time.time()
                active_models = []
                cooling_models = []
                for model in models:
                    model_id = self._get_model_state_id(model)
                    if model_id in excluded_ids:
                        continue
                    state = self.model_states.get(model_id, {})
                    cooldown_until = state.get("cooldown_until", 0.0)
                    model_info = {
                        "model": model,
                        "health_score": state.get("health_score", 0),
                        "consecutive_failures": state.get("consecutive_failures", 0),
                        "cooldown_until": cooldown_until,
                        "last_used_at": state.get("last_used_at", 0.0),
                        "success_count": state.get("success_count", 0),
                        "failure_count": state.get("failure_count", 0),
                    }
                    if cooldown_until <= now:
                        active_models.append(model_info)
                    else:
                        cooling_models.append(model_info)

            if active_models:
                active_models.sort(
                    key=lambda item: (
                        -item["health_score"],
                        item["consecutive_failures"],
                        item["last_used_at"],
                        -(item["success_count"] - item["failure_count"]),
                        item["model"].get("order", 0),
                    )
                )
                return [item["model"] for item in active_models]

            if not cooling_models:
                return []

            earliest_cooldown_until = min(item["cooldown_until"] for item in cooling_models)
            wait_seconds = max(0.1, min(earliest_cooldown_until - time.time(), 2.0))
            logger.warning(f"所有模型均处于熔断冷却中，排队等待 {wait_seconds:.2f} 秒")
            await asyncio.sleep(wait_seconds)

    async def _post_json(self, url: str, json_data: str, headers: dict, timeout: int) -> httpx.Response:
        queue_entered_at = time.perf_counter()
        async with self.upstream_semaphore:
            queued_seconds = time.perf_counter() - queue_entered_at
            if queued_seconds >= 0.2:
                logger.info(f"请求在上游队列中等待了 {queued_seconds:.2f} 秒")
            async with httpx.AsyncClient() as client:
                return await client.post(
                    url,
                    content=json_data,
                    headers=headers,
                    timeout=timeout
                )

    async def _get_json(self, url: str, headers: dict, timeout: int) -> httpx.Response:
        queue_entered_at = time.perf_counter()
        async with self.upstream_semaphore:
            queued_seconds = time.perf_counter() - queue_entered_at
            if queued_seconds >= 0.2:
                logger.info(f"任务查询在上游队列中等待了 {queued_seconds:.2f} 秒")
            async with httpx.AsyncClient() as client:
                return await client.get(
                    url,
                    headers=headers,
                    timeout=timeout
                )

    def _load_json_object(self, response: httpx.Response, stage: str) -> dict:
        try:
            result = response.json()
        except Exception as e:
            raise RetryableResponseError(f"{stage} 返回了无法解析的 JSON: {e}")

        if result is None:
            raise RetryableResponseError(f"{stage} 返回了空响应")

        if not isinstance(result, dict):
            raise RetryableResponseError(f"{stage} 返回了非对象响应: {type(result).__name__}")

        return result

    def _is_empty_shell_response(self, result: dict) -> bool:
        if not isinstance(result, dict) or not result:
            return True

        if self._extract_image_url(result):
            return False

        task_id = result.get("task_id")
        task_status = str(result.get("task_status", "")).strip().upper()
        if self._is_non_empty_string(task_id):
            return False
        if task_status in ["PENDING", "PROCESSING", "SUCCEED", "FAILED"]:
            return False

        choices = result.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    if self._is_non_empty_string(message.get("content")):
                        return False
                    tool_calls = message.get("tool_calls")
                    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                        return False
                    if self._is_non_empty_string(message.get("reasoning_content")):
                        return False

        return True
    
    def _extract_image_url(self, result: dict) -> Optional[str]:
        try:
            if not isinstance(result, dict):
                return None

            choices = result.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0]
                if isinstance(choice, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if self._looks_like_image_url(content):
                            return content
            
            image_url = result.get("image_url")
            if self._looks_like_image_url(image_url):
                return image_url
            
            url = result.get("url")
            if self._looks_like_image_url(url):
                return url
            
            images = result.get("images")
            if isinstance(images, list):
                for image in images:
                    if self._looks_like_image_url(image):
                        return image
            
            image = result.get("image")
            if self._looks_like_image_url(image):
                return image

            output_images = result.get("output_images")
            if isinstance(output_images, list):
                for image in output_images:
                    if self._looks_like_image_url(image):
                        return image
            
            result_str = json.dumps(result)
            url_match = re.search(r'https?://[^\s"\'<>]+', result_str)
            if url_match:
                return url_match.group(0)
            
        except Exception as e:
            logger.warning(f"提取图片 URL 失败: {e}")
        
        return None
    
    def _validate_image_size(self, data: dict) -> Tuple[bool, Optional[str]]:
        try:
            if "messages" in data and isinstance(data["messages"], list):
                for message in data["messages"]:
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") == "image_url":
                                image_obj = item.get("image_url", {})
                                if not isinstance(image_obj, dict):
                                    continue
                                width = image_obj.get("width")
                                height = image_obj.get("height")
                                if width and height:
                                    ratio = width / height
                                    if ratio < 1 / 3 or ratio > 3:
                                        return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"

            if "images" in data:
                images = data["images"]
                if isinstance(images, list) and len(images) > 0:
                    for img in images:
                        if isinstance(img, dict):
                            width = img.get("width", 1024)
                            height = img.get("height", 1024)
                            ratio = width / height
                            if ratio < 1 / 3 or ratio > 3:
                                return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"

            if "image_url" in data:
                image_urls = data["image_url"]
                if isinstance(image_urls, list) and len(image_urls) > 0:
                    for img in image_urls:
                        if isinstance(img, dict):
                            width = img.get("width", 1024)
                            height = img.get("height", 1024)
                            ratio = width / height
                            if ratio < 1 / 3 or ratio > 3:
                                return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"
            
            width = data.get("width", 1024)
            height = data.get("height", 1024)
            ratio = width / height
            if ratio < 1 / 3 or ratio > 3:
                return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"
            
            return True, None
        except Exception as e:
            logger.warning(f"图片尺寸校验异常: {e}")
            return True, None
    
    def _convert_openai_to_modelscope(self, data: dict, target_category: str) -> dict:
        try:
            modelscope_data = {}
            
            if "messages" in data and len(data["messages"]) > 0:
                last_message = data["messages"][-1]
                if isinstance(last_message, dict):
                    content = last_message.get("content", "")
                else:
                    content = ""
                
                if isinstance(content, str):
                    modelscope_data["prompt"] = content
                elif isinstance(content, list):
                    text_parts = []
                    image_urls = []
                    
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "image_url":
                            image_obj = item.get("image_url", {})
                            if isinstance(image_obj, dict):
                                img_url = image_obj.get("url", "")
                            else:
                                img_url = ""
                            if img_url:
                                image_urls.append(img_url)
                    
                    if text_parts:
                        modelscope_data["prompt"] = " ".join(text_parts)
                    
                    if image_urls and target_category == "img2img":
                        modelscope_data["image_url"] = image_urls[0]
            
            for key in ["width", "height", "size", "n", "quality"]:
                if key in data:
                    modelscope_data[key] = data[key]
            
            return modelscope_data
        except Exception as e:
            logger.warning(f"请求格式转换失败: {e}")
            return data
    
    def _format_image_response(self, result: dict, image_url: str, original_data: dict) -> dict:
        try:
            formatted_result = result.copy() if isinstance(result, dict) else {}

            first_choice = None
            choices = formatted_result.get("choices")
            if isinstance(choices, list) and choices:
                candidate = choices[0]
                if isinstance(candidate, dict):
                    first_choice = candidate

            if not first_choice:
                formatted_result["choices"] = [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": image_url
                    },
                    "finish_reason": "stop"
                }]
            else:
                message = first_choice.get("message")
                if not isinstance(message, dict):
                    message = {
                        "role": "assistant",
                        "content": image_url
                    }
                    first_choice["message"] = message
                else:
                    current_content = message.get("content", "")
                    if not self._looks_like_image_url(current_content):
                        message["content"] = image_url
            
            formatted_result["image_url"] = image_url
            formatted_result["images"] = [image_url]
            
            return formatted_result
        except Exception as e:
            logger.warning(f"格式化响应失败: {e}")
            return result if isinstance(result, dict) else {"image_url": image_url, "images": [image_url]}
    
    async def call_model(self, model_name: str, data: dict, headers: dict, timeout: int, client_ip: str = None) -> Tuple[dict, int, dict, dict]:
        target_category = "chat"
        for model in config.MODELS:
            if model.get("name") == model_name:
                target_category = model.get("category", "chat")
                break
        
        if target_category == "img2img":
            valid, error_msg = self._validate_image_size(data)
            if not valid:
                raise Exception(error_msg)
        
        models_by_category = config.get_models_by_category()
        category_models = models_by_category.get(target_category, [])
        
        if not category_models:
            category_models = config.MODELS
        
        all_keys = config.API_KEYS
        
        if not all_keys:
            raise Exception("没有可用的 API Key")
        
        logger.info(f"请求分类: {target_category}, 模型数: {len(category_models)}, Key 数: {len(all_keys)}, 上游并发限制: {self.upstream_concurrency}")
        exhausted_models = set()

        # 图片生成类请求耗时较长，限制最多尝试 3 个模型
        max_image_model_attempts = 3 if target_category in ("text2img", "img2img") else len(category_models)
        model_attempt_count = 0

        while True:
            candidate_models = await self._rank_available_models(category_models, exhausted_models)
            if not candidate_models:
                break

            made_progress = False

            for model in candidate_models:
                made_progress = True
                model_id = self._get_model_state_id(model)
                await self._mark_model_selected(model_id)
                logger.info(f"尝试模型: {model['name']} ({model['model_id']})")
                exhausted_keys_for_this_model = set()
                model_last_error = None
                model_retryable = False

                previous_key_name = None
                while True:
                    candidate_keys = await self._rank_available_keys(all_keys, exhausted_keys_for_this_model)
                    if not candidate_keys:
                        break

                    key_made_progress = False

                    for key in candidate_keys:
                        key_made_progress = True
                        request_attempt = 0
                        last_error = None
                        await self._mark_key_selected(key["id"])
                        
                        # 打印切换日志
                        if previous_key_name:
                            logger.info(f"🔄 Key 切换: {previous_key_name} → {key['name']}")
                        
                        while request_attempt < self.max_request_retries:
                            request_attempt += 1
                            
                            try:
                                if target_category in ["text2img", "img2img"]:
                                    url = f"{config.BASE_URL}/images/generations"
                                    headers_copy = {
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {key['key']}",
                                        "X-ModelScope-Async-Mode": "true"
                                    }
                                else:
                                    url = f"{config.BASE_URL}/chat/completions"
                                    headers_copy = {
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {key['key']}"
                                    }
                                
                                request_data = data.copy()
                                
                                if target_category in ["text2img", "img2img"]:
                                    request_data = self._convert_openai_to_modelscope(request_data, target_category)
                                
                                request_data["model"] = model["model_id"]
                                
                                logger.info(f"  使用模型: {model['name']}，Key: {key['name']}，第 {request_attempt}/{self.max_request_retries} 次尝试")
                                
                                json_data = json.dumps(request_data)
                                response = await self._post_json(
                                    url,
                                    json_data,
                                    headers_copy,
                                    timeout
                                )
                                
                                quota = self._update_quota_from_headers(key["id"], response.headers)
                                
                                if response.status_code == 429:
                                    try:
                                        error_text = response.text
                                    except Exception:
                                        error_text = str(response)
                                    logger.warning(f"  Key {key['name']} 被限流 429，立即切换下一个 Key")
                                    await self._mark_key_retryable_failure(key["id"], f"限流 429: {error_text[:200]}", status_code=429)
                                    exhausted_keys_for_this_model.add(key["id"])
                                    previous_key_name = key['name']
                                    model_last_error = "限流 429"
                                    model_retryable = True
                                    break

                                if response.status_code >= 400:
                                    try:
                                        error_text = response.text
                                    except Exception:
                                        error_text = str(response)
                                    logger.warning(f"  Key {key['name']} HTTP {response.status_code} 错误: {error_text[:300]}")
                                    await self._mark_key_fatal_failure(key["id"], f"HTTP {response.status_code}")
                                    exhausted_keys_for_this_model.add(key["id"])
                                    previous_key_name = key['name']
                                    model_last_error = f"HTTP {response.status_code}"
                                    model_retryable = False
                                    break
                                
                                if self.should_try_next_key(quota):
                                    logger.info(f"  Key {key['name']} 模型额度用完，换 Key")
                                    await self._mark_key_fatal_failure(key["id"], "模型额度不足")
                                    exhausted_keys_for_this_model.add(key["id"])
                                    previous_key_name = key['name']
                                    model_last_error = "模型额度不足"
                                    model_retryable = False
                                    break
                                
                                if self.is_key_exhausted(key["id"], quota):
                                    logger.info(f"  Key {key['name']} 完全耗尽")
                                    await self._mark_key_fatal_failure(key["id"], "Key 配额耗尽")
                                    exhausted_keys_for_this_model.add(key["id"])
                                    previous_key_name = key['name']
                                    model_last_error = "Key 配额耗尽"
                                    model_retryable = False
                                    break
                                
                                result = self._load_json_object(response, "主请求")

                                if target_category in ["chat", "vision"] and self._is_empty_text_response(result):
                                    raise RetryableResponseError("文本/视觉响应为空壳，切换下一个 Key 或模型重试")

                                if target_category in ["text2img", "img2img"]:
                                    task_id = result.get("task_id")
                                    if self._is_non_empty_string(task_id):
                                        logger.info(f"  异步任务已提交，task_id: {task_id}")

                                        task_headers = {
                                            "Authorization": f"Bearer {key['key']}",
                                            "X-ModelScope-Task-Type": "image_generation"
                                        }

                                        if target_category == "img2img":
                                            max_retries = 60
                                            poll_interval = 4
                                        else:
                                            max_retries = 40
                                            poll_interval = 3
                                        retry_count = 0
                                        task_completed = False

                                        while retry_count < max_retries:
                                            await asyncio.sleep(poll_interval)
                                            retry_count += 1

                                            task_response = await self._get_json(
                                                f"{config.BASE_URL}/tasks/{task_id}",
                                                task_headers,
                                                timeout
                                            )

                                            if task_response.status_code == 429:
                                                raise RetryableResponseError("任务查询被上游限流 429")

                                            task_result = self._load_json_object(task_response, "任务查询")
                                            task_status = str(task_result.get("task_status", "")).strip().upper()

                                            logger.info(f"  任务状态: {task_status} (尝试 {retry_count}/{max_retries})")

                                            if task_status == "SUCCEED":
                                                task_completed = True
                                                image_url = self._extract_image_url(task_result)
                                                if image_url:
                                                    result = self._format_image_response(task_result, image_url, data)
                                                    logger.info(f"✅ 成功提取图片链接: {image_url[:50]}...")
                                                    break

                                                raise RetryableResponseError("异步任务成功但返回空壳响应，切换下一个 Key 或模型重试")
                                            if task_status == "FAILED":
                                                task_completed = True
                                                logger.error(f"任务失败: {task_result}")
                                                raise Exception(f"任务失败: {task_result.get('error_message', '未知错误')}")
                                            if task_status in ["PENDING", "PROCESSING"]:
                                                continue
                                            if self._is_empty_shell_response(task_result):
                                                task_completed = True
                                                raise RetryableResponseError("任务查询返回空壳响应，切换下一个 Key 或模型重试")

                                            task_completed = True
                                            logger.warning(f"未知任务状态: {task_status}")
                                            raise RetryableResponseError(f"异步任务返回未知状态 {task_status or 'EMPTY'}，切换下一个 Key 或模型重试")

                                        if not task_completed:
                                            raise RetryableResponseError("异步任务轮询超时，切换下一个 Key 或模型重试")
                                    else:
                                        image_url = self._extract_image_url(result)
                                        if image_url:
                                            result = self._format_image_response(result, image_url, data)
                                            logger.info(f"✅ 成功提取图片链接: {image_url[:50]}...")
                                        else:
                                            task_status = str(result.get("task_status", "")).strip().upper()
                                            if task_status in ["PENDING", "PROCESSING"]:
                                                raise RetryableResponseError("异步任务返回了空 task_id，切换下一个 Key 或模型重试")
                                            if self._is_empty_shell_response(result):
                                                raise RetryableResponseError("主请求返回空壳响应，切换下一个 Key 或模型重试")
                                            raise RetryableResponseError(f"图像任务未返回可用图片链接，状态: {task_status or 'EMPTY'}，切换下一个 Key 或模型重试")

                                await self._mark_key_success(key["id"])
                                await self._mark_model_success(model_id)
                                logger.info(f"✅ 成功！模型: {model['name']}, Key: {key['name']}")
                                call_info = {
                                    "actual_model": model['name'],
                                    "actual_model_id": model['model_id'],
                                    "actual_key_name": key['name'],
                                    "actual_key_id": key['id'],
                                    "client_ip": client_ip
                                }
                                return result, response.status_code, dict(response.headers), call_info

                            except RetryableResponseError as e:
                                last_error = e
                                model_last_error = str(e)
                                model_retryable = True
                                status_code = 429 if "429" in str(e) else None
                                await self._mark_key_retryable_failure(key["id"], str(e), status_code=status_code)
                                logger.warning(f"  Key {key['name']} 第 {request_attempt}/{self.max_request_retries} 次返回可重试异常: {e}")
                                if request_attempt >= self.max_request_retries:
                                    logger.error(f"  Key {key['name']} 连续返回空壳/限流/异常响应，切换下一个 Key")
                                    exhausted_keys_for_this_model.add(key["id"])
                                    break
                                await asyncio.sleep(self._retry_delay(request_attempt, status_code))
                                continue
                            except httpx.TimeoutException as e:
                                last_error = e
                                model_last_error = "请求超时"
                                model_retryable = True
                                await self._mark_key_retryable_failure(key["id"], "请求超时", status_code=None)
                                logger.warning(f"  Key {key['name']} 请求超时，立即切换下一个 Key")
                                exhausted_keys_for_this_model.add(key["id"])
                                previous_key_name = key['name']
                                break
                            except httpx.NetworkError as e:
                                last_error = e
                                model_last_error = f"网络错误: {str(e)}"
                                model_retryable = True
                                await self._mark_key_retryable_failure(key["id"], str(e), status_code=None)
                                logger.warning(f"  Key {key['name']} 网络错误，立即切换下一个 Key: {e}")
                                exhausted_keys_for_this_model.add(key["id"])
                                previous_key_name = key['name']
                                break
                            except Exception as e:
                                last_error = e
                                model_last_error = str(e)
                                model_retryable = False
                                await self._mark_key_fatal_failure(key["id"], str(e))
                                logger.error(f"  Key {key['name']} 调用异常: {e}")
                                exhausted_keys_for_this_model.add(key["id"])
                                previous_key_name = key['name']
                                break

                        if last_error and request_attempt >= self.max_request_retries:
                            exhausted_keys_for_this_model.add(key["id"])

                    if not key_made_progress:
                        break

                if model_last_error:
                    if model_retryable:
                        status_code = 429 if "429" in model_last_error else None
                        await self._mark_model_retryable_failure(model_id, model_last_error, status_code=status_code)
                    else:
                        await self._mark_model_fatal_failure(model_id, model_last_error)
                else:
                    await self._mark_model_retryable_failure(model_id, "模型下无可用 Key")

                exhausted_models.add(model_id)
                model_attempt_count += 1
                logger.warning(f"⚠️  模型 {model['name']} 所有 Key 都失败，换下一个模型 ({model_attempt_count}/{max_image_model_attempts})")

                if model_attempt_count >= max_image_model_attempts:
                    logger.warning(f"图片生成类请求已达最大模型尝试次数 ({max_image_model_attempts})，不再重试")
                    break

            if not made_progress:
                break
        
        raise Exception("所有模型和 Key 都调用失败，请检查配置")

    async def call_model_stream(self, model_name: str, data: dict, headers: dict, timeout: int, client_ip: str = None):
        target_category = "chat"
        for model in config.MODELS:
            if model.get("name") == model_name:
                target_category = model.get("category", "chat")
                break

        if target_category in ("text2img", "img2img"):
            raise Exception("图片生成模型不支持流式调用，请使用非流式请求")

        models_by_category = config.get_models_by_category()
        category_models = models_by_category.get(target_category, [])
        if not category_models:
            category_models = config.MODELS

        all_keys = config.API_KEYS
        if not all_keys:
            raise Exception("没有可用的 API Key")

        exhausted_models = set()
        stream_started = False

        while True:
            candidate_models = await self._rank_available_models(category_models, exhausted_models)
            if not candidate_models:
                break

            made_progress = False

            for model in candidate_models:
                made_progress = True
                model_id = self._get_model_state_id(model)
                await self._mark_model_selected(model_id)
                logger.info(f"流式请求 - 尝试模型: {model['name']} ({model['model_id']})")
                exhausted_keys_for_this_model = set()
                model_last_error = None
                model_retryable = False

                while True:
                    candidate_keys = await self._rank_available_keys(all_keys, exhausted_keys_for_this_model)
                    if not candidate_keys:
                        break

                    key_made_progress = False

                    for key in candidate_keys:
                        key_made_progress = True
                        await self._mark_key_selected(key["id"])

                        url = f"{config.BASE_URL}/chat/completions"
                        headers_copy = {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {key['key']}"
                        }

                        request_data = data.copy()
                        request_data["model"] = model["model_id"]
                        request_data["stream"] = True

                        json_data = json.dumps(request_data)

                        try:
                            async with httpx.AsyncClient() as client:
                                async with client.stream(
                                    "POST", url, content=json_data,
                                    headers=headers_copy, timeout=timeout
                                ) as response:
                                    self._update_quota_from_headers(key["id"], response.headers)

                                    if response.status_code == 429:
                                        retry_after = response.headers.get("Retry-After", "60")
                                        raise _PreStreamError(f"上游限流 429，请 {retry_after} 秒后重试")

                                    if response.status_code >= 400:
                                        body_text = await response.aread()
                                        error_detail = body_text.decode('utf-8', errors='replace')[:500]
                                        
                                        try:
                                            error_json = json.loads(error_detail)
                                            if isinstance(error_json, dict) and "error" in error_json:
                                                error_msg = error_json["error"].get("message", error_detail)
                                            else:
                                                error_msg = error_detail
                                        except:
                                            error_msg = error_detail
                                        
                                        if response.status_code == 401:
                                            raise _PreStreamError(f"API Key 无效或已过期: {error_msg}")
                                        elif response.status_code == 402:
                                            raise _PreStreamError(f"API Key 额度不足: {error_msg}")
                                        elif response.status_code == 404:
                                            raise _PreStreamError(f"模型不存在或不可用: {error_msg}")
                                        elif response.status_code >= 500:
                                            raise _PreStreamError(f"上游服务错误 {response.status_code}: {error_msg}")
                                        else:
                                            raise _PreStreamError(f"HTTP {response.status_code}: {error_msg}")

                                    stream_started = True

                                    async for line in response.aiter_lines():
                                        line = line.strip()
                                        if not line:
                                            continue
                                        if not line.startswith("data:"):
                                            continue

                                        payload = line[5:].strip()

                                        if payload == "[DONE]":
                                            yield "data: [DONE]\n\n"
                                            await self._mark_key_success(key["id"])
                                            await self._mark_model_success(model_id)
                                            logger.info(f"✅ 流式完成！模型: {model['name']}, Key: {key['name']}")
                                            return

                                        try:
                                            chunk_data = json.loads(payload)
                                            chunk_data["model"] = model_name
                                            chunk_data = self._normalize_stream_chunk(chunk_data)
                                            yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
                                        except json.JSONDecodeError:
                                            yield f"data: {payload}\n\n"

                                    await self._mark_key_success(key["id"])
                                    await self._mark_model_success(model_id)
                                    yield "data: [DONE]\n\n"
                                    return

                        except _PreStreamError as e:
                            model_last_error = str(e)
                            model_retryable = True
                            status_code = 429 if "429" in str(e) else None
                            await self._mark_key_retryable_failure(key["id"], str(e), status_code=status_code)
                            logger.warning(f"  流式 Key {key['name']} 预流式错误: {e}")
                            exhausted_keys_for_this_model.add(key["id"])
                            continue
                        except httpx.TimeoutException as e:
                            if stream_started:
                                logger.error(f"  流式传输超时: {e}")
                                error_obj = {
                                    "error": {
                                        "message": "请求超时，请稍后重试",
                                        "type": "timeout_error",
                                        "code": "stream_timeout"
                                    }
                                }
                                yield f"data: {json.dumps(error_obj, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                return
                            model_last_error = "请求超时"
                            model_retryable = True
                            await self._mark_key_retryable_failure(key["id"], "请求超时", status_code=None)
                            logger.error(f"  流式 Key {key['name']} 请求超时")
                            exhausted_keys_for_this_model.add(key["id"])
                            continue
                        except httpx.NetworkError as e:
                            if stream_started:
                                logger.error(f"  流式传输网络错误: {e}")
                                error_obj = {
                                    "error": {
                                        "message": "网络连接失败，请检查网络",
                                        "type": "network_error",
                                        "code": "stream_network_error"
                                    }
                                }
                                yield f"data: {json.dumps(error_obj, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                return
                            model_last_error = f"网络错误: {str(e)}"
                            model_retryable = True
                            await self._mark_key_retryable_failure(key["id"], str(e), status_code=None)
                            logger.error(f"  流式 Key {key['name']} 网络错误: {e}")
                            exhausted_keys_for_this_model.add(key["id"])
                            continue
                        except Exception as e:
                            if stream_started:
                                logger.error(f"  流式传输中途异常: {e}", exc_info=True)
                                error_obj = {
                                    "error": {
                                        "message": f"流式传输错误: {str(e)}",
                                        "type": "stream_error",
                                        "code": "stream_internal_error"
                                    }
                                }
                                yield f"data: {json.dumps(error_obj, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                return
                            model_last_error = str(e)
                            model_retryable = False
                            await self._mark_key_fatal_failure(key["id"], str(e))
                            logger.error(f"  流式 Key {key['name']} 连接异常: {e}")
                            exhausted_keys_for_this_model.add(key["id"])
                            continue

                    if not key_made_progress:
                        break

                if model_last_error:
                    if model_retryable:
                        status_code = 429 if "429" in model_last_error else None
                        await self._mark_model_retryable_failure(model_id, model_last_error, status_code=status_code)
                    else:
                        await self._mark_model_fatal_failure(model_id, model_last_error)
                else:
                    await self._mark_model_retryable_failure(model_id, "模型下无可用 Key")

                exhausted_models.add(model_id)

            if not made_progress:
                break

        raise Exception("所有模型和 Key 都调用失败，请检查配置")


api_client = APIClient()
