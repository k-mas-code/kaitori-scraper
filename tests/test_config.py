import pytest

from arbitrage.config import ConfigError, load_campaigns


def load(tmp_path, text):
    path = tmp_path / "campaigns.yaml"
    path.write_text(text, encoding="utf-8")
    return load_campaigns(path)


def test_example_file_is_valid():
    from arbitrage.config import ROOT
    cfg = load_campaigns(ROOT / "config" / "campaigns.example.yaml")
    assert cfg.common and cfg.campaigns


def test_cap_is_min_of_order_and_period(tmp_path):
    cfg = load(tmp_path, "common:\n  - {name: a, rate: 0.05, cap_per_order: 3000, cap_per_period: 1000}\n")
    assert cfg.common[0].cap == 1000


@pytest.mark.parametrize("text", [
    "common:\n  - {name: a, rate: 0.05, cap: 3000}\n",                 # キー名の間違いを黙って通さない
    "campaigns:\n  - {name: a, rate: 0.05, min_purchace: 5000}\n",
    "bsplus: {cap: 100}\n",
    "common:\n  - {name: a, rate: 5}\n",                               # 5% を 5 と書いた
    "campaigns:\n  - {name: a, rate: 0.05, target: page}\n",           # page_title が無い
    "campaigns:\n  - {name: a, rate: 0.05, target: everyone}\n",
    "campaigns:\n  - {name: a, rate: 0.05, entry_required: 'no'}\n",
    "common:\n  - {name: a, rate: 0.01}\ncampaigns:\n  - {name: a, rate: 0.05}\n",   # name の重複
    "extra: 1\n",
])
def test_invalid_config_is_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load(tmp_path, text)
