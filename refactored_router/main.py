import sys
import uvicorn
import uuid
import time
import json
import asyncio
import socket
import logging
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

if sys.platform == "win32":
    from asyncio.proactor_events import _ProactorBasePipeTransport
    _orig_pipe_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost
    def _safe_pipe_call_connection_lost(self, exc):
        try:
            _orig_pipe_call_connection_lost(self, exc)
        except (OSError, socket.error):
            pass
    _ProactorBasePipeTransport._call_connection_lost = _safe_pipe_call_connection_lost
    try:
        from asyncio.proactor_events import _ProactorBaseSocketTransport
        _orig_sock_call_connection_lost = _ProactorBaseSocketTransport._call_connection_lost
        def _safe_sock_call_connection_lost(self, exc):
            try:
                _orig_sock_call_connection_lost(self, exc)
            except (OSError, socket.error):
                pass
        _ProactorBaseSocketTransport._call_connection_lost = _safe_sock_call_connection_lost
    except ImportError:
        pass

current_dir = Path(__file__).parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

from settings import config
from network import api_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def truncate_text(text: str, max_len: int = 500) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def api_error(message: str, status_code: int = 400, **extra):
    content = {"success": False, "error": message}
    content.update(extra)
    return JSONResponse(content=content, status_code=status_code)


def clean_required(value: str) -> str:
    return (value or "").strip()


def find_model_by_identifier(identifier: str):
    for model in config.MODELS:
        if identifier in (model.get("id"), model.get("name"), model.get("model_id")):
            return model
    return None


def extract_messages_content(messages: list) -> str:
    if not messages:
        return ""
    content_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        if role == "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            content_parts.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            if text_parts:
                content_parts.append(f"[{role}]: {' '.join(text_parts)}")
    return "\n".join(content_parts)

