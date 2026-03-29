import requests
import json

# 测试图生图功能（使用远程服务器）
url = "http://8.163.52.51:2166/v1/chat/completions"

headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer multi-proxy-2025-2000q"
}

data = {
    "model": "img2img",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "优化这张图片，让它更清晰"},
                {"type": "image_url", "image_url": {"url": "https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg"}}
            ]
        }
    ]
}

print("正在发送图生图请求...")
print(f"远程服务器: {url}")
print()

try:
    response = requests.post(url, headers=headers, json=data, timeout=120)
    print(f"状态码: {response.status_code}")
    print()
    
    if response.status_code == 200:
        result = response.json()
        print("响应内容:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print()
        
        # 检查是否有图片链接
        if "image_url" in result:
            print(f"✅ 成功获取图片链接: {result['image_url']}")
        elif "images" in result and result["images"] is not None and len(result["images"]) > 0:
            print(f"✅ 成功获取图片链接: {result['images'][0]}")
        elif "choices" in result and result["choices"] is not None and len(result["choices"]) > 0:
            content = result["choices"][0]["message"].get("content", "")
            if content and content.startswith("http"):
                print(f"✅ 成功获取图片链接: {content}")
            else:
                print("⚠️  返回内容不是图片链接")
        else:
            print("❌ 未找到图片链接")
    else:
        print(f"请求失败: {response.text}")
        
except Exception as e:
    print(f"发生错误: {e}")
