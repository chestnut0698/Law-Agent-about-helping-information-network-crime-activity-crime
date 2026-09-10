"""报告导出：把服务端存储的 markdown 报告简单转成 docx 供下载，不落盘、不改存储。

存储与版本管理始终只针对 markdown；docx 仅在下载请求时即时生成。
排版从简：标题分级、项目符号/有序列表、去粗体与链接标记。
"""

from __future__ import annotations

import io
import re

from docx import Document

_INLINE_MARK_RE = re.compile(r"(\*\*|__|\*|_|`)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_ORDERED_RE = re.compile(r"^\d+[.)]\s+")


def _clean(text: str) -> str:
    text = _LINK_RE.sub(r"\1", text)
    return _INLINE_MARK_RE.sub("", text).strip()


def markdown_to_docx(markdown: str, title: str = "跨案关联线索核验单") -> bytes:
    doc = Document()
    try:
        doc.core_properties.title = title
    except Exception:
        pass

    for raw in (markdown or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("```"):
            continue
        if len(stripped) >= 3 and set(stripped) <= set("-—*_"):
            continue  # 分隔线
        if stripped.startswith("### "):
            doc.add_heading(_clean(stripped[4:]), level=3)
        elif stripped.startswith("## "):
            doc.add_heading(_clean(stripped[3:]), level=2)
        elif stripped.startswith("# "):
            doc.add_heading(_clean(stripped[2:]), level=1)
        elif stripped.startswith(("- ", "* ", "• ")):
            doc.add_paragraph(_clean(stripped[2:]), style="List Bullet")
        elif _ORDERED_RE.match(stripped):
            doc.add_paragraph(_clean(_ORDERED_RE.sub("", stripped)), style="List Number")
        elif stripped.startswith(">"):
            doc.add_paragraph(_clean(stripped.lstrip(">").strip()))
        else:
            doc.add_paragraph(_clean(stripped))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