def extract_response_content(result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    choices = result.get("choices", [])
    if not choices:
        return ""
    content_parts = []
    for choice in choices:
        message = choice.get("message", {})
        content = message.get("content", "")
        if content:
            content_parts.append(content)
        reasoning = message.get("reasoning_content", "")
        if reasoning:
            content_parts.append(f"[推理过程]: {reasoning}")
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                args = func.get("arguments", "")
                content_parts.append(f"[工具调用]: {name}({args})")
    return "\n".join(content_parts)

def log_request_detail(
    client_ip: str,
    requested_model: str,
    actual_model: str,
    actual_key_name: str,
    input_content: str,
    output_content: str,
    status: str = "success",
    error_msg: str = None
):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = "=" * 80
    
    log_lines = [
        separator,
        f"📋 请求日志 [{timestamp}]",
        separator,
        f"🌐 客户端 IP: {client_ip}",
        f"🎯 请求模型: {requested_model}",
        f"🤖 实际模型: {actual_model}",
        f"🔑 使用 Key: {actual_key_name}",
        f"📊 状态: {status}",
        "-" * 40,
        f"📥 输入内容 (前500字符):",
        truncate_text(input_content, 500),
        "-" * 40,
    ]
    
    if status == "success":
        log_lines.extend([
            f"📤 输出内容 (前500字符):",
            truncate_text(output_content, 500),
        ])
    else:
        log_lines.append(f"❌ 错误信息: {error_msg}")
    
    log_lines.append(separator)
    
    logger.info("\n" + "\n".join(log_lines))


class AddKeyRequest(BaseModel):
    name: str
    key: str


class AddModelRequest(BaseModel):
    name: str
    model_id: str
    category: str = "chat"


class MoveModelRequest(BaseModel):
    direction: str


app = FastAPI(title="ModelScopeApiRouter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    static_path = Path(__file__).parent / "static" / "index.html"
    if static_path.exists():
        return FileResponse(static_path)
    return {"message": "ModelScopeApiRouter is running"}


@app.get("/v1/models")
async def list_models():
    models_list = []
    for model in config.MODELS:
        models_list.append({
            "id": model.get("name", model.get("model_id", "")),
            "object": "model",
            "created": 0,
            "owned_by": model.get("category", "chat")
        })
    for alias in ["chat", "txt2img", "img2img", "vision"]:
        if not any(m["id"] == alias for m in models_list):
            models_list.append({
                "id": alias,
                "object": "model",
                "created": 0,
                "owned_by": "router-alias"
            })
    return {"object": "list", "data": models_list}


@app.get("/api/keys")
async def get_keys():
    keys_with_quota = []
    for key_info in config.API_KEYS:
        key_data = key_info.copy()
        quota = config.get_quota(key_info["id"])
        if quota:
            key_data["quota"] = quota
        keys_with_quota.append(key_data)
    return {"keys": keys_with_quota}


@app.post("/api/keys")
async def add_key(req: AddKeyRequest):
    name = clean_required(req.name)
    key = clean_required(req.key)
    if not name:
        return api_error("Key 名称不能为空")
    if not key:
        return api_error("API Key 不能为空")
    new_key = config.add_api_key(key, name)
    return {"success": True, "key": new_key}


@app.post("/api/keys/test-all")
async def test_all_keys(request: Request = None):
    """一键测试所有 Key 的额度 — 发轻量请求获取实时配额"""
    import httpx
    import asyncio as _asyncio

    if not config.API_KEYS:
        return api_error("没有可用的 API Key", 400)

    model_override = None
    if request:
        try:
            body = await request.json()
            model_override = body.get("model", "").strip()
        except Exception:
            pass

    if model_override:
        test_model_id = model_override
    else:
        models_by_cat = config.get_models_by_category()
        chat_models = models_by_cat.get("chat", [])
        test_model_id = chat_models[0]["model_id"] if chat_models else "deepseek-v3"

    concurrency = 3

    async def test_one(key: dict) -> dict:
        try:
            url = f"{config.BASE_URL}/chat/completions"
            test_data = {
                "model": test_model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1
            }
            headers = {
                "Authorization": f"Bearer {key['key']}",
                "Content-Type": "application/json"
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=test_data, headers=headers, timeout=15)

            daily_limit = resp.headers.get("modelscope-ratelimit-requests-limit")
            daily_remaining = resp.headers.get("modelscope-ratelimit-requests-remaining")
            model_limit = resp.headers.get("modelscope-ratelimit-model-requests-limit")
            model_remaining = resp.headers.get("modelscope-ratelimit-model-requests-remaining")

            quota = {}
            if daily_limit: quota["daily_limit"] = int(daily_limit)
            if daily_remaining: quota["daily_remaining"] = int(daily_remaining)
            if model_limit: quota["model_limit"] = int(model_limit)
            if model_remaining: quota["model_remaining"] = int(model_remaining)

            if quota:
                config.update_quota(key["id"], quota)
                return {
                    "key_id": key["id"],
                    "key_name": key["name"],
                    "success": resp.status_code < 400,
                    "quota": quota,
                    "status": resp.status_code
                }
            else:
                return {
                    "key_id": key["id"],
                    "key_name": key["name"],
                    "success": resp.status_code < 400,
                    "status": resp.status_code,
                    "quota": None,
                    "note": "响应头无额度信息"
                }
        except Exception as e:
            return {
                "key_id": key["id"],
                "key_name": key["name"],
                "success": False,
                "error": str(e)
            }

    sem = _asyncio.Semaphore(concurrency)
    async def worker(key):
        async with sem:
            return await test_one(key)

    results = await _asyncio.gather(*[worker(key) for key in config.API_KEYS])

    ok = sum(1 for r in results if r.get("success"))
    return {
        "success": True,
        "total": len(results),
        "ok": ok,
        "fail": len(results) - ok,
        "results": results
    }


@app.post("/api/keys/{key_id}/test")
async def test_single_key(key_id: str, request: Request = None):
    import httpx

    key = next((k for k in config.API_KEYS if k["id"] == key_id), None)
    if not key:
        return api_error("Key 不存在", 404)

    model_override = None
    if request:
        try:
            body = await request.json()
            model_override = body.get("model", "").strip()
        except Exception:
            pass

    if model_override:
        test_model_id = model_override
    else:
        models_by_cat = config.get_models_by_category()
        chat_models = models_by_cat.get("chat", [])
        test_model_id = chat_models[0]["model_id"] if chat_models else "deepseek-v3"

    try:
        url = f"{config.BASE_URL}/chat/completions"
        test_data = {
            "model": test_model_id,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1
        }
        headers = {
            "Authorization": f"Bearer {key['key']}",
            "Content-Type": "application/json"
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=test_data, headers=headers, timeout=15)

        daily_limit = resp.headers.get("modelscope-ratelimit-requests-limit")
        daily_remaining = resp.headers.get("modelscope-ratelimit-requests-remaining")
        model_limit = resp.headers.get("modelscope-ratelimit-model-requests-limit")
        model_remaining = resp.headers.get("modelscope-ratelimit-model-requests-remaining")

        quota = {}
        if daily_limit: quota["daily_limit"] = int(daily_limit)
        if daily_remaining: quota["daily_remaining"] = int(daily_remaining)
        if model_limit: quota["model_limit"] = int(model_limit)
        if model_remaining: quota["model_remaining"] = int(model_remaining)

        if quota:
            config.update_quota(key["id"], quota)
            return {
                "key_id": key["id"],
                "key_name": key["name"],
                "success": resp.status_code < 400,
                "quota": quota,
                "status": resp.status_code
            }
        else:
            return {
                "key_id": key["id"],
                "key_name": key["name"],
                "success": resp.status_code < 400,
                "status": resp.status_code,
                "quota": None,
                "note": "响应头无额度信息"
            }
    except Exception as e:
        return {
            "key_id": key["id"],
            "key_name": key["name"],
            "success": False,
            "error": str(e)
        }


@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: str):
    success = config.delete_api_key(key_id)
    if not success:
        return api_error("Key 不存在", 404)
    return {"success": True}


@app.get("/api/models")
async def get_models():
    return {
        "models": config.MODELS,
        "categories": config.MODEL_CATEGORIES,
        "models_by_category": config.get_models_by_category()
    }


@app.post("/api/models")
async def add_model(req: AddModelRequest):
    name = clean_required(req.name)
    model_id = clean_required(req.model_id)
    category = clean_required(req.category) or "chat"
    if not name:
        return api_error("模型名称不能为空")
    if not model_id:
        return api_error("Model ID 不能为空")
    if category not in config.MODEL_CATEGORIES:
        return api_error("模型分类无效")
    new_model = config.add_model(name, model_id, category)
    return {"success": True, "model": new_model}


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str, category: str = Query(...)):
    success = config.delete_model(model_id, category)
    if not success:
        return api_error("模型不存在或分类不匹配", 404)
    return {"success": True}


