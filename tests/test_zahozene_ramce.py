"""Zahozený rámec — ztráta, kterou krabička nedohoní sama.

Přeplněná fronta jde nejdřív **na disk** (`test_preliv.py`); sem se dostane
jen to, co se nevešlo ani tam: plná karta nebo výpadek delší než strop
souboru. Až tohle je skutečná ztráta.

Dohledat ji umí jen server: dekodér si průjezdy pamatuje, ale krabička
**protokolu P3 nerozumí** a rozumět nemá (`track_agent.py`: *„Neví nic o P3
ani o formátu startovky"*). Server je stáhne přes `tcp_exchange` na téhle
krabičce a **spouští to člověk** tlačítkem „Dohledat průjezdy z dekodéru".

Do 7. 9. 2026 se rámce při přeplněné frontě zahazovaly **mlčky**, s poznámkou
„dotáhne si to záložkou". Jenže nikdo se nedozvěděl, že se to má udělat: na
displeji ani v aplikaci o tom nebylo slovo. Tichá ztráta výsledku.
"""
from __future__ import annotations

import track_agent as ta


def _vynuluj():
    with ta._prujezdy_lock:
        ta._prujezdy["zahozeno"] = 0
        ta._prujezdy["zahozeno_kdy"] = ""


def test_zahozene_ramce_se_scitaji():
    _vynuluj()

    ta._zaznamenat_zahozene(12)
    ta._zaznamenat_zahozene(3)

    stav = ta._prujezdy_stav()
    assert stav["zahozeno"] == 15
    assert stav["zahozeno_kdy"], "musí být vidět, kdy se to stalo"


def test_nula_a_zapor_se_nepocitaji():
    """Ať se počítadlo nerozsvítí samo tím, že se fronta vyprázdnila."""
    _vynuluj()

    ta._zaznamenat_zahozene(0)
    ta._zaznamenat_zahozene(-5)

    assert ta._prujezdy_stav()["zahozeno"] == 0
    assert ta._prujezdy_stav()["zahozeno_kdy"] == ""


def test_stav_pro_displej_zahozene_nese():
    """Displej u trati je jediné místo, kde to obsluha uvidí bez internetu."""
    _vynuluj()
    ta._zaznamenat_zahozene(7)

    stav = ta._prujezdy_stav()

    assert stav["zahozeno"] == 7
    # A nesmí to zmizet s pulsem diody: ztráta není blik, je to stav.
    assert "zahozeno" in stav and "cerstvy" in stav


def test_hello_hlasi_zahozene_serveru(monkeypatch):
    """Server je ten, kdo umí dohledat — musí se to k němu dostat."""
    _vynuluj()
    ta._zaznamenat_zahozene(4)

    poslano = {}

    def falesny_request(self, cesta, telo=None, **kwargs):
        poslano["cesta"] = cesta
        poslano["telo"] = telo
        return {}

    monkeypatch.setattr(ta.Server, "_request", falesny_request)
    ta.Server("https://cloud", "T" * 24).hello()

    assert poslano["cesta"] == "/bmx/api/agent/hello/"
    assert poslano["telo"]["dropped_frames"] == 4


def test_displej_ma_pruh_a_rekne_co_delat():
    """Bez věty „co teď" je hlášení ztráty jen zlá zpráva.

    Obsluha u trati je ten, kdo dohledání spouští — a při výpadku aplikace
    je displej krabičky jediné místo, kde se to dozví.
    """
    sablona = ta._SCREEN

    assert "zahozeno-pruh" in sablona
    assert "DOHLEDEJTE PRŮJEZDY Z DEKODÉRU" in sablona
    # Ztráta **není puls**: pruh nesmí zmizet po dvou sekundách jako dioda.
    assert "pruh.hidden = zahozeno <= 0" in sablona
