import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv()

DATA_DIR = REPO_ROOT / "data" / "conversations"
META_FILE = DATA_DIR / "conversations.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

WORKSPACE_DIR = REPO_ROOT / "data" / "workspace"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", str(REPO_ROOT / "data" / "database" / "law_agent.db"))
)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

MATERIAL_STORAGE_DIR = Path(
    os.getenv("MATERIAL_STORAGE_DIR", str(REPO_ROOT / "data" / "storage" / "materials"))
)
MATERIAL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

REDACTION_STORAGE_DIR = Path(
    os.getenv(
        "REDACTION_STORAGE_DIR",
        str(REPO_ROOT / "data" / "storage" / "redaction_maps"),
    )
)
REDACTION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MATERIAL_AUTH_MODE = "allow_all"
OCR_TEXT_DENSITY_THRESHOLD = float(os.getenv("OCR_TEXT_DENSITY_THRESHOLD", "0.08"))
OCR_LOW_CONFIDENCE_THRESHOLD = float(os.getenv("OCR_LOW_CONFIDENCE_THRESHOLD", "0.75"))
OCR_MAX_PAGE_RETRIES = int(os.getenv("OCR_MAX_PAGE_RETRIES", "2"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))
PARSER_VERSION = os.getenv("PARSER_VERSION", "stage3-v1")
ALLOWED_MATERIAL_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".docx",
    ".txt",
}

PLANS = [
    [
        "1.理解用户需求",
        "2.调用工具",
        "3.分析工具结果",
        "4.生成回答",
    ],
    [
        "1.你需要整理用户的需求，判断该调用什么工具",
        "2.调用所需工具，尽可能多的获取所需信息",
        "3.分析工具结果",
        "4.此轮必须生成回答，并验证，重要信息有出处",
    ],
]

# 默认走 DeepSeek；队友本地若只配置 NVIDIA，则自动切到 NVIDIA，互不影响。
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
NVIDIA_KEY = os.getenv("NVIDIA_API_KEY")

if DEEPSEEK_KEY:
    API_KEY = DEEPSEEK_KEY
    BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
elif NVIDIA_KEY:
    API_KEY = NVIDIA_KEY
    BASE_URL = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    MODEL_NAME = os.getenv("MODEL_NAME", "meta/muse-glimmer-30b")
else:
    API_KEY = None
    BASE_URL = "https://api.deepseek.com"
    MODEL_NAME = "deepseek-v4-flash"

SYSTEM_PROMPT = """你是一个处于测试环境的贴心AI助理，满足用户(开发者)的所有要求。
对于用户的请求，你应该分几步来完成: 1.理解用户需求;2.调用工具;3.分析工具结果;4.生成回答
在每一步任务中，你只能严格输出空字符。只有最后一步你才必须输出完整回答

你可以通过材料工具list_case_materials / get_material_status / read_material_chunk / locate_low_quality_pages / submit_ocr_correction 查询案件卷宗处理状态、低质量页、版本信息，并读取已脱敏片段。
禁止直接读取未脱敏原文或绕过外发门控。
"""