@app.post("/api/models/{model_id}/move")
async def move_model(model_id: str, req: MoveModelRequest):
    direction = clean_required(req.direction)
    if direction not in ("up", "down"):
        return api_error("移动方向无效")
    success = config.move_model(model_id, direction)
    if not success:
        return api_error("模型不存在，或已经在当前方向的边界位置", 400)
    return {"success": True}


@app.post("/api/models/{model_id}/test")
async def test_model(model_id: str):
    """测试模型是否可用 - 多 Key 轮换 + 重试，避免因上游偶发空响应误报"""
    import httpx
    from settings import config
    import json as _json
    import asyncio as _asyncio

    target_model = find_model_by_identifier(model_id)

    if not target_model:
        return api_error("模型不存在", 404)

    if not config.API_KEYS:
        return api_error("没有可用的 API Key", 400)

    category = target_model.get("category", "chat")
    max_retries = 2
    last_error = None

    for key in config.API_KEYS:
        for attempt in range(max_retries):
            try:
                if category in ("txt2img", "img2img"):
                    url = f"{config.BASE_URL}/images/generations"
                    test_data = {
                        "model": target_model["model_id"],
                        "prompt": "test"
                    }
                    headers = {
                        "Authorization": f"Bearer {key['key']}",
                        "Content-Type": "application/json",
                        "X-ModelScope-Async-Mode": "true"
                    }
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(url, content=_json.dumps(test_data), headers=headers, timeout=20)
                        if resp.status_code == 429:
                            last_error = f"[{key['name']}] 被限流(429)"
                            break
                        if resp.status_code >= 400:
                            last_error = f"[{key['name']}] HTTP {resp.status_code}"
                            break
                        result = resp.json()
                        if not isinstance(result, dict):
                            last_error = f"[{key['name']}] 返回非JSON格式"
                            break
                        task_id = result.get("task_id", "")
                        if task_id:
                            return {"success": True, "task_id": task_id, "model": target_model["model_id"], "key_name": key["name"]}
                        if result.get("image_url") or result.get("choices"):
                            return {"success": True, "model": target_model["model_id"], "key_name": key["name"]}
                        last_error = f"[{key['name']}] 返回空响应(choices=null)"
                        if attempt < max_retries - 1:
                            await _asyncio.sleep(0.5)
                        continue
                else:
                    url = f"{config.BASE_URL}/chat/completions"
                    test_data = {
                        "model": target_model["model_id"],
                        "messages": [{"role": "user", "content": "hi"}]
                    }
                    headers = {
                        "Authorization": f"Bearer {key['key']}",
                        "Content-Type": "application/json"
                    }
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(url, content=_json.dumps(test_data), headers=headers, timeout=20)
                        if resp.status_code == 429:
                            last_error = f"[{key['name']}] 被限流(429)"
                            break
                        if resp.status_code >= 400:
                            last_error = f"[{key['name']}] HTTP {resp.status_code}"
                            break
                        result = resp.json()
                        choices = result.get("choices")
                        if isinstance(choices, list) and choices:
                            msg = choices[0].get("message", {})
                            content = msg.get("content", "")
                            if content and content.strip():
                                return {"success": True, "model": target_model["model_id"], "content": content[:80], "key_name": key["name"]}
                        last_error = f"[{key['name']}] 返回空响应(choices=null)"
                        if attempt < max_retries - 1:
                            await _asyncio.sleep(0.5)
                        continue

            except Exception as e:
                last_error = f"[{key['name']}] {e}"
                if attempt < max_retries - 1:
                    await _asyncio.sleep(0.5)
                    continue
                break

    return api_error(last_error or "所有 Key 均失败", 400)


