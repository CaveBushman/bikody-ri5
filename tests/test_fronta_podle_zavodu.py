"""Fronta průjezdů vedená zvlášť pro každý závod (agent 1.23).

Kontrola spolupráce systémů 13. 10. 2026: fronta smyčky, která z konfigurace
zmizela (konec odběru, uzavření závodu, přeřazení krabičky), čekala na disku
a odešla, až se tatáž smyčka objevila u **jiného** závodu — server ji zapsal
do závodu, který měl token zrovna přiřazený. Rozhodnutí Davida: nechat
a ukázat, do jiného závodu neposlat.
"""
import importlib.util
import threading
from pathlib import Path

import pytest


@pytest.fixture
def ta(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "ri_fronta", Path(__file__).resolve().parents[1] / "agent/track_agent.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    monkeypatch.setattr(modul, "config_path", lambda: tmp_path / "config.json")
    return modul


def test_kazdy_zavod_ma_vlastni_frontu(ta):
    sobota = ta.preliv_path("dek-1", "sobota").with_suffix(".sqlite3")
    nedele = ta.preliv_path("dek-1", "nedele").with_suffix(".sqlite3")
    bez = ta.preliv_path("dek-1").with_suffix(".sqlite3")

    assert len({sobota, nedele, bez}) == 3
    assert sobota.parent == nedele.parent


def test_davka_nese_svuj_zavod(ta):
    poslano = []

    class Server:
        def push_passings(self, decoder, frames, casy=None, event=""):
            poslano.append((decoder, list(frames), event))
            hotovo.set()
            return {"ok": True, "stored": len(frames)}

    hotovo = threading.Event()
    link = ta.StreamLink(Server(), {"decoder": "dek-1", "event": "nedele"})
    with ta.FrameQueue(ta.preliv_path("dek-1", "nedele").with_suffix(".sqlite3")) as fronta:
        fronta.pridej(["ramec"])
        odesilatel = threading.Thread(target=link._send_loop, args=("dek-1", fronta))
        odesilatel.start()
        try:
            assert hotovo.wait(3)
        finally:
            link.stop()
            odesilatel.join(3)

    assert poslano == [("dek-1", ["ramec"], "nedele")]


def test_fronta_stareho_zavodu_zustane_odlozena_a_je_videt(ta):
    with ta.FrameQueue(ta.preliv_path("dek-1", "sobota").with_suffix(".sqlite3")) as stara:
        stara.pridej(["a", "b", "c"])
    nedele = str(ta.preliv_path("dek-1", "nedele").with_suffix(".sqlite3"))

    assert ta._odlozene_fronty({nedele}) == 3


def test_stara_fronta_bez_zavodu_se_prevezme_do_aktualniho(ta):
    """Fronta z ≤1.22 patří k závodu, který smyčka jede teď — tak se s ní
    zacházelo vždycky."""
    stara = ta.preliv_path("dek-1").with_suffix(".sqlite3")
    with ta.FrameQueue(stara) as fronta:
        fronta.pridej(["x", "y"])

    with ta.FrameQueue(ta.preliv_path("dek-1", "nedele").with_suffix(".sqlite3")) as nova:
        ta._prevezmi_starou_frontu(stara, nova)
        assert nova.dalsi(10) == ["x", "y"]

    assert not stara.exists()


def test_jiny_zavod_restartuje_proud(ta):
    link = ta.StreamLink(None, {"decoder": "dek-1", "host": "192.168.9.20", "event": "sobota"})

    assert not link.matches({"decoder": "dek-1", "host": "192.168.9.20", "event": "nedele"})
