from tools.files import redact_text


SAMPLE = """
姓名：王小明
身份证号：110101199001011234
手机号：13812345678
银行卡：6222021234567890123
住址：北京市朝阳区建国路1号
账号：user_alpha_01
"""


def test_six_sensitive_types_detected():
    redacted, hits = redact_text(SAMPLE)
    types = {h.sens_type for h in hits}
    assert {"name", "id_card", "phone", "bank_card", "address", "account"} <= types
    assert "110101199001011234" not in redacted
    assert "13812345678" not in redacted
    assert "6222021234567890123" not in redacted
    assert "[NAME_" in redacted
    assert "[ID_CARD_" in redacted
    assert "[PHONE_" in redacted
