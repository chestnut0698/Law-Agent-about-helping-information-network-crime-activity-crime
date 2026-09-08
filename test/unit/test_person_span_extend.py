"""人名跨度补全：残缺两字名右扩（类别样例，无卷宗真人名）。"""

from tools.entities import _extend_person_span


def test_two_char_surname_plus_given_extends_right_one():
    # 合成：姓 + 模型只切出二字，右侧仍是名用字且之后收界
    text = "证人钱博元到案"
    start, end = text.index("钱博"), text.index("钱博") + 2
    ns, ne = _extend_person_span(text, start, end)
    assert text[ns:ne] == "钱博元"


def test_does_not_extend_into_following_content_word():
    # 完整二字名后接实词：不得误扩
    text = "证人侯刚去大陆办事"
    start, end = text.index("侯刚"), text.index("侯刚") + 2
    ns, ne = _extend_person_span(text, start, end)
    assert text[ns:ne] == "侯刚"


def test_extends_when_closed_by_punctuation():
    text = "转给周文强(账号尾号)"
    start, end = text.index("周文"), text.index("周文") + 2
    ns, ne = _extend_person_span(text, start, end)
    assert text[ns:ne] == "周文强"


def test_does_not_swallow_legal_right_token():
    text = "钱博元供述称"
    start, end = text.index("钱博"), text.index("钱博") + 2
    ns, ne = _extend_person_span(text, start, end)
    assert text[ns:ne] == "钱博元"
    ns2, ne2 = _extend_person_span(text, ns, ne)
    assert text[ns2:ne2] == "钱博元"


def test_stops_at_right_stop_char():
    text = "钱博向银行转账"
    start, end = text.index("钱博"), text.index("钱博") + 2
    ns, ne = _extend_person_span(text, start, end)
    assert text[ns:ne] == "钱博"


def test_bare_surname_extends_up_to_three():
    text = "嫌疑人钱博元到案"
    start = text.index("钱")
    end = start + 1
    ns, ne = _extend_person_span(text, start, end)
    assert text[ns:ne] == "钱博元"
