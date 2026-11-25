"""Tests for 0.1 USDT integer arithmetic and command parser."""

from jzbot.money import format_receipt, format_tenths, format_usdt, parse_business_command


def test_parse_valid_commands():
    assert parse_business_command("+10").kind == "deposit"
    assert parse_business_command("+10").amount_tenths == 100
    assert parse_business_command("  -0.5  ").kind == "withdraw"
    assert parse_business_command("  -0.5  ").amount_tenths == 5
    assert parse_business_command("+123.4").amount_tenths == 1234
    assert parse_business_command("/").kind == "query"
    assert parse_business_command("//").kind == "clear"


def test_parse_invalid_or_ignored_commands():
    for bad in [
        None,
        "",
        "   ",
        "+0",
        "-0",
        "+0.0",
        "-0.0",
        "+1.23",
        "-10.55",
        "10",
        "+",
        "-",
        "///",
        "hello",
        "+1000000000000000",  # 16 digits -> overflow
        "+10 USDT",
    ]:
        assert parse_business_command(bad) is None
    assert parse_business_command("+199898999999").amount_tenths == 1998989999990


def test_formatting_and_receipts():
    assert format_tenths(0) == "0"
    assert format_tenths(150) == "15"
    assert format_tenths(-386) == "-38.6"
    assert format_usdt(5) == "0.5 USDT"
    assert format_receipt("deposit", 150, 150) == "入金 15 USDT, 余额 15 USDT"
    assert format_receipt("withdraw", 52, -386) == "出金 5.2 USDT, 余额 -38.6 USDT"
    assert format_receipt("clear", 0, 0) == "余额已清零"
