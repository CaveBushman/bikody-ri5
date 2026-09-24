"""Nálezy auditu 23. 9. 2026 — každý test hlídá jednu opravenou díru.

Uložené XSS přes „Zkusit spojení", celý token pro kohokoli v síti, tiše
umřelá vlákna, neatomický zápis nastavení, příkazy na loopback a čtení těla
POST bez stropu.
"""
from __future__ import annotations

import http.client
import json
import os
import stat
import threading
import time

import pytest

import track_agent as ta

TOKEN = "AKUW-BCDE-FGHJ-KMNP-QRST-7G59"
ZLO = '"><script>alert(1)</script>'


# --- XSS -------------------------------------------------------------------


def test_nastaveni_escapuje_zkousku_i_tabulku(monkeypatch):
    monkeypatch.setattr(ta, "_posledni_zkouska", {"host": ZLO, "port": ZLO})
    monkeypatch.setattr(ta, "_recent", [])
    ta._remember(ZLO, 1, False, "<img src=x onerror=alert(2)>")

    class Worker:
        status = "server unavailable (<b>boom</b>)"
        latest_version = "<i>9.9</i>"
        connected = False

    stranka = ta._render_settings(Worker(), {"token": TOKEN, "server": ZLO}).decode()

    assert "<script>alert(1)" not in stranka
    assert "<img src=x" not in stranka
    assert "<b>boom</b>" not in stranka
    assert "<i>9.9</i>" not in stranka
    assert "&lt;script&gt;" in stranka


def test_displej_escapuje_stav_agenta():
    class Worker:
        connected = False
        status = "<script>alert(3)</script>"

    stranka = ta._render_screen(Worker(), {"token": TOKEN}).decode()

    assert "<script>alert(3)" not in stranka
    assert "&lt;script&gt;alert(3)" in stranka


# --- token jen tomuhle počítači ---------------------------------------------


def test_token_celý_jen_mistne():
    mistni = json.loads(ta._stav_json(None, {"token": TOKEN}, plny_token=True))
    cizi = json.loads(ta._stav_json(None, {"token": TOKEN}, plny_token=False))

    assert mistni["token"].replace("\n", "-") == TOKEN
    assert cizi["token"] == "AKUW-••••"
    assert "7G59" not in ta._render_settings(None, {"token": TOKEN}).decode()
    assert "7G59" not in ta._render_screen(None, {"token": TOKEN}).decode()


def test_vyroba_tokenu_neloguje_cely_token(monkeypatch, capsys):
    monkeypatch.setattr(ta, "save_config", lambda *a, **k: None)

    token = ta.ensure_token({})

    vystup = capsys.readouterr().out
    assert token not in vystup
    assert token.split("-")[0] + "-••••" in vystup


# --- HTTP server ------------------------------------------------------------


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setattr(ta, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ta, "load_config", lambda: {"token": TOKEN})
    monkeypatch.setattr(ta, "_recent", [])
    server = ta.build_web_server({}, host="127.0.0.1", port=0)
    vlakno = threading.Thread(target=server.serve_forever, daemon=True)
    vlakno.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _post(port, cesta, telo=b"", hlavicky=None):
    spojeni = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    spojeni.request("POST", cesta, body=telo, headers={
        "Content-Type": "application/x-www-form-urlencoded", **(hlavicky or {})})
    odpoved = spojeni.getresponse()
    odpoved.read()
    spojeni.close()
    return odpoved


def _get(port, cesta):
    spojeni = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    spojeni.request("GET", cesta)
    odpoved = spojeni.getresponse()
    telo = odpoved.read()
    spojeni.close()
    return odpoved, telo


def test_zkusit_z_lan_odmitnuto(web, monkeypatch):
    monkeypatch.setattr(ta, "_je_z_tohoto_pocitace", lambda _adresa: False)

    odpoved = _post(web, "/zkusit", b"host=1.2.3.4&port=x")

    assert odpoved.status == 403
    assert ta._posledni_zkouska.get("host") != "1.2.3.4"


def test_zkusit_mistne_projde(web):
    assert _post(web, "/zkusit", b"host=192.168.9.25&port=x").status == 303


def test_post_z_cizi_stranky_odmitnut_i_z_loopbacku(web):
    cizi = _post(web, "/zkusit", b"host=192.168.9.25&port=x",
                 {"Origin": "http://zlo.example"})
    vlastni = _post(web, "/zkusit", b"host=192.168.9.25&port=x",
                    {"Origin": f"http://127.0.0.1:{web}"})
    fetch = _post(web, "/zkusit", b"host=192.168.9.25&port=x",
                  {"Sec-Fetch-Site": "cross-site"})

    assert cizi.status == 403
    assert fetch.status == 403
    assert vlastni.status == 303


def test_velke_telo_se_necte(web):
    spojeni = http.client.HTTPConnection("127.0.0.1", web, timeout=5)
    # Hlavička slibuje 10 MB, posílá se nic: server musí odpovědět hned,
    # ne čekat na tělo (a ne ho číst do paměti).
    spojeni.putrequest("POST", "/zkusit")
    spojeni.putheader("Content-Length", str(10 * 1024 * 1024))
    spojeni.endheaders()
    odpoved = spojeni.getresponse()
    spojeni.close()

    assert odpoved.status == 413


def test_get_z_lan_dostane_maskovany_token(web, monkeypatch):
    _odpoved, mistni = _get(web, "/stav")
    monkeypatch.setattr(ta, "_je_z_tohoto_pocitace", lambda _adresa: False)
    odpoved, cizi = _get(web, "/stav")

    assert "7G59" in mistni.decode()
    assert "7G59" not in cizi.decode()
    assert "frame-ancestors 'none'" in odpoved.headers["Content-Security-Policy"]


