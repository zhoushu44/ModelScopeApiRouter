<div align="center">

# ModelScope Router

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.95%2B-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Docker](https://img.shields.io/badge/Docker-supported-blue)
![Version](https://img.shields.io/badge/version-2.3-blue)

**用于 ModelScope 服务的企业级负载均衡与高可用路由网关**
</div>

---

## ✨ 功能特性

- 🔄 **智能轮询**：多 API Key 自动轮询，提升可用性
- ⚖️ **负载均衡**：支持多模型、多 Key 的负载分发
- 🛡️ **高可用容错**：请求失败自动重试、自动切换 Key / 模型
- 📊 **额度追踪**：自动记录并展示各 Key 的 quota 信息，Web UI 一键检测日配额+模型配额
- 🧪 **模型/Key 可用性检测**：Web UI 支持单模型测试 + 批量并发测试 + Key 额度一键检测
- 🌐 **OpenAI 兼容接口**：兼容 `/v1/chat/completions` 和 `/v1/models`
- 🖼️ **多类型支持**：支持 chat、vision、text2img、img2img
- ⏱️ **自适应轮询**：图片生成类请求按类别动态调整轮询时长
- 🔌 **客户端即插即用**：兼容 Trae、ChatBox、OpenAI SDK 等主流客户端
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

# 或者直接双击bat文件一键启动：
# 双击 -> 一键启动器(python启动版).bat
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

### 模型列表

```bash
curl http://localhost:2166/v1/models \
  -H "Authorization: Bearer multi-proxy-2025-2000q"
```

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
          {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
        ]
      }
    ]
  }'
```

### txt2img

```bash
curl http://localhost:2166/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer multi-proxy-2025-2000q" \
  -d '{
    "model": "txt2img",
    "messages": [{"role": "user", "content": "一只可爱的猫咪，高清，柔和光线"}]
  }'
```

> 生成约需 50-60 秒，客户端请设置 120s+ 超时

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
          {"type": "text", "text": "优化这张图片，让它更清晰"},
          {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
        ]
      }
    ]
  }'
```

> 生成约需 3-5 分钟，客户端请设置 300s+ 超时

---

## 🔌 客户端配置（Trae / ChatBox / OpenAI SDK）

兼容 OpenAI 接口的客户端可直接使用：

| 字段 | 值 |
|------|-----|
| 服务商 | OpenAI |
| 请求地址 | `http://your-server:2166/v1` |
| API 密钥 | `multi-proxy-2025-2000q`（任意值） |
| 模型 | `chat` / `txt2img` / `img2img` / `vision` |

> 注意：请求地址只填到 `/v1`，不要加 `/chat/completions`。客户端会自动拼接。

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

- `2.2`
- `latest`

对应镜像示例：

```text
<DOCKER_HUB_USERNAME>/modelscope-router:2.2
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
├── .dockerignore               # Docker 排除规则（含 .env）
├── .gitignore
├── Dockerfile
├── README.md
├── docker-compose.yml
├── logo.jpg
└── requirements.txt
```

> `.dockerignore` 已排除 `.env`、`router_data/*.json` 等敏感/运行时文件，确保不会打包进镜像。
```

---

## 🤝 贡献与支持

欢迎提交 Issue 和 Pull Request！
如果您觉得这个项目有帮助，请给一个 ⭐️ Star！

---

## 📄 许可证

MIT License

---

## 📋 更新日志

### v2.2 (2026-05-08)

**新增**
- Key 额度一键检测：Web UI 支持并发测试所有 Key 的日配额和模型配额
- 支持指定模型 ID 查看特定模型的限额，避免随机使用默认模型
- 模型测试端点多 Key 轮换 + 重试（每 Key 最多 2 次），解决上游偶发空壳响应(choices=null)导致测试结果不稳定
- 单个 Key 可独立测试额度

**修复**
- 强制去掉 `stream=true` 转为非流式返回，解决 Trae 等客户端卡在"正在分析问题"
- 模型配额显示统一为 已用/总量，日配额和模型配额进度条方向一致
- Key 额度检测路由顺序修复（避免被 DELETE `/api/keys/{key_id}` 拦截）

### v2.1 (2026-05-08)

**新增**
- `/v1/models` 端点：OpenAI 兼容的模型列表接口，动态从配置读取
- 客户端兼容：支持 Trae、ChatBox 等主流 OpenAI 客户端即插即用
- 模型可用性检测：Web UI 支持单个测试 + 5 并发批量测试，显示状态指示灯

**修复**
- 图片生成超时：txt2img/img2img 按类别自适应轮询（txt2img 40次×3s，img2img 60次×4s）
- 图片生成请求超时按类别区分（图片类 180s，文本类 60s）
- 图片生成模型重试上限 3 次，避免多模型叠加放大超时
- 额度追踪响应头映射错误（`modelscope-ratelimit-tpm` → `modelscope-ratelimit-requests-limit` 等）

**轮询参数**
| 类别 | 次数 | 间隔 | 最长等待 |
|------|------|------|------|
| txt2img (文生图) | 40 | 3s | ~160s |
| img2img (图生图) | 60 | 4s | ~300s |

### v2.0

- 重构多 Key / 多模型路由架构
- 健康分调度 + 熔断冷却机制
- 支持 chat / vision / text2img / img2img 四类模型
