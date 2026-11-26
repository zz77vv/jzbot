"""Tests for LedgerFlow domain parser, formatting, and HTML receipts."""

from ledgerflow.domain import build_receipt_html, fmt_money, fmt_num, parse_chat_intent


def test_parse_rich_intents():
    p1 = parse_chat_intent("+100")
    assert p1 and p1.kind == "in" and p1.amount_tenths == 1000 and p1.note == ""

    p2 = parse_chat_intent("+50.5 货款尾款")
    assert p2 and p2.kind == "in" and p2.amount_tenths == 505 and p2.note == "货款尾款"

    p3 = parse_chat_intent("  -12.3   转出测试 ")
    assert p3 and p3.kind == "out" and p3.amount_tenths == 123 and p3.note == "转出测试"

    # Full-width symbols, trailing zero decimal, and no-space Chinese memo
    p4 = parse_chat_intent("＋100货款")
    assert p4 and p4.kind == "in" and p4.amount_tenths == 1000 and p4.note == "货款"

    p5 = parse_chat_intent("－50.50")
    assert p5 and p5.kind == "out" and p5.amount_tenths == 505 and p5.note == ""

    assert parse_chat_intent("／").kind == "query"
    assert parse_chat_intent("／／").kind == "settle"

    assert parse_chat_intent("撤销").kind == "undo"
    assert parse_chat_intent("撤").kind == "undo"
    assert parse_chat_intent("-undo").kind == "undo"
    assert parse_chat_intent("/").kind == "query"
    assert parse_chat_intent("账单").kind == "query"
    assert parse_chat_intent("//").kind == "settle"
    assert parse_chat_intent("清账").kind == "settle"


def test_parse_rejects_invalid():
    for bad in [None, "", "   ", "+0", "-0.0", "+1.23", "+10USDT", "+1000000000000000", "hello", "100"]:
        assert parse_chat_intent(bad) is None


def test_formatting_and_html_receipt():
    assert fmt_num(12345) == "1,234.5"
    assert fmt_money(-500) == "-50 USDT"
    html = build_receipt_html(kind="in", amount_tenths=1000, balance_after=2500, journal_id=42, note="换U")
    assert "<blockquote>" in html
    assert '<tg-emoji emoji-id="5244837092042750681">' in html
    assert "+100 USDT" in html
    assert "换U" in html
    assert "#42" not in html
