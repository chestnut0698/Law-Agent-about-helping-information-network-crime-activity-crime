# 链证智析

面向基层检察院的帮信罪关联案件证据链条挖掘与漏犯漏罪智能监督系统。

当前仓库可本地跑通：FastAPI 后端 + `ui/` 原生前端工作台，以及案件材料上传 / 解析 / 脱敏管线。设计细节见 `docs/`。

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows / macOS / Linux |
| Python | **3.11+**（技术规划约定；低于 3.11 未验证） |
| 包管理 | 建议使用虚拟环境（`venv`） |
| 浏览器 | 现代 Chromium / Firefox / Edge 即可 |
| 模型 API | 配置 **DeepSeek** |

可选：安装 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 作为离线识字兜底。默认用 DeepSeek 多模态识别 PNG/JPG 以及扫描件 PDF 中的文字，**不必先装 Paddle**。关闭 `DEEPSEEK_EXTERNAL_CALLS_ENABLED` 或未配置密钥时，才会尝试 Paddle，再退回内置 Fallback。

---

## 依赖库

依赖锁定在仓库根目录 `requirements.txt`，主要包括：

- **Web**：`fastapi`、`uvicorn`、`python-multipart`
- **模型**：`openai`（兼容 DeepSeek OpenAI 风格接口）
- **配置**：`python-dotenv`
- **卷宗解析**：`pymupdf`、`Pillow`、`python-docx`
- **公网检索**：`baidusearch`、`requests`
- **测试**：`pytest`

OCR 重依赖默认注释掉，仅在需要离线兜底时取消注释安装：

```text
# paddleocr>=2.7.0
# paddlepaddle>=2.5.0
```

---

## 快速启动

在仓库根目录执行：

```bash
# 1. 拉取代码
git clone https://github.com/chestnut0698/Law-Agent-about-helping-information-network-crime-activity-crime.git
cd Law-Agent-about-helping-information-network-crime-activity-crime

# 3. 安装依赖,
pip install -r requirements.txt
python -m spacy download zh_core_web_trf

# 4. 配置环境变量
# 复制 .env.example 为 .env，填入自己的密钥（不要提交 .env）
cp .env.example .env          # Windows 可用 copy .env.example .env
```

编辑 `.env` 时至少注意这几项：

```env
# 推荐：默认模型链路 DeepSeek
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

启动服务（仍在仓库根目录、虚拟环境已激活）：

```bash
python -m app.main
# 或
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开：<http://127.0.0.1:8000>

前端由 FastAPI 挂载 `ui/` 静态目录，无需另起前端工程。

---

## 环境变量说明（常用）

完整模板见 `.env.example`。最常改的是：

| 变量 | 含义 | 本地建议 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥 | 有则优先使用 |
| `DEEPSEEK_MODEL` | 模型名 | 默认 `deepseek-v4-flash` |

密钥、真实 `.env` **不要提交**。

---

## 目录速览

```text
app/          # FastAPI 入口与配置（main.py / config.py）
agents/       # 智能体
tools/        # 业务工具（files.py 卷宗管线，tasks.py 监督任务）
ui/           # 前端工作台
data/         # 本地数据（对话、workspace、SQLite、材料存储）
knowledge/    # 法规/政策示例知识库
docs/         # 设计方案、技术规划、已有成果说明
test/         # 测试（请勿随意改；功能验证走智能体全流程）
```

更细的实现说明：`docs/当前项目已有实际成果及其实现细节说明.md`。

---

## 常见问题

1. **材料上传全部失败**  
   检查 `.env` 是否为 `MATERIAL_AUTH_MODE=allow_all`，改完后重启进程。

2. **智能体不调模型 / 报无 API Key**  
   确认至少配置了 `DEEPSEEK_API_KEY`，且服务从仓库根目录启动（才会读到根目录 `.env`）。

3. **依赖冲突 / 装不上 Paddle**  
   先不装 OCR 重依赖，用 Fallback 即可开发；Paddle 按本机 CUDA/CPU 说明单独装。

4. **端口占用**  
   默认 `127.0.0.1:8000`，可改启动命令中的 `--port`，或结束占用该端口的进程。

5. **与远程分叉推不上去**  
   先 `git pull --rebase origin main`，解决冲突后再 `git push`（勿对 `main` 强推）。
