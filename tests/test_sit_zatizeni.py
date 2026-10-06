"""Síťová zátěž krabičky (agent 1.17, po BMX víkendu 3.–4. 10. 2026).

Krabička se do 1.16 ptala na příkazy hned po každé odpovědi — 27 897
dotazů za dva dny, každý držel workera serveru vteřinu. Tyhle testy hlídají,
že se v klidu ptá řidčeji, po příkazu hned, a že dávky průjezdů pořád
odcházejí bez čekání, nezdvojují se a pomalý server nezahltí.
"""
from __future__ import annotations

import threading
import time

import track_agent as ta

TOKEN = "AKUW-BCDE-FGHJ-KMNP-QRST-7G59"


# --- přestávka mezi dotazy na příkazy ---------------------------------------

def test_po_prikazu_se_pta_hned():
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    ted = time.monotonic()
    worker._posledni_prikaz = ted - 1.0

    assert worker.pauza_pred_dotazem(ted) == 0.0


def test_v_klidu_s_proudem_vterina():
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    worker._streams = {"d": object()}
    ted = time.monotonic()
    worker._posledni_prikaz = ted - 10 * ta.PRIKAZY_KLID_PO_S

    # Měří se: proud jede, takže ani dlouhé ticho neznamená hluboký klid.
    assert worker.pauza_pred_dotazem(ted) == ta.PRIKAZY_PAUZA_S


def test_bez_proudu_a_bez_prikazu_hluboky_klid():
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    ted = time.monotonic()
    worker._posledni_prikaz = ted - ta.PRIKAZY_PAUZA_S - ta.PRIKAZY_HORKO_S
    assert worker.pauza_pred_dotazem(ted) == ta.PRIKAZY_PAUZA_S

    worker._posledni_prikaz = ted - ta.PRIKAZY_KLID_PO_S
    assert worker.pauza_pred_dotazem(ted) == ta.PRIKAZY_PAUZA_KLID_S


def test_rozpocet_prikazu_na_serveru_vydrzi():
    """Nejtěsnější rozpočet má `tcp_exchange`: 1,5 s na dekodér + 2 s na cestu.

    Přestávka se k doručení prvního příkazu přičte celá, takže i s nejdelším
    čtením z dekodéru a dvěma cestami po LTE (2× 0,4 s) se do něj musí vejít.
    """
    nejhorsi = ta.PRIKAZY_PAUZA_S + 1.5 + 2 * 0.4
    assert nejhorsi < 1.5 + 2.0
    # Hluboký klid jen s typickým čtením (ticho 0,3 s), proto zvlášť.
    assert ta.PRIKAZY_PAUZA_KLID_S + 0.35 + 2 * 0.4 < 1.5 + 2.0


class _Server:
    """Dvojník serveru: zapisuje, kdy se krabička ptala."""

    base = "http://192.168.9.1"

    def __init__(self, odpovedi, hotovo, worker):
        self.odpovedi = list(odpovedi)
        self.hotovo = hotovo
        self.worker = worker
        self.dotazy: list[float] = []
        self.vysledky: list[str] = []

    def hello(self):
        return {"agent": "a", "organization": "o"}

    def poll(self):
        self.dotazy.append(time.monotonic())
        if not self.odpovedi:
            self.hotovo.set()
            self.worker._stop.wait(5)
            return {}
        return self.odpovedi.pop(0)

    def result(self, command_id, ok, data=None, error=""):
        self.vysledky.append(command_id)


def _prubeh(monkeypatch, odpovedi, *, posledni_prikaz):
    monkeypatch.setattr(ta, "PRIKAZY_PAUZA_S", 0.3)
    monkeypatch.setattr(ta, "PRIKAZY_PAUZA_KLID_S", 0.3)
    monkeypatch.setattr(ta, "PRIKAZY_HORKO_S", 0.2)
    monkeypatch.setattr(ta, "ACTIONS", {"nic": lambda _args: {}})
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    hotovo = threading.Event()
    worker.server = _Server(odpovedi, hotovo, worker)
    worker._posledni_prikaz = posledni_prikaz
    worker.start()
    try:
        assert hotovo.wait(5), "krabička se přestala ptát"
    finally:
        worker.stop()
    return worker.server


def test_v_klidu_se_mezi_dotazy_ceka(monkeypatch):
    server = _prubeh(monkeypatch, [{}, {}, {}], posledni_prikaz=0.0)

    mezery = [b - a for a, b in zip(server.dotazy, server.dotazy[1:])]
    assert len(mezery) == 3
    assert all(m >= 0.25 for m in mezery), mezery


def test_po_prikazu_se_zepta_bez_prestavky(monkeypatch):
    """Navazující příkaz (další kontrolka, další stahování) nesmí čekat."""
    prikaz = {"commands": [{"id": "c1", "action": "nic", "args": {}}]}
    server = _prubeh(monkeypatch, [{}, prikaz, {}], posledni_prikaz=0.0)

    assert server.vysledky == ["c1"]
    pred, po_prikazu = server.dotazy[1] - server.dotazy[0], server.dotazy[2] - server.dotazy[1]
    assert pred >= 0.25, "v klidu se čeká"
    assert po_prikazu < 0.1, "po příkazu se ptá hned"


