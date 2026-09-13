"""Přeplněná fronta jde **na disk**, ne do koše.

Krabička protokolu P3 nerozumí a rozumět nemá — rámec je pro ni neprůhledný
kus bajtů. Uložit ho a poslat později ale umí i tak, a to je celé dohledání,
které zvládne **sama, bez obsluhy**: server dostane tytéž bajty, jen se
zpožděním, a rozumí jim on.

Do 7. 9. 2026 se při přeplněné frontě rámce zahazovaly. Zahození je od téhle
sady až druhá obrana — pro to, co se nevešlo ani na disk.
"""
from __future__ import annotations

import track_agent as ta


def _preliv(tmp_path):
    return ta.Preliv(tmp_path / "preliv.txt")


def test_prebytek_konci_na_disku_a_nic_se_neztrati(tmp_path):
    preliv = _preliv(tmp_path)

    ztraceno = preliv.pridej(["AAA", "BBB", "CCC"])

    assert ztraceno == 0, "disk je k dispozici — ztrácet není proč"
    assert preliv.ceka() == 3


def test_dosle_se_v_davkach_a_v_poradi(tmp_path):
    preliv = _preliv(tmp_path)
    preliv.pridej([f"R{i:03d}" for i in range(5)])

    prvni = preliv.dalsi(2)
    preliv.potvrd()
    druha = preliv.dalsi(2)
    preliv.potvrd()

    assert prvni == ["R000", "R001"]
    assert druha == ["R002", "R003"], "starší jdou první — jinak by se míchaly"
    assert preliv.ceka() == 1


def test_bez_potvrzeni_se_davka_nabidne_znovu(tmp_path):
    """Spadlá krabička pošle dávku dvakrát — a to je správně.

    Server průjezd pozná podle jeho čísla a duplikát zahodí, takže poslat
    dvakrát nestojí nic. Ztratit ho stojí výsledek.
    """
    preliv = _preliv(tmp_path)
    preliv.pridej(["AAA", "BBB"])

    prvni = preliv.dalsi(1)
    znovu = preliv.dalsi(1)

    assert prvni == znovu == ["AAA"]
    assert preliv.ceka() == 2, "dokud to server nevzal, čeká to dál"


def test_doslany_preliv_po_sobe_uklidi(tmp_path):
    """Karta v Raspberry je malá — doslaný soubor tam nemá co dělat."""
    preliv = _preliv(tmp_path)
    preliv.pridej(["AAA", "BBB"])

    preliv.dalsi(10)
    preliv.potvrd()

    assert preliv.ceka() == 0
    assert not (tmp_path / "preliv.txt").exists()


def test_po_restartu_krabicka_vi_kolik_ceka(tmp_path):
    """Výpadek sítě a restart jdou v praxi spolu — někdo přepojuje switch."""
    preliv = _preliv(tmp_path)
    preliv.pridej(["AAA", "BBB", "CCC"])

    po_restartu = ta.Preliv(tmp_path / "preliv.txt")

    assert po_restartu.ceka() == 3
    assert po_restartu.dalsi(1) == ["AAA"]


def test_plny_preliv_uz_je_ztrata_a_prizna_se(tmp_path, monkeypatch):
    """Nad stropem souboru ztráta **nastane** a musí se přiznat.

    Tohle je jediné místo, odkud se rámec dostane do počítadla zahozených:
    až když nepomůže ani disk. Dál už to spraví jen člověk tlačítkem
    „Dohledat průjezdy z dekodéru" — to jde od záložky dál, takže díru
    dotáhne celou.
    """
    monkeypatch.setattr(ta, "PRELIV_MAX_BAJTU", 4)
    preliv = _preliv(tmp_path)
    preliv.pridej(["AAAA"])            # 5 B s koncem řádku — ještě se vejde

    ztraceno = preliv.pridej(["BBBB", "CCCC"])

    assert ztraceno == 2
    assert preliv.ceka() == 1, "co je na disku, tam zůstává"


def test_nezapisovatelna_karta_neshodi_agenta(tmp_path):
    """Plná nebo jen pro čtení — agent musí běžet dál a přiznat ztrátu."""
    (tmp_path / "prekazka").write_text("nejsem složka", encoding="utf-8")
    preliv = ta.Preliv(tmp_path / "prekazka" / "preliv.txt")

    ztraceno = preliv.pridej(["AAA"])

    assert ztraceno == 1
    assert preliv.ceka() == 0


