import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------- 测试样例文件构造 ----------


def make_text_pdf(path: Path, pages: list[str]) -> Path:
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    doc.save(path)
    doc.close()
    return path


def make_blank_pdf(path: Path, page_count: int = 1) -> Path:
    """低文本密度 PDF，用于触发 OCR 分支。"""
    import fitz

    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.save(path)
    doc.close()
    return path


def make_mixed_pdf(path: Path) -> Path:
    import fitz

    doc = fitz.open()
    first = doc.new_page()
    first.insert_text((72, 72), "数字页：嫌疑人张三转账记录", fontsize=12)
    doc.new_page()  # blank -> OCR
    doc.save(path)
    doc.close()
    return path


def make_image_with_ocr_sidecar(path: Path, text: str) -> Path:
    """1x1 PNG 加 OCR 旁路标记，供 FallbackOCREngine 读取。"""
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png + b"__OCR_TEXT__:" + text.encode("utf-8"))
    return path


def make_docx(path: Path, text: str) -> Path:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)
    return path


def make_txt(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def make_corrupt_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4\nnot a real pdf content")
    return path
