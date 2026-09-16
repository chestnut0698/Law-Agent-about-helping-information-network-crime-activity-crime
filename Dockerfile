FROM python:3.11-slim-bookworm

# 国内构建可传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_VERSION=2.14.0
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git：prikit 只能从源仓库安装；tesseract：prikit 经 pytesseract 调用的系统识字引擎
# libgl1/libglib2.0-0：presidio-image-redactor 依赖的 opencv 需要
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 必须先装 CPU 版 torch。zh_core_web_trf 会间接拉 torch，而 Linux 上
# PyPI 默认给的是 CUDA 构建，体积多出数 GB，本项目推理全在云端用不上。
RUN pip install --no-cache-dir "torch==${TORCH_VERSION}" \
        --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 中文实体模型约 450MB，预装进镜像，评审端无需再下载
RUN python -m spacy download zh_core_web_trf

COPY . .

# 容器内必须监听 0.0.0.0，否则宿主机无法访问
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"

CMD ["python", "-m", "app.main"]
