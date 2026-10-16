"""Srovnání hodin dekodérů v okamžiku celé sekundy (1.25, 16. 10. 2026).

Server dřív spočítal čas při zařazení příkazu a dekodéry ho dostaly postupně
o cestu později — Finish zůstal 1,2 s za Hill. Teď okamžik určuje krabička.
"""
from __future__ import annotations

import base64
import socketserver
import threading
import time

import pytest

import track_agent as ta


class Hodiny(socketserver.BaseRequestHandler):
    prijato: list = []

    def handle(self):
        data = self.request.recv(4096)
        Hodiny.prijato.append((time.time(), data))
        self.request.sendall(b"OK:" + data)


@pytest.fixture
def dekodery(monkeypatch):
    monkeypatch.setattr(ta, "_validated_target", lambda host, port: (host, int(port)))
    Hodiny.prijato = []
    servery = [socketserver.ThreadingTCPServer(("127.0.0.1", 0), Hodiny) for _ in range(2)]
    for server in servery:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield [s.server_address for s in servery]
    for server in servery:
        server.shutdown()
        server.server_close()
    ta._pool.clear()


def test_posle_vsem_naraz_na_zacatku_sekundy(dekodery):
    ted = int(time.time())
    cile = [{"host": h, "port": p, "ramce": {str(s): base64.b64encode(f"S{s}".encode()).decode() for s in range(ted, ted + 5)}}
            for h, p in dekodery]

    data = ta.ACTIONS["tcp_v_sekunde"]({"cile": cile, "timeout": 1, "quiet": 0.1})

    sekunda = data["sekunda"]
    assert [base64.b64decode(v["data"]) for v in data["vysledky"]] == [f"OK:S{sekunda}".encode()] * 2
    casy = [kdy for kdy, _d in Hodiny.prijato]
    assert len(casy) == 2 and all(0 <= kdy - sekunda < 0.1 for kdy in casy), casy


def test_bez_budoucich_ramcu_odmitne(dekodery):
    (h, p), _ = dekodery
    with pytest.raises(ValueError, match="uplynuly"):
        ta.ACTIONS["tcp_v_sekunde"]({"cile": [{"host": h, "port": p, "ramce": {"1000": "eA=="}}]})
