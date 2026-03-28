import sys
import uvicorn
import httpx
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- 强制 Windows 终端使用 UTF-8 ---
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass
# ----------------------------------

from .settings import config
from .network import api_client

class AddKeyRequest(BaseModel):
    name: str
    key: str

class AddModelRequest(BaseModel):
    name: str
    model_id: str
    category: str = "chat"

class MoveModelRequest(BaseModel):
    direction: str  # "up" or "down"

app = FastAPI(title="ModelScope 智能路由 (Refactored)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Web UI 路由 ---
@app.get("/")
async def root():
    static_path = Path(__file__).parent / "static" / "index.html"
    if static_path.exists():
        return FileResponse(static_path)
    return {"message": "ModelScope Router is running"}

# --- API Key 管理 API ---
@app.get("/api/keys")
async def get_keys():
    """获取 API Keys 列表，包含额度信息"""
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
    new_key = config.add_api_key(req.key, req.name)
    return {"success": True, "key": new_key}

@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: str):
    success = config.delete_api_key(key_id)
    return {"success": success}

# --- 模型信息 API ---
@app.get("/api/models")
async def get_models():
    """获取所有模型，按分类组织"""
    return {
        "models": config.MODELS,
        "categories": config.MODEL_CATEGORIES,
        "models_by_category": config.get_models_by_category()
    }

@app.post("/api/models")
async def add_model(req: AddModelRequest):
    """添加新模型"""
    new_model = config.add_model(req.name, req.model_id, req.category)
    return {"success": True, "model": new_model}

@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str):
    """删除模型"""
    success = config.delete_model(model_id)
    return {"success": success}

@app.post("/api/models/{model_id}/move")
async def move_model(model_id: str, req: MoveModelRequest):
    """移动模型排序"""
    success = config.move_model(model_id, req.direction)
    return {"success": success}

# --- 调用说明 API ---
@app.get("/api/examples")
async def get_examples():
    """获取各种调用示例（简化版）"""
    examples = {
        "chat": {
            "name": "对话 (chat)",
            "description": "文本对话模型，只需要传 model='chat'",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer multi-proxy-2025-2000q" \\
  -d '{
    "model": "chat",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url="http://localhost:2166/v1",
    api_key="multi-proxy-2025-2000q"
)

response = client.chat.completions.create(
    model="chat",
    messages=[
        {"role": "user", "content": "你好"}
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
            "description": "视觉理解模型，支持单图或多图，只需要传 model='vision'",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer multi-proxy-2025-2000q" \\
  -d '{
    "model": "vision",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "这张图片里有什么？"},
          {"type": "image_url", "image_url": {"url": "https://example.com/image1.jpg"}},
          {"type": "image_url", "image_url": {"url": "https://example.com/image2.jpg"}}
        ]
      }
    ]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url="http://localhost:2166/v1",
    api_key="multi-proxy-2025-2000q"
)

# ============ 单图示例 ============
response_single = client.chat.completions.create(
    model="vision",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图片里有什么？"},
                {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
            ]
        }
    ]
)
print("单图结果:", response_single.choices[0].message.content)

# ============ 多图示例 ============
response_multi = client.chat.completions.create(
    model="vision",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "对比这两张图片有什么不同？"},
                {"type": "image_url", "image_url": {"url": "https://example.com/image1.jpg"}},
                {"type": "image_url", "image_url": {"url": "https://example.com/image2.jpg"}}
            ]
        }
    ]
)
print("多图结果:", response_multi.choices[0].message.content)""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "vision"
            }
        },
        "txt2img": {
            "name": "文生图 (txt2img)",
            "description": "文本生成图片，只需要传 model='txt2img'",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer multi-proxy-2025-2000q" \\
  -d '{
    "model": "txt2img",
    "messages": [{"role": "user", "content": "一只可爱的猫咪"}]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url="http://localhost:2166/v1",
    api_key="multi-proxy-2025-2000q"
)

response = client.chat.completions.create(
    model="txt2img",
    messages=[{"role": "user", "content": "一只可爱的猫咪"}]
)

print(response)""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "txt2img"
            }
        },
        "img2img": {
            "name": "图生图 (img2img)",
            "description": "图片生成图片，支持单图或多图输入，只需要传 model='img2img'",
            "curl": """curl -X POST http://localhost:2166/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer multi-proxy-2025-2000q" \\
  -d '{
    "model": "img2img",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "优化这张图片，让它更清晰"},
          {"type": "image_url", "image_url": {"url": "https://example.com/input.jpg"}}
        ]
      }
    ]
  }'""",
            "python": """import openai

client = openai.OpenAI(
    base_url="http://localhost:2166/v1",
    api_key="multi-proxy-2025-2000q"
)

# ============ 单图示例 ============
response_single = client.chat.completions.create(
    model="img2img",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "优化这张图片，让它更清晰"},
                {"type": "image_url", "image_url": {"url": "https://example.com/input.jpg"}}
            ]
        }
    ]
)
print("单图结果:", response_single)

# ============ 多图示例 ============
response_multi = client.chat.completions.create(
    model="img2img",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "融合这两张图片的风格"},
                {"type": "image_url", "image_url": {"url": "https://example.com/input1.jpg"}},
                {"type": "image_url", "image_url": {"url": "https://example.com/input2.jpg"}}
            ]
        }
    ]
)
print("多图结果:", response_multi)""",
            "openai": {
                "base_url": "http://localhost:2166/v1",
                "api_key": "multi-proxy-2025-2000q",
                "model": "img2img"
            }
        }
    }
    return {"examples": examples}

# --- 查询配额 API ---
@app.get("/api/quota")
async def get_quota():
    """查询额度信息"""
    return {"success": True, "quota_info": config.QUOTA_INFO}

# --- 兼容 OpenAI 的聊天接口 ---
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """兼容 OpenAI 的聊天接口"""
    try:
        body = await request.json()
        requested_model = body.get("model", "chat")
        
        # 【新增】分类映射逻辑
        # 外部传入 4 种类型：chat / txt2img / img2img / vision
        category_mapping = {
            "chat": "chat",
            "txt2img": "text2img",
            "img2img": "img2img",
            "vision": "vision"
        }
        
        # 检查是否是分类标识符
        target_category = category_mapping.get(requested_model)
        
        if target_category:
            # 是分类，找到该分类 order=0 的模型
            models_by_cat = config.get_models_by_category()
            cat_models = models_by_cat.get(target_category, [])
            if cat_models:
                # 使用该分类优先级第一的模型
                body["model"] = cat_models[0]["name"]
                model_name = cat_models[0]["name"]
            else:
                # 该分类没有模型，用默认
                model_name = requested_model
        else:
            # 不是分类，直接使用请求的模型名
            model_name = requested_model
        
        # 获取请求头（忽略外部传的 Authorization，内部用自己的 Key 池）
        headers = dict(request.headers)
        # 移除外部的 Authorization，防止干扰
        headers.pop("Authorization", None)
        
        # 调用新的 network 层，它会自动处理 Key 和模型切换
        result, status, resp_headers = await api_client.call_model(
            model_name, body, headers, timeout=60
        )
        
        return JSONResponse(content=result, status_code=status, headers=resp_headers)
        
    except Exception as e:
        return JSONResponse(
            content={"error": {"message": str(e)}},
            status_code=500
        )

@app.on_event("startup")
async def startup_event():
    print(f"Server is running on port 2166...")
    print(f"Web UI: http://localhost:2166")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=2166, log_level="info")