def test_web_ma_timeout_soketu():
    server = ta.build_web_server({}, host="127.0.0.1", port=0)
    try:
        assert server.RequestHandlerClass.timeout == ta.WEB_TIMEOUT_S
    finally:
        server.server_close()


# --- vlákna ---------------------------------------------------------------


class _Vysledky:
    def __init__(self):
        self.vysledky = []

    def result(self, command_id, ok, data=None, error=""):
        self.vysledky.append((command_id, ok, error))


def test_prikaz_bez_hostu_nezabije_vlakno():
    server = _Vysledky()

    ta.run_command(server, {"id": "c1", "action": "tcp_probe", "args": {}})
    ta.run_command(server, {"id": "c2", "action": "tcp_probe", "args": ["x"]})

    assert [(i, ok) for i, ok, _ in server.vysledky] == [("c1", False), ("c2", False)]
    assert "invalid command" in server.vysledky[0][2]


def test_divna_odpoved_serveru_nezabije_workera(monkeypatch):
    monkeypatch.setattr(ta, "RECONNECT_MIN", 0.01)
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    druhy = threading.Event()

    class Server(_Vysledky):
        base = "http://192.168.9.1"
        volani = 0

        def hello(self):
            return {"agent": "a", "organization": "o"}

        def poll(self):
            Server.volani += 1
            if Server.volani == 1:
                return ["not", "a", "dict"]
            if Server.volani == 2:
                return {"commands": [{"id": "c", "action": "tcp_probe"}, "junk"]}
            druhy.set()
            worker._stop.wait(5)
            return {}

    worker.server = Server()
    worker.start()
    try:
        assert druhy.wait(3), "vlákno agenta umřelo"
        assert worker.is_running()
        assert worker.server.vysledky and worker.server.vysledky[0][1] is False
    finally:
        worker.stop()


def test_odesilatel_prezije_divnou_odpoved(monkeypatch, tmp_path):
    monkeypatch.setattr(ta, "STREAM_RETRY_SECONDS", 0.01)
    hotovo = threading.Event()
    volani = []

    class Server:
        def push_passings(self, _d, frames, casy=None):
            volani.append(frames)
            if len(volani) == 1:
                return ["not a dict"]
            if len(volani) == 2:
                raise RuntimeError("unexpected")
            hotovo.set()
            return {"ok": True, "stored": 1}

    link = ta.StreamLink(Server(), {})
    with ta.FrameQueue(tmp_path / "q.db") as fronta:
        fronta.pridej(["frame"])
        odesilatel = threading.Thread(target=link._send_loop, args=("d", fronta))
        odesilatel.start()
        try:
            assert hotovo.wait(3), "odesílatel umřel"
        finally:
            link.stop()
            odesilatel.join(2)
        assert fronta.ceka() == 0
        assert len(volani) == 3


def test_mrtvy_odesilatel_znamena_mrtvy_proud():
    link = ta.StreamLink(None, {"decoder": "d"})
    link._thread = threading.Thread(target=lambda: time.sleep(1), daemon=True)
    link._thread.start()
    link._sender = threading.Thread(target=lambda: None)
    link._sender.start()
    link._sender.join()

    assert not link.is_alive()
    assert link.hlaseni()["zije"] is False


def test_displej_nesviti_zelene_nad_mrtvym_vlaknem():
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    worker.connected = True
    worker._thread = threading.Thread(target=lambda: None)
    worker._thread.start()
    worker._thread.join()

    assert ta._screen_state(worker, {"token": TOKEN})["slovo"] != "OK"
    assert worker.umrel()


def test_hlidac_ukonci_proces_pri_mrtvem_vlaknu(monkeypatch):
    monkeypatch.setattr(ta, "HLIDAC_S", 0.01)
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    worker._thread = threading.Thread(target=lambda: None)
    worker._thread.start()
    worker._thread.join()
    ukonceno = threading.Event()

    ta._hlidej_workera({"worker": worker}, konec=ukonceno.set)

    assert ukonceno.is_set()


def test_zastaveny_worker_neni_umrely():
    worker = ta.Worker("http://192.168.9.1", TOKEN)
    worker._thread = threading.Thread(target=lambda: None)
    worker._thread.start()
    worker.stop()

    assert not worker.umrel()


# --- zápis nastavení ------------------------------------------------------


def test_save_config_je_atomicky_a_0600(monkeypatch, tmp_path):
    cesta = tmp_path / "config.json"
    monkeypatch.setattr(ta, "config_path", lambda: cesta)
    ta.save_config("https://bikody.com", TOKEN)

    def rozbite_prejmenovani(*_a):
        raise OSError("power loss")

    monkeypatch.setattr(ta.os, "replace", rozbite_prejmenovani)
    with pytest.raises(OSError):
        ta.save_config("https://jinde", "NOVY-TOKEN")

    assert json.loads(cesta.read_text())["token"] == TOKEN, "starý soubor zůstal celý"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"], "dočasný soubor uklizen"
    if os.name == "posix":
        assert stat.S_IMODE(cesta.stat().st_mode) == 0o600


# --- cíle příkazů ---------------------------------------------------------


def test_prikaz_na_loopback_odmitnut(monkeypatch):
    with pytest.raises(ValueError):
        ta._validated_target("127.0.0.1", 8088)
    monkeypatch.setattr(ta, "POVOLIT_LOOPBACK", True)
    assert ta._validated_target("127.0.0.1", 5403) == ("127.0.0.1", 5403)
    assert ta._validated_target("192.168.9.25", 5403) == ("192.168.9.25", 5403)


def test_verze_zvednuta():
    assert ta.VERSION == "1.15"
