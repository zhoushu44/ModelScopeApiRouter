# ModelScope 智能模型路由器 (ModelScope Smart Router)

<div align="center">

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.95%2B-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Docker](https://img.shields.io/badge/Docker-supported-blue)

**用于 ModelScope 服务的企业级负载均衡与高可用路由网关**
</div>

---

## 📖 项目简介

ModelScope Smart Router 是一个基于 FastAPI 构建的高性能 AI 模型网关。它就像一个智能交通指挥官，旨在解决**单点故障**和**API调用限流**问题。通过智能路由算法，它能自动管理多个 ModelScope 模型实例，实现负载均衡、故障转移（Failover）和精细化的限流控制，确保您的 AI 应用始终保持高可用性。

无论您是个人开发者还是企业用户，都可以通过本系统统一管理 API 访问，提升服务的稳定性和成功率。完全兼容 OpenAI API 格式，可直接接入现有的 AI 工具链（如 Cursor, NextChat, LangChain 等）。

## ✨ 核心功能

- **🤖 智能路由策略**: 自动识别请求模型，在多个同类模型后端中选择最佳候选者。
- **⚖️ 负载均衡**: 基于调用次数和权重的负载均衡，防止单一账号或模型过载。
- **🛡️ 自动故障转移**: 当某个模型调用失败或超时，自动无缝切换到备用模型，用户无感知。
- **🚦 智能限流熔断**: 实时监测 API 调用限制，自动跳过已耗尽配额的模型，并在配额重置后自动恢复。
- **🌐 Web UI 界面**: 可视化管理 API Keys 和模型配置。
- **🔑 多 API Key 管理**: 支持添加、删除、排序多个 API Key。
- **📂 模型分类**: 支持 4 种类型（对话、视觉理解、文生图、图生图）。
- **📋 简化调用**: 只需传类型（chat/txt2img/img2img/vision），内部自动处理。
- **🔌 OpenAI 兼容**: 提供与 OpenAI `v1/chat/completions` 完全兼容的接口，零成本迁移。
- **🌊 流式响应支持**: 完美支持 Server-Sent Events (SSE) 流式输出，打字机效果流畅。

---

## 🚀 快速启动 (Quick Start)

### 方式一：使用 Docker（推荐）

```bash
# 使用 Docker Compose 启动
docker-compose up -d

# 或直接构建和运行
docker build -t modelscope-router .
docker run -d -p 8080:8080 -v ./router_data:/app/refactored_router/router_data --name modelscope-router modelscope-router
```

### 方式二：本地运行

仅需一行 Python 命令即可启动服务。

#### 1. 安装依赖

```bash
pip install -r requirements.txt
```

#### 2. 启动服务

在项目**根目录**下运行：
```bash
python -m refactored_router.main
```

服务将在 `http://localhost:8080` 启动。数据将持久化保存在 `./router_data` 目录。

---

## 🌐 Web UI 使用

打开浏览器访问：**http://localhost:8080**

### 功能：
- **API Key 管理**: 添加、删除 API Key
- **模型管理**: 添加、删除、排序模型，按分类筛选
- **调用说明**: 查看各种调用示例（cURL、Python、OpenAI）
- **额度查询**: 查看各 API Key 的额度信息

---

## 💻 使用指南 (Usage)

本服务提供与 OpenAI 兼容的 API，这意味着您可以直接使用任何支持 OpenAI 的客户端库或软件。

### 简化调用方式（推荐）

只需传类型标识符，内部会自动选择该分类优先级最高的模型：

| 类型 | 说明 |
|------|------|
| `chat` | 对话模型 |
| `vision` | 视觉理解模型 |
| `txt2img` | 文生图模型 |
| `img2img` | 图生图模型 |

### 接入第三方客户端 (Cursor, NextChat 等)

- **Base URL (API域名)**: `http://localhost:8080/v1` (注意部分软件不需要 `/v1`)
- **API Key**: 任意填写
- **Model Name**: `chat` / `vision` / `txt2img` / `img2img` (推荐)

### 命令行调用 (cURL)

**对话：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chat",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

**文生图：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "txt2img",
    "messages": [{"role": "user", "content": "一只可爱的猫咪"}]
  }'