@app.get("/api/examples")
async def get_examples():
    examples = {
        "chat": {
            "name": "对话 (chat)",
            "description": "文本对话模型，只需要传 model='chat'",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H \"Content-Type: application/json\" \\
  -H \"Authorization: Bearer multi-proxy-2025-2000q\" \\
  -d '{
    \"model\": \"chat\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"你好，你是什么大模型？\"}
    ]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url=\"http://localhost:2166/v1\",
    api_key=\"multi-proxy-2025-2000q\"
)

response = client.chat.completions.create(
    model=\"chat\",
    messages=[
        {\"role\": \"user\", \"content\": \"你好，你是什么大模型？\"}
    ]
)

print(response.choices[0].message.content)""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "chat"
            }
        },
        "vision": {
            "name": "视觉理解 (vision)",
            "description": "视觉理解模型，支持单图或多图，只需要传 model='vision'！ 注：url字段，直接传入图片的base64编码完整字符串也是可以的！",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H \"Content-Type: application/json\" \\
  -H \"Authorization: Bearer multi-proxy-2025-2000q\" \\
  -d '{
    \"model\": \"vision\",
    \"messages\": [
      {
        \"role\": \"user\",
        \"content\": [
          {\"type\": \"text\", \"text\": \"这张图片里描绘了什么？\"},
          {\"type\": \"image_url\", \"image_url\": {\"url\": \"https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg\"}}
        ]
      }
    ]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url=\"http://localhost:2166/v1\",
    api_key=\"multi-proxy-2025-2000q\"
)

