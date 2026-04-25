<div align="center">

# ModelScope Router

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.95%2B-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Docker](https://img.shields.io/badge/Docker-supported-blue)

**用于 ModelScope 服务的企业级负载均衡与高可用路由网关**
</div>

---

## ✨ 功能特性

- 🔄 **智能轮询**：多 API Key 自动轮询，提升可用性
- ⚖️ **负载均衡**：支持多模型、多 Key 的负载分发
- 🛡️ **高可用容错**：请求失败自动重试、自动切换 Key / 模型
- 📊 **额度追踪**：自动记录并展示各 Key 的 quota 信息
- 🌐 **OpenAI 兼容接口**：兼容 `/v1/chat/completions`
- 🖼️ **多类型支持**：支持 chat、vision、text2img、img2img
- 🚀 **Docker / GitHub Actions 支持**：便于部署与自动发布

---

## 📋 环境要求

- Python 3.8+
- Docker（可选）

---

## 🚀 快速启动 (Quick Start)

### 方式一：使用 Docker（推荐）

```bash
# 使用 Docker Compose 启动
docker-compose up -d

# 或直接构建和运行
docker build -t modelscope-router .
docker run -d -p 2166:2166 -v ./router_data:/app/refactored_router/router_data --name modelscope-router modelscope-router
```

### 方式二：本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python refactored_router/main.py
```

启动后访问：
- Web UI: `http://localhost:2166`
- API: `http://localhost:2166/v1/chat/completions`

---

## ⚙️ 配置说明

### 1. 配置 API Keys

首次运行后，在 `refactored_router/router_data/api_keys.json` 中添加你的 ModelScope API Key：

```json
[
  {
    "id": "your-key-id",
    "key": "ms-xxxxxxxxxxxxxxxx",
    "name": "主 Key"
  }
]
```

### 2. 配置模型

编辑 `refactored_router/config.json`，可按分类维护模型列表：

```json
[
  {
    "id": "1",
    "name": "deepseek-v3-2",
    "model_id": "deepseek-ai/DeepSeek-V3.2",
    "category": "chat",
    "order": 0
  }
]
```

支持分类：
- `chat`
- `vision`
- `text2img`
- `img2img`

---

## 🔌 API 使用示例

### chat

```bash
curl http://localhost:2166/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer multi-proxy-2025-2000q" \
  -d '{
    "model": "chat",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### vision

```bash
curl http://localhost:2166/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer multi-proxy-2025-2000q" \
  -d '{
    "model": "vision",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "这张图片里有什么？"},
          {"type": "image_url", "image_url": {"url": "https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg"}}
        ]
      }
    ]
  }'
```

### text2img

```bash
curl http://localhost:2166/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer multi-proxy-2025-2000q" \
  -d '{
    "model": "txt2img",
    "messages": [{"role": "user", "content": "一只可爱的猫咪，高清，柔和光线"}]
  }'
```

### img2img

```bash
curl http://localhost:2166/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer multi-proxy-2025-2000q" \
  -d '{
    "model": "img2img",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "优化这张图片，让它更清晰，颜色更自然"},
          {"type": "image_url", "image_url": {"url": "https://qcloud.dpfile.com/pc/d6A1POwDkj8vKTNgbAZswnAaIM2fuXnejIO0X7lJQb9NIYslSlGEPeQVyA4hZRCP.jpg"}}
        ]
      }
    ]
  }'
```

---

## 🧠 路由与稳定性策略

当前路由层已统一支持以下稳定性策略：

- 上游并发队列化
- Key 级健康分调度
- Key 级熔断与冷却
- 模型级健康分调度
- 模型级熔断与冷却
- 空壳响应判失败并自动重试

这套策略已统一应用于：
- `chat`
- `vision`
- `text2img`
- `img2img`

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
  -p 2166:2166 \
  -v ./router_data:/app/refactored_router/router_data \
  --name modelscope-router \
  modelscope-router
```

### 端口说明
- 服务监听：**0.0.0.0:2166**
- 可从外部网络访问

---

## 🚀 GitHub Actions 自动构建

本项目已配置 GitHub Actions，支持自动构建并推送 Docker 镜像。

### 需要配置的 Secrets：

在 GitHub 仓库 → Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 |
|-------------|------|
| `DOCKER_HUB_USERNAME` | 你的 Docker Hub 用户名 |
| `DOCKER_HUB_TOKEN` | 你的 Docker Hub 密码或 Personal Access Token |

### 自动推送标签

GitHub Actions 在远端构建镜像时，会自动为同一个镜像推送以下固定标签：

- `2.0`
- `latest`

对应镜像示例：

```text
<DOCKER_HUB_USERNAME>/modelscope-router:2.0
<DOCKER_HUB_USERNAME>/modelscope-router:latest
```

说明：
- 推送动作由 GitHub Actions 自动完成
- 本地无需执行 `docker push`
- 工作流文件位于 [.github/workflows/docker-build-publish.yml](.github/workflows/docker-build-publish.yml)

### 触发方式：
- 推送到 `main` 分支
- 推送到 `master` 分支
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