def test_displej_rozlisi_odlozene_od_ztraceneho():
    """Dvě různé zprávy, ne jedna.

    „Čeká na disku" se spraví samo, „zahozeno" chce člověka u obrazovky.
    Slít je do jednoho čísla by znamenalo posílat obsluhu běhat i za výpadek
    wifi, který krabička dořeší bez ní.
    """
    with ta._prujezdy_lock:
        ta._prujezdy["zahozeno"] = 0
        ta._prujezdy["preliv"] = 0

    ta._zaznamenat_preliv(800)

    stav = ta._prujezdy_stav()
    assert stav["preliv"] == 800
    assert stav["zahozeno"] == 0, "odložená zásilka není ztráta"


def test_pruh_prelivu_je_na_displeji_a_rika_ze_doleti_sam():
    assert 'id="preliv-pruh"' in ta._SCREEN
    assert "DOLETÍ SAMY" in ta._SCREEN
    # Oranžová, ne červená: nic se neztratilo. Barvu drží třída `preliv`
    # ve `_STYLE`, který se do stránky vkládá zvlášť.
    assert ".preliv {{" in ta._STYLE


# --- drátování: co dělá `_pump`, když server nebere ---------------------------


class _FalesnySocket:
    """Dekodér, který jednou vysype rámce a pak mlčí.

    Mlčení je `socket.timeout` — přesně to, co dělá skutečný socket, když
    z trati nic nejede. Bez něj by se `_pump` točil v prázdné smyčce.
    """

    def __init__(self, davky):
        self._davky = list(davky)
        self.odeslano = []

    def settimeout(self, _t):
        pass

    def recv(self, _kolik):
        if self._davky:
            return self._davky.pop(0)
        import socket
        raise socket.timeout()

    def sendall(self, data):
        self.odeslano.append(data)


class _NeberouciServer:
    """Server, který je nedostupný — POST skončí chybou sítě."""

    def __init__(self):
        self.pokusy = 0

    def push_passings(self, decoder_id, batch):
        self.pokusy += 1
        raise OSError("server nedostupný")


class _BerouciServer:
    def __init__(self):
        self.davky = []

    def push_passings(self, decoder_id, batch):
        self.davky.append(list(batch))
        return {"ok": True, "stored": len(batch)}


def _ramec(cislo: int) -> bytes:
    """Rámec SOR…EOR — obsah je krabičce jedno, P3 nezná."""
    return bytes([ta.STREAM_SOR]) + f"{cislo:04d}".encode("ascii") + bytes([ta.STREAM_EOR])


def _link(server, tmp_path):
    link = ta.StreamLink.__new__(ta.StreamLink)
    link.server = server
    link.config = {}
    link._stop = __import__("threading").Event()
    return link



def test_dosli_preliv_posle_a_potvrdi(tmp_path):
    """Až server bere, historie doletí — a soubor se uklidí."""
    server = _BerouciServer()
    link = _link(server, tmp_path)
    preliv = _preliv(tmp_path)
    preliv.pridej(["AAA", "BBB"])

    link._dosli_preliv("dek-1", preliv)

    assert server.davky == [["AAA", "BBB"]]
    assert preliv.ceka() == 0


def test_dosli_preliv_pri_nedostupnem_serveru_nic_nezahodi(tmp_path):
    server = _NeberouciServer()
    link = _link(server, tmp_path)
    preliv = _preliv(tmp_path)
    preliv.pridej(["AAA"])

    link._dosli_preliv("dek-1", preliv)

    assert preliv.ceka() == 1, "co server nevzal, zůstává na disku"


def test_displej_se_da_vykreslit():
    """**Hlídka na `{` versus `{{`.**

    `_SCREEN` je formátovací šablona, takže složená závorka v JavaScriptu
    se v ní musí zdvojit. Test hledající řetězec to nepozná — stránka se
    poskládá teprve `.format()` a do té doby chyba mlčí. Přesně na tohle
    se 7. 9. 2026 spálil pruh zahozených rámců.
    """
    ta._zaznamenat_preliv(0)

    stranka = ta._render_screen(None, {}).decode("utf-8")

    assert 'id="preliv-pruh"' in stranka
    assert 'id="zahozeno-pruh"' in stranka
