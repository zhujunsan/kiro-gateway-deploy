# app/tests/test_usage.py
from kiro_gateway_tray import usage


def test_format_summary_with_overage():
    data = {
        "subscription": "Kiro Pro",
        "breakdowns": [
            {"used": 1100, "limit": 1000, "overage": 100, "overageCostUsd": 4.0},
        ],
        "overageRateUsd": 0.04,
        "overageCreditsTotal": 100,
        "overageCostUsd": 4.0,
    }
    out = usage.format_summary(data)
    assert "Kiro Pro" in out
    assert "1100 / 1000" in out
    assert "超额 100" in out
    assert "$4.0" in out
    assert "预计超额费用: $4.0" in out


def test_format_summary_no_overage():
    data = {
        "subscription": "Kiro Pro",
        "breakdowns": [{"used": 500, "limit": 1000, "overage": 0, "overageCostUsd": 0.0}],
        "overageCostUsd": 0.0,
    }
    out = usage.format_summary(data)
    assert "500 / 1000" in out
    assert "超额" not in out
    assert "预计超额费用" not in out


def test_format_menu_line_with_overage():
    data = {
        "breakdowns": [{"used": 1100, "limit": 1000}],
        "overageCostUsd": 4.0,
    }
    assert usage.format_menu_line(data) == "1100 / 1000 ($4.0)"


def test_format_menu_line_no_overage():
    data = {"breakdowns": [{"used": 500, "limit": 1000}], "overageCostUsd": 0.0}
    assert usage.format_menu_line(data) == "500 / 1000"


def test_format_menu_line_empty():
    assert usage.format_menu_line({"breakdowns": []}) == "无数据"


def test_split_models_for_menu_pairs_aliases_and_keeps_native_models():
    ids = sorted([
        "auto",
        "claude-haiku-4.5",
        "claude-opus-4.6",
        "claude-sonnet-4.6",
        "deepseek-3.2",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "kiro-deepseek-3.2",
        "kiro-h-4.5",
        "kiro-o-4.6",
        "kiro-s-4.6",
    ])
    aliases = {
        "kiro-h-4.5": "claude-haiku-4.5",
        "kiro-o-4.6": "claude-opus-4.6",
        "kiro-s-4.6": "claude-sonnet-4.6",
        "kiro-deepseek-3.2": "deepseek-3.2",
    }
    canonical, alias_list = usage.split_models_for_menu(ids, aliases=aliases)
    assert alias_list == [
        "kiro-h-4.5",
        "kiro-o-4.6",
        "kiro-s-4.6",
        "kiro-deepseek-3.2",
    ]
    assert canonical == [
        "auto",
        "claude-haiku-4.5",
        "claude-opus-4.6",
        "claude-sonnet-4.6",
        "deepseek-3.2",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]


def test_split_models_for_menu_unpaired_real_appended():
    ids = ["claude-haiku-4.5", "mystery-model", "kiro-h-4.5"]
    aliases = {"kiro-h-4.5": "claude-haiku-4.5"}
    canonical, alias_list = usage.split_models_for_menu(ids, aliases=aliases)
    assert canonical == ["claude-haiku-4.5", "mystery-model"]
    assert alias_list == ["kiro-h-4.5"]
