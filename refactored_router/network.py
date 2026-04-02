import sys
import httpx
import logging
import json
import base64
import re
import asyncio
from pathlib import Path
from typing import Tuple, Dict, List, Optional

# --- 添加模块路径 ---
current_dir = Path(__file__).parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))
# ------------------

from settings import config

logger = logging.getLogger(__name__)

class APIClient:
    def __init__(self):
        self.client = None
    
    def _update_quota_from_headers(self, key_id: str, headers: dict):
        """从 ModelScope 响应头提取额度信息"""
        quota = {}
        
        for h in headers:
            if h.lower() == "modelscope-ratelimit-tpm":
                quota["tpm"] = int(headers[h])
            elif h.lower() == "modelscope-ratelimit-rpm":
                quota["rpm"] = int(headers[h])
            elif h.lower() == "modelscope-ratelimit-model-limit":
                quota["model_limit"] = int(headers[h])
            elif h.lower() == "modelscope-ratelimit-daily-remaining":
                quota["daily_remaining"] = int(headers[h])
            elif h.lower() == "modelscope-ratelimit-daily-limit":
                quota["daily_limit"] = int(headers[h])
        
        config.update_quota(key_id, quota)
        return quota
    
    def is_key_exhausted(self, key_id: str, quota: Dict) -> bool:
        """判断 Key 是否完全不可用（额度全用完或报错失败）"""
        model_exhausted = (
            quota.get("model_limit", 0) > 0 and 
            quota.get("daily_remaining", 0) <= 0
        )
        return model_exhausted
    
    def should_try_next_key(self, quota: Dict) -> bool:
        """判断是否应该尝试下一个 Key（当前 Key 失败或模型额度用完，但每日还有额度）"""
        model_quota_exhausted = (
            quota.get("model_limit", 0) > 0 and 
            quota.get("daily_remaining", 0) < quota.get("model_limit", 0) and
            quota.get("daily_remaining", 0) > 0
        )
        return model_quota_exhausted
    
    def _extract_image_url(self, result: dict) -> Optional[str]:
        """从响应中提取图片 URL"""
        try:
            # 尝试 OpenAI 格式：choices[0].message.content
            if "choices" in result and len(result["choices"]) > 0:
                choice = result["choices"][0]
                if "message" in choice and "content" in choice["message"]:
                    content = choice["message"]["content"]
                    # 检查 content 是否是图片 URL
                    if content and (content.startswith("http://") or content.startswith("https://") or content.startswith("data:image")):
                        return content
            
            # 尝试直接查找 image_url 字段
            if "image_url" in result:
                return result["image_url"]
            
            # 尝试查找 url 字段
            if "url" in result:
                return result["url"]
            
            # 尝试查找 images 数组
            if "images" in result and isinstance(result["images"], list) and len(result["images"]) > 0:
                return result["images"][0]
            
            # 尝试查找 image 字段
            if "image" in result:
                return result["image"]
            
            # 尝试在任意地方查找 URL
            result_str = json.dumps(result)
            url_match = re.search(r'https?://[^\s"\'<>]+', result_str)
            if url_match:
                return url_match.group(0)
            
        except Exception as e:
            logger.warning(f"提取图片 URL 失败: {e}")
        
        return None
    
    def _validate_image_size(self, data: dict) -> Tuple[bool, Optional[str]]:
        """校验图片尺寸是否在合理范围内（1:3 到 3:1）"""
        try:
            # 检查 OpenAI 多模态消息里的 image_url 输入（用于 img2img）
            if "messages" in data and isinstance(data["messages"], list):
                for message in data["messages"]:
                    content = message.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if item.get("type") == "image_url":
                                image_obj = item.get("image_url", {})
                                width = image_obj.get("width")
                                height = image_obj.get("height")
                                if width and height:
                                    ratio = width / height
                                    if ratio < 1/3 or ratio > 3:
                                        return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"

            # 检查是否有 images 或 image_url 字段（用于 img2img）
            if "images" in data:
                images = data["images"]
                if isinstance(images, list) and len(images) > 0:
                    for img in images:
                        if isinstance(img, dict):
                            width = img.get("width", 1024)
                            height = img.get("height", 1024)
                            ratio = width / height
                            if ratio < 1/3 or ratio > 3:
                                return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"

            if "image_url" in data:
                image_urls = data["image_url"]
                if isinstance(image_urls, list) and len(image_urls) > 0:
                    for img in image_urls:
                        if isinstance(img, dict):
                            width = img.get("width", 1024)
                            height = img.get("height", 1024)
                            ratio = width / height
                            if ratio < 1/3 or ratio > 3:
                                return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"
            
            # 检查是否有 width 和 height 字段
            width = data.get("width", 1024)
            height = data.get("height", 1024)
            ratio = width / height
            if ratio < 1/3 or ratio > 3:
                return False, f"图片尺寸比例必须在 1:3 到 3:1 之间，当前比例为 {width}:{height}"
            
            return True, None
        except Exception as e:
            logger.warning(f"图片尺寸校验异常: {e}")
            return True, None
    
    def _convert_openai_to_modelscope(self, data: dict, target_category: str) -> dict:
        """将 OpenAI 格式的请求转换为 ModelScope 文生图/图生图格式"""
        try:
            modelscope_data = {}
            
            # 提取 prompt
            if "messages" in data and len(data["messages"]) > 0:
                last_message = data["messages"][-1]
                content = last_message.get("content", "")
                
                if isinstance(content, str):
                    # 文生图：简单文本
                    modelscope_data["prompt"] = content
                elif isinstance(content, list):
                    # 图生图：文本 + 图片
                    text_parts = []
                    image_urls = []
                    
                    for item in content:
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "image_url":
                            img_url = item.get("image_url", {}).get("url", "")
                            if img_url:
                                image_urls.append(img_url)
                    
                    if text_parts:
                        modelscope_data["prompt"] = " ".join(text_parts)
                    
                    if image_urls and target_category == "img2img":
                        # ModelScope 图生图接口的 image_url 按官方示例使用单个字符串
                        modelscope_data["image_url"] = image_urls[0]
            
            # 复制其他可能的参数
            for key in ["width", "height", "size", "n", "quality"]:
                if key in data:
                    modelscope_data[key] = data[key]
            
            return modelscope_data
        except Exception as e:
            logger.warning(f"请求格式转换失败: {e}")
            return data
    
    def _format_image_response(self, result: dict, image_url: str, original_data: dict) -> dict:
        """格式化响应，确保包含图片链接"""
        try:
            # 构建标准的 OpenAI 兼容响应格式，同时包含图片链接
            formatted_result = result.copy()
            
            # 如果没有 choices 或者 choices 为空，创建一个
            if "choices" not in formatted_result or len(formatted_result["choices"]) == 0:
                formatted_result["choices"] = [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": image_url
                    },
                    "finish_reason": "stop"
                }]
            else:
                # 更新第一个 choice 的 content
                if "message" in formatted_result["choices"][0]:
                    # 如果 content 不是图片链接，替换为图片链接
                    current_content = formatted_result["choices"][0]["message"].get("content", "")
                    if not (current_content.startswith("http://") or current_content.startswith("https://") or current_content.startswith("data:image")):
                        formatted_result["choices"][0]["message"]["content"] = image_url
            
            # 添加 image_url 字段便于直接访问
            formatted_result["image_url"] = image_url
            formatted_result["images"] = [image_url]
            
            return formatted_result
        except Exception as e:
            logger.warning(f"格式化响应失败: {e}")
            return result
    
    async def call_model(self, model_name: str, data: dict, headers: dict, timeout: int) -> Tuple[dict, int, dict]:
        """
        新的核心切换逻辑：
        1. 先确定请求的分类（根据 model_name 或默认 chat）
        2. 获取该分类的模型列表（按 order 排序）
        3. 对每个模型，尝试所有可用的 Key
        4. Key 失败 → 换 Key；当前模型所有 Key 全废 → 换模型
        """
        
        # 步骤 1：确定分类
        target_category = "chat"
        for model in config.MODELS:
            if model.get("name") == model_name:
                target_category = model.get("category", "chat")
                break
        
        # 步骤 1.5：对图生图请求进行图片尺寸校验
        if target_category == "img2img":
            valid, error_msg = self._validate_image_size(data)
            if not valid:
                raise Exception(error_msg)
        
        # 步骤 2：获取该分类的模型列表
        models_by_category = config.get_models_by_category()
        category_models = models_by_category.get(target_category, [])
        
        if not category_models:
            category_models = config.MODELS
        
        all_keys = config.API_KEYS
        
        if not all_keys:
            raise Exception("没有可用的 API Key")
        
        logger.info(f"请求分类: {target_category}, 模型数: {len(category_models)}, Key 数: {len(all_keys)}")
        
        # 步骤 3 & 4：逐个模型尝试
        for model in category_models:
            logger.info(f"尝试模型: {model['name']} ({model['model_id']})")
            
            exhausted_keys_for_this_model = set()
            
            for key in all_keys:
                if key["id"] in exhausted_keys_for_this_model:
                    continue
                
                try:
                    # 根据请求分类确定 URL
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
                    
                    # 请求体里设置 model
                    request_data = data.copy()
                    
                    # 对文生图和图生图进行格式转换
                    if target_category in ["text2img", "img2img"]:
                        request_data = self._convert_openai_to_modelscope(request_data, target_category)
                    
                    request_data["model"] = model["model_id"]
                    
                    logger.info(f"  使用 Key: {key['name']}")
                    
                    json_data = json.dumps(request_data)
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            url,
                            content=json_data,
                            headers=headers_copy,
                            timeout=timeout
                        )
                    
                    quota = self._update_quota_from_headers(key["id"], response.headers)
                    
                    if response.status_code >= 400:
                        try:
                            error_text = response.text
                        except:
                            error_text = str(response)
                        logger.warning(f"  Key {key['name']} HTTP {response.status_code} 错误: {error_text[:300]}")
                        exhausted_keys_for_this_model.add(key["id"])
                        continue
                    
                    if self.should_try_next_key(quota):
                        logger.info(f"  Key {key['name']} 模型额度用完，换 Key")
                        exhausted_keys_for_this_model.add(key["id"])
                        continue
                    
                    if self.is_key_exhausted(key["id"], quota):
                        logger.info(f"  Key {key['name']} 完全耗尽")
                        exhausted_keys_for_this_model.add(key["id"])
                        continue
                    
                    result = response.json()
                    
                    # 对文生图和图生图响应进行特殊处理（异步模式）
                    if target_category in ["text2img", "img2img"]:
                        # 检查是否有非空 task_id（异步模式）
                        task_id = result.get("task_id")
                        if task_id:
                            logger.info(f"  异步任务已提交，task_id: {task_id}")

                            # 轮询任务状态
                            task_headers = {
                                "Authorization": f"Bearer {key['key']}",
                                "X-ModelScope-Task-Type": "image_generation"
                            }
                            
                            max_retries = 30
                            retry_count = 0
                            task_completed = False

                            while retry_count < max_retries:
                                await asyncio.sleep(2)
                                retry_count += 1
                                
                                async with httpx.AsyncClient() as client:
                                    task_response = await client.get(
                                        f"{config.BASE_URL}/tasks/{task_id}",
                                        headers=task_headers,
                                        timeout=timeout
                                    )
                                
                                task_result = task_response.json()
                                task_status = task_result.get("task_status")
                                
                                logger.info(f"  任务状态: {task_status} (尝试 {retry_count}/{max_retries})")
                                
                                if task_status == "SUCCEED":
                                    task_completed = True
                                    # 从 output_images 中提取图片链接
                                    if "output_images" in task_result and len(task_result["output_images"]) > 0:
                                        image_url = task_result["output_images"][0]
                                        result = self._format_image_response(task_result, image_url, data)
                                        logger.info(f"✅ 成功提取图片链接: {image_url[:50]}...")
                                        break
                                    else:
                                        logger.warning("任务成功但 output_images 为空")
                                        result = task_result
                                        break
                                elif task_status == "FAILED":
                                    task_completed = True
                                    logger.error(f"任务失败: {task_result}")
                                    raise Exception(f"任务失败: {task_result.get('error_message', '未知错误')}")
                                elif task_status not in ["PENDING", "PROCESSING"]:
                                    task_completed = True
                                    logger.warning(f"未知任务状态: {task_status}")
                                    result = task_result
                                    break

                            if not task_completed:
                                logger.warning(f"任务轮询超时，返回最后一次任务结果: {task_result}")
                                result = task_result
                        else:
                            # 同步模式，直接尝试提取图片 URL
                            image_url = self._extract_image_url(result)
                            if image_url:
                                result = self._format_image_response(result, image_url, data)
                                logger.info(f"✅ 成功提取图片链接: {image_url[:50]}...")
                    
                    logger.info(f"✅ 成功！模型: {model['name']}, Key: {key['name']}")
                    return result, response.status_code, dict(response.headers)
                    
                except Exception as e:
                    logger.error(f"  Key {key['name']} 调用异常: {e}")
                    exhausted_keys_for_this_model.add(key["id"])
                    continue
            
            logger.warning(f"⚠️  模型 {model['name']} 所有 Key 都失败，换下一个模型")
        
        raise Exception("所有模型和 Key 都调用失败，请检查配置")

api_client = APIClient()