# --- dávky průjezdů -----------------------------------------------------------

def test_davka_ma_delsi_timeout_nez_nejpomalejsi_potvrzeni(monkeypatch):
    """Server o víkendu potvrzoval i za 17 s; s 15 s se dávka posílala znovu."""
    server = ta.Server("http://192.168.9.1", TOKEN)
    zachyceno = {}

    def request(path, payload=None, *, timeout):
        zachyceno["timeout"] = timeout
        return {"ok": True}

    monkeypatch.setattr(server, "_request", request)
    server.push_passings("d", ["AAA"])

    assert zachyceno["timeout"] == ta.PRUJEZDY_TIMEOUT_S >= 17


def _odesilatel(link, fronta):
    vlakno = threading.Thread(target=link._send_loop, args=("d", fronta))
    vlakno.start()
    return vlakno


def test_odlozena_davka_se_neopakuje_kazdou_vterinu(monkeypatch, tmp_path):
    """„Krabička nemá přiřazený závod" platí, dokud ho obsluha nepřiřadí."""
    monkeypatch.setattr(ta, "STREAM_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(ta, "STREAM_ODLOZENO_SECONDS", 0.2)
    volani = []

    class Server:
        def push_passings(self, _d, frames, casy=None):
            volani.append(time.monotonic())
            return {"ok": False, "code": "no_open_event"}

    link = ta.StreamLink(Server(), {})
    with ta.FrameQueue(tmp_path / "q.db") as fronta:
        fronta.pridej(["frame"])
        vlakno = _odesilatel(link, fronta)
        time.sleep(1.0)
        link.stop()
        vlakno.join(2)
        assert fronta.ceka() == 1, "odložená dávka se nesmí zahodit"
    # 0,05 → 0,1 → 0,2 → 0,2 …: kolem sedmi pokusů. Se stropem chyby
    # spojení (0,01 s) by jich bylo přes padesát.
    assert 3 <= len(volani) <= 10, len(volani)


def test_po_timeoutu_se_ta_sama_davka_neposila_hned(monkeypatch, tmp_path):
    monkeypatch.setattr(ta, "STREAM_RETRY_SECONDS", 0.3)
    hotovo = threading.Event()
    volani = []

    class Server:
        def push_passings(self, _d, frames, casy=None):
            volani.append(time.monotonic())
            if len(volani) == 1:
                raise TimeoutError("the server is still working")
            hotovo.set()
            return {"ok": True, "stored": 1}

    link = ta.StreamLink(Server(), {})
    with ta.FrameQueue(tmp_path / "q.db") as fronta:
        fronta.pridej(["frame"])
        vlakno = _odesilatel(link, fronta)
        try:
            assert hotovo.wait(3)
        finally:
            link.stop()
            vlakno.join(2)
        assert fronta.ceka() == 0
    assert volani[1] - volani[0] >= 0.25, "po timeoutu se nečeká ani vteřinu"


def test_pomaly_server_dostane_davku_ne_duplicitu(tmp_path):
    """Co přijde během pomalého potvrzení, odejde **jednou** a pohromadě."""
    zacal, pust = threading.Event(), threading.Event()
    davky = []

    class Server:
        def push_passings(self, _d, frames, casy=None):
            davky.append(list(frames))
            if len(davky) == 1:
                zacal.set()
                assert pust.wait(3)
            return {"ok": True, "stored": len(frames)}

    link = ta.StreamLink(Server(), {})
    with ta.FrameQueue(tmp_path / "q.db") as fronta:
        fronta.pridej(["f0"])
        link._ready.set()
        vlakno = _odesilatel(link, fronta)
        try:
            assert zacal.wait(2)
            for cislo in range(1, 6):
                fronta.pridej([f"f{cislo}"])
                link._ready.set()
            pust.set()
            konec = time.monotonic() + 2
            while fronta.ceka() and time.monotonic() < konec:
                time.sleep(0.01)
        finally:
            link.stop()
            vlakno.join(2)
        assert fronta.ceka() == 0
    assert davky == [["f0"], ["f1", "f2", "f3", "f4", "f5"]]


def test_necinny_odesilatel_nehoni_frontu(tmp_path):
    """Bez rámců se odesílatel nebudí každou vteřinu; rámec ho vzbudí hned."""
    odeslano = threading.Event()

    class Server:
        def push_passings(self, _d, frames, casy=None):
            odeslano.set()
            return {"ok": True, "stored": len(frames)}

    link = ta.StreamLink(Server(), {})
    with ta.FrameQueue(tmp_path / "q.db") as fronta:
        dotazy = []
        puvodni = fronta.dalsi

        def dalsi(limit):
            dotazy.append(time.monotonic())
            return puvodni(limit)

        fronta.dalsi = dalsi
        vlakno = _odesilatel(link, fronta)
        try:
            time.sleep(1.3)
            assert len(dotazy) == 1, "odesílatel se budí naprázdno"
            zacatek = time.monotonic()
            fronta.pridej(["f"])
            link._ready.set()
            assert odeslano.wait(1)
            assert time.monotonic() - zacatek < 0.2
        finally:
            link.stop()
            vlakno.join(2)
