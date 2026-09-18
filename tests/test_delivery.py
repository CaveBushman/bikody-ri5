"""Transportní regrese RI; socketpair bez dekodéru a bez internetu."""
import base64
import importlib.util
from pathlib import Path
import socket
import threading
import time

import pytest


def agent_module():
    spec = importlib.util.spec_from_file_location(
        'ri_transport', Path(__file__).resolve().parents[1] / 'agent/track_agent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agent():
    return agent_module()


def test_queue_survives_restart_and_ack_only_deletes_offered_frames(agent, tmp_path):
    path = tmp_path / 'queue.db'
    with agent.FrameQueue(path) as queue:
        assert queue.pridej(['first', 'second']) == 0
        assert queue.dalsi(1) == ['first']
        assert queue.pridej(['third']) == 0
        queue.potvrd()
    with agent.FrameQueue(path) as queue:
        assert queue.dalsi(50) == ['second', 'third']
    # Bez ACK se po restartu nabídne stejná dávka.
    with agent.FrameQueue(path) as queue:
        assert queue.dalsi(50) == ['second', 'third']
        queue.potvrd()
        assert queue.ceka() == 0


def test_expired_frames_and_full_queue_are_accounted_for(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, 'PRELIV_MAX_BAJTU', 8)
    with agent.FrameQueue(tmp_path / 'queue.db') as queue:
        assert queue.pridej(['old'], received=[time.time() - 3 * 86400]) == 0
        assert queue.pridej(['new']) == 0
        assert queue.pridej(['overflow']) == 1
        assert queue.dalsi(50) == ['new']
        assert agent._prujezdy_stav()['zahozeno'] == 1


@pytest.mark.parametrize('failure', ['reject', 'lost_ack'])
def test_sender_retries_without_another_passing(agent, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(agent, 'STREAM_RETRY_SECONDS', .01)
    done = threading.Event()
    calls = []
    class Server:
        def push_passings(self, _, frames, casy=None):
            calls.append(frames)
            if len(calls) == 1:
                if failure == 'lost_ack':
                    raise OSError('ACK lost')
                return {'ok': False}
            done.set()
            return {'ok': True, 'stored': 1}
    link = agent.StreamLink(Server(), {})
    with agent.FrameQueue(tmp_path / 'queue.db') as queue:
        queue.pridej(['frame'])
        sender = threading.Thread(target=link._send_loop, args=('d', queue))
        sender.start()
        try:
            assert done.wait(2)
        finally:
            link.stop()
            sender.join(2)
        assert not sender.is_alive()
        assert calls == [['frame'], ['frame']]
        assert queue.ceka() == 0


def test_continuous_frames_are_sent_without_waiting_for_silence(agent, tmp_path):
    acknowledged = threading.Event()
    calls = []
    class Server:
        def push_passings(self, _, frames, casy=None):
            calls.extend(frames)
            acknowledged.set()
            return {'ok': True, 'stored': len(frames)}
    link = agent.StreamLink(Server(), {})
    reader_socket, decoder = socket.socketpair()
    with agent.FrameQueue(tmp_path / 'queue.db') as queue:
        sender = threading.Thread(target=link._send_loop, args=('d', queue))
        reader = threading.Thread(target=link._pump, args=(reader_socket, queue, b'', 10))
        sender.start(); reader.start()
        try:
            # Každý rámec musí odejít ještě před dalším (žádné 100ms ticho).
            for number in range(8):
                acknowledged.clear()
                raw = bytes([agent.STREAM_SOR, number, agent.STREAM_EOR])
                decoder.sendall(raw)
                assert acknowledged.wait(.09), 'RI čeká na ticho místo okamžitého odeslání'
                assert calls[-1] == base64.b64encode(raw).decode('ascii')
        finally:
            link.stop(); sender.join(2); reader.join(2)
            reader_socket.close(); decoder.close()
        assert not reader.is_alive() and not sender.is_alive()


def test_slow_http_does_not_stop_reading_or_watchdog(agent, tmp_path):
    started, release = threading.Event(), threading.Event()
    class Server:
        def push_passings(self, _, frames, casy=None):
            started.set()
            assert release.wait(2)
            return {'ok': True, 'stored': len(frames)}
    link = agent.StreamLink(Server(), {})
    reader_socket, decoder = socket.socketpair()
    decoder.settimeout(1)
    with agent.FrameQueue(tmp_path / 'queue.db') as queue:
        sender = threading.Thread(target=link._send_loop, args=('d', queue))
        reader = threading.Thread(target=link._pump, args=(reader_socket, queue, b'PING', .01))
        sender.start(); reader.start()
        try:
            decoder.sendall(bytes([agent.STREAM_SOR, 1, agent.STREAM_EOR]))
            assert started.wait(1)
            decoder.sendall(bytes([agent.STREAM_SOR, 2, agent.STREAM_EOR]))
            assert decoder.recv(4) == b'PING'
            assert queue.ceka() == 2  # Druhý rámec je trvale uložen, HTTP stále stojí.
        finally:
            link.stop(); release.set(); sender.join(2); reader.join(2)
            reader_socket.close(); decoder.close()
        assert queue.dalsi(50) == [base64.b64encode(bytes([agent.STREAM_SOR, 2, agent.STREAM_EOR])).decode('ascii')]


def test_davka_veze_cas_prijeti_krabickou(agent, tmp_path):
    """Bez času z krabičky se latence nedá rozložit na úseky.

    „Od smyčky do cloudu" je rozdíl razítka serveru a času **z dekodéru**,
    takže v něm leží doba doručení i chyba hodin dekodéru — a nepozná se,
    čeho je kolik (David 18. 9. 2026: „toto by chtělo optimalizovat, není to
    200 ms"). Čas přijetí si fronta vedla od začátku kvůli vypršení; teď ho
    i vydá.
    """
    fronta = agent.FrameQueue(tmp_path / "fronta.db")
    # Čerstvé časy: `dalsi()` maže rámce starší než `PRELIV_MAX_DNI`, takže
    # pevné razítko z roku 2023 by fronta správně zahodila dřív, než by ho
    # kdo přečetl.
    ted = time.time()
    fronta.pridej(["AAA", "BBB"], received=[ted, ted + 0.333])

    ramce = fronta.dalsi(10)
    casy = fronta.casy()

    assert ramce == ["AAA", "BBB"]
    assert casy == [int(ted * 1000), int((ted + 0.333) * 1000)], "milisekundy, ne sekundy"
    assert len(casy) == len(ramce), "čas musí sedět na rámec"


def test_bez_nabidnute_davky_zadne_casy(agent, tmp_path):
    fronta = agent.FrameQueue(tmp_path / "fronta.db")

    assert fronta.casy() == []
