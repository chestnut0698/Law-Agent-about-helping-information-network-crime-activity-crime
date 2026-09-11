from pathlib import Path

import pytest

from app.files import ALLOWED_MATERIAL_EXTENSIONS, ERROR_CODES, MaterialError, parse_file_to_pages


def test_xml_extracts_repeating_rows(tmp_path: Path):
    xml_path = tmp_path / "flow.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<records>
  <row><account>622200001</account><name>张某</name></row>
  <row><account>622200002</account><name>李某</name></row>
</records>
""",
        encoding="utf-8",
    )
    pages = parse_file_to_pages(xml_path)
    text = pages[0]["text"]
    assert ".xml" in ALLOWED_MATERIAL_EXTENSIONS
    assert "622200001" in text
    assert "张某" in text
    assert "622200002" in text


def test_xlsx_extracts_sheet_text(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    xlsx_path = tmp_path / "flow.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "流水"
    sheet.append(["卡号", "姓名"])
    sheet.append(["622200001", "张某"])
    workbook.save(xlsx_path)
    pages = parse_file_to_pages(xlsx_path)
    text = pages[0]["text"]
    assert ".xlsx" in ALLOWED_MATERIAL_EXTENSIONS
    assert "622200001" in text
    assert "张某" in text


def test_unknown_extension_still_rejected(tmp_path: Path):
    path = tmp_path / "x.exe"
    path.write_bytes(b"MZ")
    with pytest.raises(MaterialError) as ei:
        parse_file_to_pages(path)
    assert ei.value.code == ERROR_CODES["UNSUPPORTED_TYPE"]
