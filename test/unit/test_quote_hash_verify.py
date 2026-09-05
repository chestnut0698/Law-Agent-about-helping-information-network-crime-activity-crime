"""引用 quote_hash 服务端校验。"""

from tools.entities import quote_hash, verify_quote_hash


def test_verify_quote_hash_accepts_matching():
    chunk = "前缀 账户6222********6231 后缀"
    quote = "账户6222********6231"
    h = quote_hash(quote)
    assert verify_quote_hash(chunk, quote, h) is True


def test_verify_quote_hash_rejects_wrong_hash():
    chunk = "账户6222********6231"
    quote = "账户6222********6231"
    assert verify_quote_hash(chunk, quote, "0" * 64) is False


def test_verify_quote_hash_rejects_quote_not_in_chunk():
    chunk = "无关文本"
    quote = "账户6222********6231"
    h = quote_hash(quote)
    assert verify_quote_hash(chunk, quote, h) is False


def test_verify_quote_hash_rejects_empty():
    assert verify_quote_hash("abc", "", "a" * 64) is False
    assert verify_quote_hash("", "abc", quote_hash("abc")) is False
