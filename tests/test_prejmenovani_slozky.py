"""Aktualizovaná krabička nesmí přijít o identitu.

18. 9. 2026 se přejmenováním na BIKODY přejmenovala i složka s nastavením
(`~/.config/event-control-agent` → `~/.config/bikody-agent`). Agent 1.13 si
v nové složce nic nenašel, vyrobil **nový token** a v aplikaci se ohlásil jako
nespárovaný — obsluha ho musela u trati opsat z displeje. Poznalo se to na
krabičce na trati den před závodem.

Stejnou cestou osiřel **přeliv**: rámce, které se nevešly do fronty a čekají
na disku. Leží vedle nastavení právě proto, aby přežily restart krabičky.
"""
from __future__ import annotations

import json

import track_agent as ta


def _slozky(tmp_path, monkeypatch):
    """Obě složky pod dočasným adresářem, ať test nesahá na skutečný domov."""
    stara = tmp_path / "event-control-agent"
    nova = tmp_path / "bikody-agent"
    monkeypatch.setattr(ta, "config_path", lambda: nova / "config.json")
    monkeypatch.setattr(ta, "stara_config_path", lambda: stara / "config.json")
    monkeypatch.setattr(ta, "_stara_slozka_prenesena", False)
    return stara, nova


def _uloz(slozka, **data):
    slozka.mkdir(parents=True, exist_ok=True)
    (slozka / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_token_prezije_prejmenovani_slozky(tmp_path, monkeypatch):
    stara, nova = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="PUVODNI-TOKEN", autostart=True)

    config = ta.load_config()

    assert config.get("token") == "PUVODNI-TOKEN", (
        "agent si vyrobí nový token a krabička se ohlásí jako nespárovaná"
    )
    assert config.get("server") == "https://bikody.com"
    assert config.get("autostart") is True


def test_nastaveni_se_prenese_do_nove_slozky(tmp_path, monkeypatch):
    """Přenos je jednorázový — příště se čte rovnou z nové cesty."""
    stara, nova = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="PUVODNI-TOKEN")

    ta.load_config()

    prenesene = json.loads((nova / "config.json").read_text(encoding="utf-8"))
    assert prenesene["token"] == "PUVODNI-TOKEN"


def test_preliv_se_prenese_taky(tmp_path, monkeypatch):
    """Neodeslané rámce nejsou odvozený údaj — jinde je nikdo nevyrobí."""
    stara, _ = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="T")
    (stara / "preliv-smycka.txt").write_text("ramec-1\nramec-2\n", encoding="utf-8")

    ta.load_config()

    prenesene = ta.config_path().with_name("preliv-smycka.txt")
    assert prenesene.exists(), "rámce čekající na disku zůstaly ve staré složce"
    assert prenesene.read_text(encoding="utf-8") == "ramec-1\nramec-2\n"


def test_nova_slozka_ma_prednost(tmp_path, monkeypatch):
    """Stará složka je záchyt, ne zdroj pravdy.

    Krabička spárovaná po aktualizaci nesmí při restartu spadnout zpátky na
    token, který už v aplikaci neplatí.
    """
    stara, nova = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="STARY")
    _uloz(nova, server="https://bikody.com", token="NOVY")

    assert ta.load_config().get("token") == "NOVY"


def test_prenos_neprepise_co_uz_v_nove_slozce_je(tmp_path, monkeypatch):
    stara, nova = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="STARY")
    (stara / "preliv-smycka.txt").write_text("stare\n", encoding="utf-8")
    nova.mkdir(parents=True, exist_ok=True)
    (nova / "preliv-smycka.txt").write_text("nove\n", encoding="utf-8")

    ta.prenes_stara_nastaveni()

    assert (nova / "preliv-smycka.txt").read_text(encoding="utf-8") == "nove\n"


def test_stara_slozka_zustane_lezet(tmp_path, monkeypatch):
    """Návrat na předchozí verzi agenta musí zůstat možný."""
    stara, _ = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="PUVODNI-TOKEN")

    ta.load_config()

    assert (stara / "config.json").exists(), "původní nastavení se smazalo"


def test_bez_stare_slozky_se_nic_nedeje(tmp_path, monkeypatch):
    _slozky(tmp_path, monkeypatch)

    assert ta.load_config() == {}


def test_prenasi_se_jen_nastaveni_a_preliv(tmp_path, monkeypatch):
    """Ve staré složce může ležet cokoli — brát všechno je zbytečné riziko."""
    stara, nova = _slozky(tmp_path, monkeypatch)
    _uloz(stara, server="https://bikody.com", token="T")
    (stara / "neco-jineho.log").write_text("x", encoding="utf-8")

    ta.prenes_stara_nastaveni()

    assert not (nova / "neco-jineho.log").exists()