response = client.chat.completions.create(
    model=\"vision\",
    messages=[
        {
            \"role\": \"user\",
            \"content\": [
                # 注：url字段，直接传入图片的base64编码完整字符串也可以！
                {\"type\": \"text\", \"text\": \"这张图片里描绘了什么？\"},
                {\"type\": \"image_url\", \"image_url\": {\"url\": \"https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg\"}}
            ]
        }
    ]
)
print(response.choices[0].message.content)""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "vision"
            },
            "base64": """import base64
import os

def image_to_base64_url(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    mime_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"
    }
    mime_type = mime_types.get(ext, "image/jpeg")
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{base64_image}"

# 将本地图片转为 base64 URL，直接填入 url 字段即可
image_url = image_to_base64_url("1.png")

# 然后在请求中使用：
# {"type": "image_url", "image_url": {"url": image_url}}"""
        },
        "txt2img": {
            "name": "文生图 (txt2img)",
            "description": "文本生成图片，只需要传 model='txt2img'",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H \"Content-Type: application/json\" \\
  -H \"Authorization: Bearer multi-proxy-2025-2000q\" \\
  -d '{
    \"model\": \"txt2img\",
    \"messages\": [{\"role\": \"user\", \"content\": \"一直金毛正面端坐，毛色金黄光亮，秋天，森林，林荫小道，微风，带牛仔帽，帅气拉风很酷，写实风格\"}]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url=\"http://localhost:2166/v1\",
    api_key=\"multi-proxy-2025-2000q\"
)