```

**视觉理解（单图）：**
```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "vision",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "这张图片里有什么？"},
          {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
        ]
      }
    ]
  }'
```

### Python 客户端示例

```python
from openai import OpenAI

client = OpenAI(
    api_key="dummy",
    base_url="http://localhost:8080/v1"
)

# 对话
response = client.chat.completions.create(
    model="chat",
    messages=[{"role": "user", "content": "你好"}],
    stream=False
)
print(response.choices[0].message.content)

# 文生图
response = client.chat.completions.create(
    model="txt2img",
    messages=[{"role": "user", "content": "一只可爱的猫咪"}]
)
print(response)

# 视觉理解
response = client.chat.completions.create(
    model="vision",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图片里有什么？"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
        ]
    }]
)
print(response.choices[0].message.content)
```

---

## ⚙️ 配置详解 (Configuration)

### 1. 模型路由配置 (config.json)

位于 `refactored_router/config.json`，定义了路由池中的模型列表。

```json
[
  {
    "id": "1",
    "name": "deepseek-v3-2",
    "model_id": "deepseek-ai/DeepSeek-V3.2",
    "category": "chat",
    "order": 0
  },
  {
    "id": "2",
    "name": "qwen-image",
    "model_id": "Qwen/Qwen-Image",
    "category": "text2img",
    "order": 0
  }
]
```

**字段说明：**
- `category`: 模型分类（chat/vision/text2img/img2img）
- `order`: 排序（数字越小优先级越高）

---

## 📦 Docker 部署

### 使用 Docker Compose

```bash
docker-compose up -d
```

### 手动构建和运行

```bash
# 构建镜像
docker build -t modelscope-router .

# 运行容器
docker run -d \
  -p 8080:8080 \
  -v ./router_data:/app/refactored_router/router_data \
  --name modelscope-router \
  modelscope-router
```

### 端口说明
- 服务监听：**0.0.0.0:8080**
- 可从外部网络访问

---

## 🚀 GitHub Actions 自动构建

本项目已配置 GitHub Actions，支持自动构建和发布 Docker 镜像。

### 需要配置的 Secrets：

在 GitHub 仓库 → Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 |
|-------------|------|
| `DOCKER_HUB_USERNAME` | 你的 Docker Hub 用户名 |
| `DOCKER_HUB_TOKEN` | 你的 Docker Hub 密码或 Personal Access Token |

### 触发方式：
- 推送到 `main` 分支
- 创建 Release

---

## 📁 目录结构

```
.
├── .github/
│   └── workflows/
│       └── docker-build-publish.yml  # GitHub Actions 工作流
├── refactored_router/
│   ├── router_data/             # 数据持久化目录
│   │   ├── .gitkeep
│   │   ├── api_keys.json        # API Keys 配置
│   │   └── quota_info.json      # 额度信息
│   ├── static/
│   │   └── index.html           # Web UI
│   ├── config.json              # 模型配置
│   ├── main.py                  # 程序入口
│   ├── network.py               # 网络请求与重试逻辑
│   ├── schema.py                # 数据模型
│   ├── settings.py              # 配置加载
│   ├── stats.py                 # 统计服务
│   └── ui.py                    # 终端 UI
├── .dockerignore
├── .gitignore
├── Dockerfile
├── README.md
├── docker-compose.yml
├── logo.jpg
└── requirements.txt
```

---

## 🤝 贡献与支持

欢迎提交 Issue 和 Pull Request！
如果您觉得这个项目有帮助，请给一个 ⭐️ Star！

---

## 📄 许可证

MIT License
