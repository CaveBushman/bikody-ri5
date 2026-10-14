"""Věta serveru z chybové odpovědi se dostane k obsluze (1.24)."""
import http.server
import importlib.util
import json
import threading
import urllib.error
from pathlib import Path

import pytest


def agent_module():
    spec = importlib.util.spec_from_file_location(
        'ri_detail', Path(__file__).resolve().parents[1] / 'agent/track_agent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Odmitni(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        telo = json.dumps({"error": "protocol_too_new",
                           "detail": "Nasaďte cloud."}).encode()
        self.send_response(426)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(telo)))
        self.end_headers()
        self.wfile.write(telo)

    def log_message(self, *args):
        pass


def test_veta_serveru_projde_drzenym_spojenim():
    """Do 1.23 se tělo zahodilo a `_detail_chyby` vracel vždy prázdno."""
    agent = agent_module()
    server = http.server.HTTPServer(("127.0.0.1", 0), _Odmitni)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        klient = agent.Server(f"http://127.0.0.1:{server.server_port}", "T")
        with pytest.raises(urllib.error.HTTPError) as chyba:
            klient._odesli("/hello/", {"protocol": 3}, timeout=2)
        assert chyba.value.code == 426
        assert agent._detail_chyby(chyba.value) == "Nasaďte cloud."
    finally:
        server.shutdown()


def test_aktualizace_se_nabizi_jen_novejsi_verze():
    """Krabička novější než cloud nesmí dostat „aktualizaci" na starší (1.24)."""
    agent = agent_module()
    assert agent._verze("1.24") > agent._verze("1.9")
    assert not agent._verze("1.23") > agent._verze("1.24")
