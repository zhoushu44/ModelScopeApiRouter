FROM python:3.10-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY refactored_router ./refactored_router

# 暴露端口
EXPOSE 2166

# 启动命令
CMD ["python", "-m", "refactored_router.main"]