response = client.chat.completions.create(
    model=\"txt2img\",
    messages=[{\"role\": \"user\", \"content\": \"一直金毛正面端坐，毛色金黄光亮，秋天，森林，林荫小道，微风，带牛仔帽，帅气拉风很酷，写实风格\"}]
)

print(f\"图片链接: {response.choices[0].message.content}\")
print(f\"图片链接 (直接访问): {response.image_url}\")
print(f\"图片链接 (数组): {response.images[0]}\")""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "txt2img",
                "note": "技术实现：采用 ModelScope 异步模式（X-ModelScope-Async-Mode: true），优先处理非空 task_id 并轮询任务状态（最多 60 次，每 2~8 秒一次），如果上游直接返回图片链接也会直接提取并返回，再从 output_images 数组中提取图片链接！"
            }
        },
        "img2img": {
            "name": "图生图 (img2img)",
            "description": "图片生成图片，当前会提取首张输入图片并转为上游要求的单个 image_url 字符串，只需要传 model='img2img'！ 注：url字段同样支持传入base64编码字符串！",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H \"Content-Type: application/json\" \\
  -H \"Authorization: Bearer multi-proxy-2025-2000q\" \\
  -d '{
    \"model\": \"img2img\",
    \"messages\": [
      {
        \"role\": \"user\",
        \"content\": [
          {\"type\": \"text\", \"text\": \"优化这张图片，添加一层很淡的雾效或柔光，整体更具朦胧和神秘的美感\"},
          {\"type\": \"image_url\", \"image_url\": {\"url\": \"https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg\"}}
        ]
      }
    ]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url=\"http://localhost:2166/v1\",
    api_key=\"multi-proxy-2025-2000q\"
)

response = client.chat.completions.create(
    model=\"img2img\",
    messages=[
        {
            \"role\": \"user\",
            \"content\": [
                # 注：url字段，直接传入图片的base64编码完整字符串也可以！
                {\"type\": \"text\", \"text\": \"优化这张图片，添加一层很淡的雾效或柔光，整体更具朦胧和神秘的美感\"},
                {\"type\": \"image_url\", \"image_url\": {\"url\": \"https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg\"}}
            ]
        }
    ]
)

print(f\"图片链接: {response.choices[0].message.content}\")
print(f\"图片链接 (直接访问): {response.image_url}\")
print(f\"图片链接 (数组): {response.images[0]}\")""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "img2img",
                "note": "技术实现：采用 ModelScope 异步模式（X-ModelScope-Async-Mode: true），优先处理非空 task_id 并轮询任务状态（最多 120 次，每 2~8 秒一次），如果上游直接返回图片链接也会直接提取并返回，再从 output_images 数组中提取图片链接！"
            },
            "base64": """import base64
import os

def image_to_base64_url(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    mime_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"
    }
    mime_type = mime_types.get(ext, "image/jpeg")
    with open(image_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{base64_image}"

# 将本地图片转为 base64 URL，直接填入 url 字段即可
image_url = image_to_base64_url("1.png")

# 然后在请求中使用：
# {"type": "image_url", "image_url": {"url": image_url}}"""
        }
    }
    return {"examples": examples}


async def _sse_stream(result: dict, model_name: str, chunk_size: int = 2, include_usage: bool = False):
    """将完整响应拆分为 OpenAI 兼容的 SSE chunks（含 reasoning_content 和 tool_calls 增量支持）

    OpenAI streaming 规范:
    - reasoning_content: 思考链内容，逐 chunk 发送（兼容 DeepSeek-R1 等思考模型）
    - tool_calls 增量: 首个 chunk 发 id+type+name，后续 chunk 发 arguments 片段
    - usage: 最后一个 chunk 可选包含 token 用量（当 stream_options.include_usage=true）
    """
    content = ""
    reasoning_content = ""
    tool_calls = None
    finish_reason = "stop"
    usage = result.get("usage")

    choices = result.get("choices", [])
    if choices and isinstance(choices, list):
        first_choice = choices[0] if choices else {}
        if isinstance(first_choice, dict):
            finish_reason = first_choice.get("finish_reason") or "stop"
            msg = first_choice.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", "") or ""
                reasoning_content = msg.get("reasoning_content", "") or ""
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    finish_reason = first_choice.get("finish_reason") or "tool_calls"

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _make_chunk(delta: dict, fr=None, usage_data=None):
        chunk = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]
        }
        if usage_data is not None:
            chunk["usage"] = usage_data
        return chunk

    # 1. role
    yield f"data: {json.dumps(_make_chunk({'role': 'assistant'}), ensure_ascii=False)}\n\n"
    await asyncio.sleep(0.01)

    # 2. reasoning_content 逐 chunk 发送（思考链，在正文之前）
    if reasoning_content:
        i = 0
        while i < len(reasoning_content):
            text = reasoning_content[i : i + chunk_size]
            i += chunk_size
            yield f"data: {json.dumps(_make_chunk({'reasoning_content': text}), ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)

    # 3. content 逐 chunk 发送
    i = 0
    while i < len(content):
        text = content[i : i + chunk_size]
        i += chunk_size
        yield f"data: {json.dumps(_make_chunk({'content': text}), ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.01)

    # 4. tool_calls 增量发送（严格遵循 OpenAI streaming 格式）
    if tool_calls and isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_index = tc.get("index", 0)
            tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:24]}")
            tc_type = tc.get("type", "function")
            tc_func = tc.get("function", {})
            func_name = tc_func.get("name", "") if isinstance(tc_func, dict) else ""
            func_args = tc_func.get("arguments", "") if isinstance(tc_func, dict) else ""
            if not isinstance(func_args, str):
                func_args = json.dumps(func_args, ensure_ascii=False)

            # 4a. 首个 chunk：发送 id + type + function.name
            delta_tc = {
                "index": tc_index,
                "id": tc_id,
                "type": tc_type,
                "function": {"name": func_name, "arguments": ""}
            }
            yield f"data: {json.dumps(_make_chunk({'tool_calls': [delta_tc]}), ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)

            # 4b. 逐段发送 arguments 字符串
            arg_chunk_size = 64
            j = 0
            while j < len(func_args):
                arg_part = func_args[j : j + arg_chunk_size]
                j += arg_chunk_size
                delta_tc_arg = {
                    "index": tc_index,
                    "function": {"arguments": arg_part}
                }
                yield f"data: {json.dumps(_make_chunk({'tool_calls': [delta_tc_arg]}), ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.005)

    # 5. finish
    yield f"data: {json.dumps(_make_chunk({}, fr=finish_reason), ensure_ascii=False)}\n\n"

    # 6. usage（当 stream_options.include_usage=true 时）
    if include_usage and usage and isinstance(usage, dict):
        yield f"data: {json.dumps(_make_chunk({}, fr=None, usage_data=usage), ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


def _message_has_image(messages) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                return True
    return False


def _resolve_model_request(requested_model: str, body: dict):
    matches = [model for model in config.MODELS if model.get("name") == requested_model or model.get("model_id") == requested_model]
    if not matches:
        return requested_model, "chat"

    if len(matches) == 1:
        return requested_model, matches[0].get("category", "chat")

    categories = {model.get("category", "chat") for model in matches}
    messages = body.get("messages", []) if isinstance(body, dict) else []

    if _message_has_image(messages) and "vision" in categories:
        return requested_model, "vision"

    if "chat" in categories:
        return requested_model, "chat"

    category_priority = ("vision", "txt2img", "img2img")
    for category in category_priority:
        if category in categories:
            return requested_model, category

    return requested_model, matches[0].get("category", "chat")


