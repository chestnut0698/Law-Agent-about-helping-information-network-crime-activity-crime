import json
from typing import Any, Optional
from app.files import get_material_service, MaterialError


# ---------- Agent 工具（只返回脱敏内容） ----------


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def list_case_materials(case_id: str, user_id: Optional[str] = None) -> str:
    """列出某案件下材料及处理状态、质量摘要。"""
    try:
        materials = get_material_service().list_materials(case_id, user_id=user_id or "system")
        return _dump(
            {
                "materials": [
                    {
                        "document_id": material["id"],
                        "filename": material["filename"],
                        "status": material["status"],
                        "quality_summary": material.get("quality_summary"),
                        "current_version_id": material.get("current_version_id"),
                        "version_count": material.get("version_count"),
                    }
                    for material in materials
                ]
            }
        )
    except MaterialError as exc:
        return _dump(exc.to_dict())


def get_material_status(document_id: str, user_id: Optional[str] = None) -> str:
    """查询单份材料处理状态、版本链与低质量页。"""
    try:
        return _dump(get_material_service().get_status(document_id, user_id=user_id or "system"))
    except MaterialError as exc:
        return _dump(exc.to_dict())


def locate_low_quality_pages(document_id: str, user_id: Optional[str] = None) -> str:
    """定位识别质量不佳、需要人工修正的页面。"""
    try:
        status = get_material_service().get_status(document_id, user_id=user_id or "system")
        return _dump(
            {
                "document_id": document_id,
                "low_quality_pages": status.get("low_quality_pages", []),
                "quality_summary": status.get("quality_summary"),
            }
        )
    except MaterialError as exc:
        return _dump(exc.to_dict())


def read_material_chunk(
    document_version_id: str,
    chunk_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """经外发门控读取已脱敏的材料片段。"""
    try:
        return _dump(
            get_material_service().read_redacted_chunk(
                document_version_id, chunk_id=chunk_id, user_id=user_id or "system"
            )
        )
    except MaterialError as exc:
        return _dump(exc.to_dict())


def submit_ocr_correction(
    document_id: str,
    source_version_id: str,
    page_no: int,
    corrected_text: str,
    user_id: Optional[str] = None,
) -> str:
    """提交 OCR 人工修正，生成新版本且不覆盖历史。"""
    try:
        return _dump(
            get_material_service().apply_correction(
                document_id=document_id,
                source_version_id=source_version_id,
                page_no=int(page_no),
                corrected_text=corrected_text,
                user_id=user_id or "system",
            )
        )
    except MaterialError as exc:
        return _dump(exc.to_dict())
