"""Párovací kód místo tokenu na displeji (David 12. 10. 2026).

Obsluha opisuje pět znaků, ne šest čtveřic. Token zůstává dlouhý — je to
heslo do klubové sítě — a krabička si k němu od serveru vyžádá krátký kód.
"""
from __future__ import annotations

import urllib.error

import track_agent as ta

TOKEN = "AKUW-BCDE-FGHJ-KMNP-QRST-7G59"


def _worker(odpoved):
    worker = ta.Worker("http://192.168.9.1", TOKEN)

    def pair():
        if isinstance(odpoved, Exception):
            raise odpoved
        return odpoved

    worker.server.pair = pair
    return worker


def test_cekajici_krabicka_ukaze_kod_misto_tokenu():
    worker = _worker({"code": "k7rq4", "expires_in": 600})

    assert worker._vyzadej_kod() is False
    stav = ta._screen_state(worker, {"server": "http://192.168.9.1", "token": TOKEN})

    assert stav["slovo"] == "WAITING"
    assert stav["token"] == "K7RQ4"
    assert "code" in stav["token_popisek"]


def test_spojena_krabicka_hned_pozdravi():
    worker = _worker({"paired": True})

    assert worker._vyzadej_kod() is True


def test_cloud_bez_parovani_ukaze_cely_token():
    worker = _worker(urllib.error.HTTPError("http://x/pair/", 404, "Not Found", {}, None))
    worker.parovaci_kod = "K7RQ4"

    assert worker._vyzadej_kod() is False
    stav = ta._screen_state(worker, {"server": "http://192.168.9.1", "token": TOKEN})

    assert stav["token"] == TOKEN, "starý cloud kód nevydá — opisuje se token jako dřív"


def test_prosly_kod_se_neukaze(monkeypatch):
    """Výpadek spojení: kód, který vypršel, na displeji nesmí zůstat."""
    worker = _worker({"code": "K7RQ4", "expires_in": 600})
    worker._vyzadej_kod()
    worker.parovaci_kod_do = 0.0

    stav = ta._screen_state(worker, {"server": "http://192.168.9.1", "token": TOKEN})

    assert stav["token"] == TOKEN


def test_404_z_parovani_neprepne_provoz_na_starou_cestu(monkeypatch):
    server = ta.Server("http://192.168.9.1", TOKEN)
    cesty = []

    def odesli(path, payload=None, *, timeout):
        cesty.append(path)
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    monkeypatch.setattr(server, "_odesli", odesli)
    try:
        server.pair()
    except urllib.error.HTTPError:
        pass

    assert cesty == [ta.CESTA + "/pair/"]
    assert server._stara_cesta is False


def test_kod_se_do_site_neprozradi():
    """Kdo kód z LAN opíše dřív než obsluha, odnese si krabičku."""
    assert ta._token_pro_klienta("K7RQ4", False) == "•••••"
    assert ta._token_pro_klienta("K7RQ4", True) == "K7RQ4"