async def _sse_error_stream(message: str, error_type: str = "server_error", error_code: str = None):
    """SSE 错误流"""
    error_obj = {"message": message, "type": error_type}
    if error_code:
        error_obj["code"] = error_code
    yield f"data: {json.dumps({'error': error_obj}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def _normalize_response(result: dict, requested_model: str) -> dict:
    """将 ModelScope 上游响应规范化为严格 OpenAI 格式

    清理内容:
    1. 移除 choices[].delta (非流式响应不应有 delta)
    2. 移除 message 中的非标准字段: function_calls, reasoning_content 等
    3. 修正 system_fingerprint: 空字符串改为省略
    4. 确保响应 model 字段为客户端请求的 model 名
    """
    if not isinstance(result, dict):
        return result

    if "id" not in result or not result["id"]:
        result["id"] = f"chatcmpl-{uuid.uuid4().hex[:29]}"

    if "object" not in result:
        result["object"] = "chat.completion"

    if "created" not in result:
        result["created"] = int(time.time())

    result["model"] = requested_model

    # 清理 system_fingerprint
    if "system_fingerprint" in result:
        sf = result["system_fingerprint"]
        if sf is None or sf == "":
            del result["system_fingerprint"]

    # 规范化 choices
    choices = result.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue

            # 非流式响应不应有 delta 字段
            if "delta" in choice:
                del choice["delta"]

            # 规范化 message
            message = choice.get("message")
            if isinstance(message, dict):
                # 移除 ModelScope 特有的非标准字段
                # function_calls 是 tool_calls 的旧版本重复，会混淆 Agent
                if "function_calls" in message:
                    del message["function_calls"]

                # reasoning_content 是思考链内容，保留但重命名为标准扩展
                # OpenAI 没有此字段，但一些 SDK 支持。保留以兼容思考模型。
                # 不做删除，但也不主动添加

                # 当 tool_calls 存在且 content 为空字符串时，设为 None
                # 某些 Agent 框架期望 tool_calls 时 content 为 null
                tc = message.get("tool_calls")
                if tc and isinstance(tc, list) and len(tc) > 0:
                    content = message.get("content")
                    if content is not None and not content.strip():
                        message["content"] = None

    return result


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    is_stream = False
    requested_model = "unknown"
    input_content = ""
    client_ip = request.client.host if request.client else "unknown"
    client_port = request.client.port if request.client else "unknown"
    client_addr = f"{client_ip}:{client_port}" if client_port != "unknown" else client_ip
    
    try:
        body = await request.json()
        requested_model = body.get("model", "chat")
        is_stream = body.get("stream", False)
        include_usage = False
        stream_options = body.pop("stream_options", None)
        if isinstance(stream_options, dict) and stream_options.get("include_usage"):
            include_usage = True

        if is_stream:
            body.pop("stream", None)
        else:
            body.pop("stream", None)

        if "messages" not in body or not isinstance(body.get("messages"), list) or len(body["messages"]) == 0:
            return JSONResponse(
                content={
                    "error": {
                        "message": "'messages' is required and must be a non-empty array.",
                        "type": "invalid_request_error",
                        "code": "missing_messages"
                    }
                },
                status_code=400
            )

        category_mapping = {
            "chat": "chat",
            "txt2img": "txt2img",
            "img2img": "img2img",
            "vision": "vision"
        }

        target_category = category_mapping.get(requested_model)

        if target_category:
            model_name = requested_model
        else:
            model_name, target_category = _resolve_model_request(requested_model, body)

        if target_category:
            body["_router_category"] = target_category

        if is_stream and target_category in ("txt2img", "img2img"):
            return JSONResponse(
                content={
                    "error": {
                        "message": "txt2img/img2img 图片生成模型不支持 stream=true，请使用非流式请求。vision 视觉理解模型仍支持流式调用。",
                        "type": "invalid_request_error",
                        "code": "image_stream_not_supported"
                    }
                },
                status_code=400
            )

        headers = dict(request.headers)
        headers.pop("Authorization", None)

        call_timeout = 300
        
        input_content = extract_messages_content(body.get("messages", []))

        if is_stream:
            collected_content = []

            async def _combined_stream():
                try:
                    async for chunk in api_client.call_model_stream(model_name, body, headers, timeout=call_timeout, client_ip=client_addr):
                        try:
                            chunk_str = chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk
                            if chunk_str.startswith("data:"):
                                chunk_data_str = chunk_str[5:].strip()
                                if chunk_data_str and chunk_data_str != "[DONE]":
                                    chunk_data = json.loads(chunk_data_str)
                                    choices = chunk_data.get("choices", [])
                                    for choice in choices:
                                        delta = choice.get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            collected_content.append(content)
                        except Exception as e:
                            logger.warning(f"处理流式 chunk 失败: {e}")
                        yield chunk
                    
                    output_content = "".join(collected_content)
                    logger.info(
                        f"\n{'='*80}\n"
                        f"📋 流式请求完成\n"
                        f"{'='*80}\n"
                        f"🌐 客户端: {client_addr}\n"
                        f"🎯 请求模型: {requested_model}\n"
                        f"📥 输入内容 (前500字符):\n{truncate_text(input_content, 500)}\n"
                        f"{'-'*40}\n"
                        f"📤 输出内容 (前500字符):\n{truncate_text(output_content, 500)}\n"
                        f"{'='*80}"
                    )
                    
                except asyncio.CancelledError:
                    logger.warning(f"客户端断开连接: {client_addr}")
                    raise
                    
                except Exception as e:
                    logger.error(f"流式传输错误: {e}", exc_info=True)
                    error_chunk = json.dumps({
                        "error": {
                            "message": f"流式传输错误: {str(e)}",
                            "type": "stream_error",
                            "code": "stream_internal_error"
                        }
                    }, ensure_ascii=False)
                    yield f"data: {error_chunk}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                _combined_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive"
                }
            )

        result, status, resp_headers, call_info = await api_client.call_model(
            model_name, body, headers, timeout=call_timeout, client_ip=client_addr
        )

        normalized = _normalize_response(result, requested_model)
        
        output_content = extract_response_content(normalized)
        
        log_request_detail(
            client_ip=client_addr,
            requested_model=requested_model,
            actual_model=call_info.get("actual_model", model_name) if call_info else model_name,
            actual_key_name=call_info.get("actual_key_name", "unknown") if call_info else "unknown",
            input_content=input_content,
            output_content=output_content,
            status="success"
        )

        safe_response_headers = {
            k: v for k, v in resp_headers.items()
            if k.lower() not in {
                "content-length",
                "transfer-encoding",
                "connection",
                "date",
                "server",
                "content-encoding"
            }
        }

        return JSONResponse(content=normalized, status_code=status, headers=safe_response_headers)

    except Exception as e:
        log_request_detail(
            client_ip=client_addr,
            requested_model=requested_model,
            actual_model="N/A",
            actual_key_name="N/A",
            input_content=input_content,
            output_content="",
            status="failed",
            error_msg=str(e)
        )
        
        if is_stream:
            return StreamingResponse(
                _sse_error_stream(str(e)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"}
            )
        error_type = "server_error"
        error_code = "internal_error"
        status_code = 500
        error_msg = str(e)

        if "没有可用的 API Key" in error_msg:
            error_type = "invalid_request_error"
            error_code = "no_api_key"
            status_code = 401
        elif "所有模型和 Key 都调用失败" in error_msg:
            error_type = "server_error"
            error_code = "all_models_failed"
            status_code = 503

        return JSONResponse(
            content={
                "error": {
                    "message": error_msg,
                    "type": error_type,
                    "code": error_code
                }
            },
            status_code=status_code
        )


@app.on_event("startup")
async def startup_event():
    print(f"Server is running on port {config.PORT}...")
    print(f"Web UI: http://localhost:{config.PORT}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="info")
