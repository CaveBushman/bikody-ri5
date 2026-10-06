#!/usr/bin/env python3
"""Agent u trati — drát mezi aplikací v cloudu a železem v klubové síti.

Dekodéry MyLaps i cílová kamera mají privátní adresy (`192.168.x.y`), na které
se server aplikace z datového centra nedostane. Tenhle program běží na počítači
u trati, **sám se hlásí serveru** a dělá to, oč si řekne: připojí se na
zadanou adresu, pošle bajty a vrátí, co přišlo zpátky.

Ven jde jen odchozí HTTPS, takže se na routeru pořadatele nic neotevírá.

Spuštění:

    python3 tools/track_agent.py

Adresa aplikace je předvyplněná (`DEFAULT_SERVER`) a token si program vyrobí
sám — ukáže ho na své stránce a obsluha ho opíše v aplikaci do Nastavení
aplikace → Přihlásit krabičku. Vlastní server a hotový token se dají předat
přepínači `--server` / `--token` nebo prostředím (`BIKODY_SERVER`,
`BIKODY_AGENT_TOKEN`).

Program je schválně **jen ze standardní knihovny**: na notebooku u trati se
nemá co instalovat a nemá co se rozbít. Neví nic o P3 ani o formátu startovky
— protokoly zůstávají na serveru, takže aktualizace aplikace neznamená
aktualizaci notebooků.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.client
import ipaddress
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

#: Verze agenta. Zvedá se **při každé změně souboru, kterou má krabička
#: u trati dostat** — cloud nabízí aktualizaci podle porovnání verzí
#: (`bmx_racing/views/track_agent.py`), takže beze zvednutí by nasazené
#: krabičky zůstaly na starém souboru. 1.14: displej mluví anglicky
#: (rozhodnutí 21. 9. 2026). 1.15: opravy z auditu 23. 9. 2026 — escapování
#: stránek, token celý jen pro tento počítač, hlídání pracovních vláken,
#: atomický zápis nastavení, žádné příkazy na loopback, strop těla požadavku.
#: 1.17: méně dotazů na server — přestávka mezi dotazy na příkazy v klidu,
#: pomalejší opakování odložené dávky, delší trpělivost s pomalým serverem
#: (`docs/SIT-A-ZATIZENI.md` v repozitáři Ri5).
VERSION = "1.17"

#: Kód, kterým server říká, že dávka **nikam nepatří** a opakování ji
#: nespraví (`bmx/views/track_agent.py`). Do agenta 1.12 se trvalá chyba
#: nedala odlišit od dočasné a dávka se zkoušela dokola: fronta se dva dny
#: plnila a pak se přiznala jako skutečná ztráta, přestože ty rámce do
#: žádného závodu nepatřily.
#:
#: **Neznámý kód se bere jako dočasný.** Zahodit cizí průjezdy je horší než
#: poslat je dvakrát — server duplicitu srazí otiskem.
KOD_ZAHODIT = "unknown_decoder"

#: Tvar rozhovoru se serverem, který tenhle agent umí. **2 = rozumím poli
#: `code`**, tedy umím odlišit trvale odmítnutou dávku od dočasně odložené.
#: Server odpovídá rozsahem, který zvládne; když je náš vyšší, řekne to
#: rovnou (`426`) místo aby se to projevilo až na trati.
PROTOKOL = 2

#: Kanonická adresa rozhraní agenta. Bez disciplíny v cestě — obsluhuje
#: krabičky všech disciplín a `bmx` v ní byla nepřesnost z doby, kdy nic
#: jiného nebylo.
CESTA = "/api/agent"
#: Adresa, na kterou chodily krabičky do 18. 9. 2026. Server ji drží
#: **navždy**; tady je proto, aby nová krabička fungovala i proti staršímu
#: cloudu — nasazuje se sice cloud první, ale krabička se nesmí rozbít ani
#: tehdy, když se cloud vrátí zpátky.
CESTA_STARA = "/bmx/api/agent"

#: Kam se agent hlásí, když mu nikdo neřekl jinak. Aplikace běží na jednom
#: místě, takže adresu nemá co obsluha u trati vypisovat — krabička po zapnutí
#: rovnou ukáže token a jediné, co zbývá, je opsat ho v aplikaci. Vlastní
#: server se pořád dá zadat na stránce krabičky, přepínačem nebo prostředím.
DEFAULT_SERVER = "https://bikody.com"

#: Server drží dotaz otevřený, dokud nemá co poslat. Čtecí timeout musí být
#: delší, jinak by agent spojení trhal těsně před odpovědí.
READ_TIMEOUT = 40.0

#: Jak dlouho se čeká na potvrzení dávky průjezdů. Do 1.16 15 s — jenže
#: o víkendu 3.–4. 10. 2026 server potvrzoval i za 17 s. Krabička to vzdala,
#: poslala **tutéž dávku znovu**, a server pak dělal dvakrát stejnou práci
#: přesně ve chvíli, kdy nestíhal. Spadlé spojení se pozná hned (RST,
#: `_ZASTARALE`), na tenhle limit čeká jen spojení, které mlčí.
PRUJEZDY_TIMEOUT_S = 20.0

#: Po výpadku sítě se zkouší dál, jen pomaleji — u trati se běžně přepojuje
#: kabel nebo přepíná wifi a agent to má přežít bez zásahu obsluhy.
RECONNECT_MIN = 1.0
RECONNECT_MAX = 15.0

#: Jak často se krabička ptá, jestli už ji někdo v aplikaci schválil. Obsluha
#: mezitím opisuje token z displeje, takže ani rychleji, ani líně.
APPROVAL_POLL_SECONDS = 5.0

#: **Přestávka mezi dotazy na příkazy** (agent 1.17, výkonnostní průchod po
#: BMX víkendu 3.–4. 10. 2026). Server drží dotaz nanejvýš vteřinu
#: (`LONG_POLL_SECONDS` v cloudu) a krabička se do 1.16 ptala hned znovu:
#: 27 897 dotazů za dva dny, každý drží jednoho workera serveru 1,1 s —
#: přitom příkaz přijde v průměru jednou za ~16 s.
#:
#: Pravidla (čísla viz `docs/SIT-A-ZATIZENI.md` v repozitáři Ri5):
#:
#: * přišel příkaz před méně než `PRIKAZY_HORKO_S` → ptát se **hned**.
#:   Pokrývá dávky příkazů (kontrolky dekodérů jdou jedna po druhé)
#:   i záchranné stahování po 1,5 s, když push nedoručuje;
#: * jinak `PRIKAZY_PAUZA_S` — o tolik se nejvýš prodlouží doručení
#:   prvního příkazu. Nejtěsnější rozpočet má na serveru `tcp_exchange`
#:   (1,5 s + 2 s), vteřina se do něj vejde i s pomalým dekodérem;
#: * krabička bez proudu průjezdů a bez příkazu přes `PRIKAZY_KLID_PO_S`
#:   (nikdo neměří ani nic nenastavuje) → `PRIKAZY_PAUZA_KLID_S`.
#:
#: Průjezdů se to netýká — ty jdou vlastním vláknem hned (`StreamLink`).
PRIKAZY_HORKO_S = 3.0
PRIKAZY_PAUZA_S = 1.0
PRIKAZY_PAUZA_KLID_S = 2.0
PRIKAZY_KLID_PO_S = 300.0


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Server:
    """Dotazy na aplikaci. Token se posílá v hlavičce, ne v adrese."""

    #: Pády, po kterých má smysl zopakovat požadavek hned na novém spojení:
    #: server (nebo NAT po LTE) držené spojení pustil a poznalo se to až teď.
    _ZASTARALE = (http.client.RemoteDisconnected, http.client.BadStatusLine,
                  ConnectionResetError, BrokenPipeError)

    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token
        # Držená spojení **na vlákno**: proud průjezdů posílá dávky z vlastního
        # vlákna, hlavní smyčka se ptá na příkazy — `http.client` nesnese dva
        # požadavky na jednom spojení naráz.
        self._mistni = threading.local()
        #: Zjistilo se, že tenhle cloud kanonickou adresu nezná? Sdílí to
        #: všechna vlákna schválně: je to vlastnost serveru, ne spojení.
        self._stara_cesta = False

    def _spojeni(self, casti, timeout: float):
        cache = getattr(self._mistni, "spojeni", None)
        if cache is None:
            cache = self._mistni.spojeni = {}
        klic = (casti.scheme, casti.netloc)
        spojeni = cache.get(klic)
        if spojeni is None:
            trida = (http.client.HTTPSConnection if casti.scheme == "https"
                     else http.client.HTTPConnection)
            spojeni = trida(casti.hostname, casti.port, timeout=timeout)
            cache[klic] = spojeni
        else:
            spojeni.timeout = timeout
            if spojeni.sock is not None:
                spojeni.sock.settimeout(timeout)
        return klic, spojeni

    def _zahod(self, klic) -> None:
        cache = getattr(self._mistni, "spojeni", None) or {}
        spojeni = cache.pop(klic, None)
        if spojeni is not None:
            spojeni.close()

    def _request(self, path: str, payload: dict | None = None, *, timeout: float) -> dict:
        """Požadavek na kanonickou adresu, se záchytem na tu starou.

        Cesty se píšou bez předpony (`"/hello/"`); tahle metoda k nim
        doplní `CESTA`, a když server odpoví `404`, zopakuje to na
        `CESTA_STARA` a **zapamatuje si to** — starý cloud se z ničeho
        nestane novým a ptát se ho na kanonickou adresu při každém dotazu
        by u trati stálo jednu zbytečnou cestu tam a zpět za vteřinu.
        """
        if self._stara_cesta:
            return self._odesli(CESTA_STARA + path, payload, timeout=timeout)
        try:
            return self._odesli(CESTA + path, payload, timeout=timeout)
        except urllib.error.HTTPError as chyba:
            if chyba.code != 404:
                raise
        log(f"Server nezná {CESTA}{path} — jedu po starém {CESTA_STARA}")
        self._stara_cesta = True
        return self._odesli(CESTA_STARA + path, payload, timeout=timeout)

    def _odesli(self, path: str, payload: dict | None = None, *, timeout: float) -> dict:
        """Jeden požadavek po **drženém** spojení.

        Do verze 1.7 šel každý požadavek přes `urllib.request.urlopen`, tedy
        nové TCP + TLS spojení: po LTE u trati 200–400 ms **na každou dávku
        průjezdů** — víc než celá práce serveru. Držené spojení pošle průjezd
        v jedné cestě tam a zpátky (David 12. 9. 2026: „potřebuji signál
        z dekodéru on-line, ne se zpožděním").

        Odpověď mimo 2xx se hlásí jako `urllib.error.HTTPError` stejně jako
        dřív, aby volající (403 = čeká na schválení) nemuseli nic měnit.
        """
        url = f"{self.base}{path}"
        casti = urllib.parse.urlsplit(url)
        cesta = casti.path + (f"?{casti.query}" if casti.query else "")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        hlavicky = {"Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json"}
        for pokus in (1, 2):
            klic, spojeni = self._spojeni(casti, timeout)
            try:
                spojeni.request("POST" if data else "GET", cesta, body=data, headers=hlavicky)
                if spojeni.sock is not None:
                    try:
                        spojeni.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except (OSError, AttributeError):
                        pass
                odpoved = spojeni.getresponse()
                telo = odpoved.read()
                if odpoved.will_close:
                    self._zahod(klic)
                if odpoved.status >= 400:
                    raise urllib.error.HTTPError(url, odpoved.status, odpoved.reason,
                                                 odpoved.headers, None)
                return json.loads(telo or b"{}")
            except self._ZASTARALE as exc:
                self._zahod(klic)
                if pokus == 2:
                    raise OSError(f"spojení na server padlo: {exc}") from exc
            except http.client.HTTPException as exc:
                self._zahod(klic)
                raise OSError(f"HTTP klient: {exc}") from exc
            except OSError:
                self._zahod(klic)
                raise
        raise OSError("the connection to the server could not be restored")  # pragma: no cover

    def hello(self) -> dict:
        # `dropped_frames` říká serveru, o kolik rámců krabička přišla —
        # dohledat je umí jen on (protokolu rozumí a dosáhne na dekodér přes
        # `tcp_exchange`), a spouští to člověk. Mlčky zahozený rámec je tichá
        # ztráta výsledku (7. 9. 2026).
        with _prujezdy_lock:
            zahozeno = int(_prujezdy["zahozeno"])
        return self._request(
            "/hello/",
            {"hostname": socket.gethostname(), "version": VERSION,
             "protocol": PROTOKOL, "dropped_frames": zahozeno},
            timeout=15.0,
        )

    def commands(self) -> list[dict]:
        answer = self.poll()
        return answer.get("commands") or []

    def poll(self) -> dict:
        """Dlouhý dotaz: příkazy k vyřízení + konfigurace proudu průjezdů.

        **Veze s sebou stav push cesty.** Zpráva o tom, že proud nedoručuje,
        musí dojít právě tehdy, když nedoručuje — takže nesmí jet dávkou
        průjezdů. Dlouhý dotaz jede bez ohledu na provoz (od 1.17 v klidu
        jednou za ~2–3 s, viz `PRIKAZY_PAUZA_S`), takže je to jediné místo,
        kde se cloud stav dozví vždycky.

        Starší server přijímá jen GET; když odmítne metodu, zeptáme se
        postaru a stav prostě nepošleme (krabička se kvůli hlášení
        neodpojuje).
        """
        stav = {"streams": list(_STAV_PROUDU.values())}
        try:
            return self._request("/commands/", stav, timeout=READ_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code != 405:
                raise
            return self._request("/commands/", timeout=READ_TIMEOUT)

    def push_passings(self, decoder_id: str, frames: list[str],
                      casy: list[int] | None = None) -> dict:
        """Pošle rámce průjezdů hned, jak je dekodér vydal (base64).

        `casy` jsou unixové časy v **milisekundách**, kdy krabička ten rámec
        převzala od dekodéru — jeden na rámec, ve stejném pořadí. Server si
        tím rozdělí „od smyčky do cloudu" na úsek dekodér→krabička a
        krabička→server; bez nich je v jednom čísle smíchaná doba doručení
        s chybou hodin dekodéru a nedá se optimalizovat.

        Pole je **nepovinné**: starší server ho ignoruje a novější server si
        se starou krabičkou poradí, jen rozklad neukáže.
        """
        telo = {"decoder": decoder_id, "frames": frames, "receipt": True}
        if casy and len(casy) == len(frames):
            telo["frame_times"] = casy
        return self._request("/passings/", telo, timeout=PRUJEZDY_TIMEOUT_S)

    def result(self, command_id: str, ok: bool, data: dict | None = None, error: str = "") -> None:
        self._request(
            "/result/",
            {"id": command_id, "ok": ok, "data": data or {}, "error": error},
            timeout=15.0,
        )

    # Fotofiniš kamera (Davidovo zadání 3. 10. 2026): nastavení je hardware
    # organizace a drží ho server; krabička ho jen čte, zapisuje a umí
    # požádat o znovuposlání poslední startovky.
    def camera(self) -> dict:
        return self._request("/camera/", timeout=10.0)

    def camera_save(self, host: str, port: int) -> dict:
        return self._request("/camera/", {"host": host, "port": port}, timeout=10.0)

    def camera_resend(self) -> dict:
        return self._request("/camera/resend/", {"resend": True}, timeout=30.0)

    def download_agent(self) -> bytes:
        """Stáhne novou verzi ze stejného serveru jako řídicí API."""
        request = urllib.request.Request(
            f"{self.base}/bmx/api/agent/download/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=30.0) as response:
            return response.read()


# --- spojení na železo -----------------------------------------------------
#
# Dekodér MyLaps pustí najednou **čtyři spojení** a víc jich nepustí ani na
# chvíli. Kdyby agent otevíral nové spojení na každý dotaz, vyčerpal by je sám
# sebou: odběr průjezdů se ptá po vteřinách a kontrolka v liště taky. Spojení
# se proto drží otevřené a používá se znovu — na dekodér se pak jde jedním
# slotem místo nekonečné řady.

_pool: dict[tuple[str, int], socket.socket] = {}

#: Výslovná výjimka pro simulátor dekodéru na notebooku (viz
#: `_validated_target`). Na krabičce se nenastavuje nikdy.
POVOLIT_LOOPBACK = os.environ.get("BIKODY_AGENT_ALLOW_LOOPBACK") == "1"


def _validated_target(host: str, port: int) -> tuple[str, int]:
    """Povolí jen IP adresu v neveřejné síti a platný TCP port.

    Token odemyká příkazy ze serveru do klubové sítě. Veřejné adresy,
    multicast a link-local metadata proto nejsou legitimní cíl decoderu ani
    kamery.

    **Loopback taky ne** (audit 23. 9. 2026): na 127.0.0.1 poslouchá stránka
    krabičky a její zápisové cesty věří právě loopbacku — příkaz ze serveru
    by přes `tcp_send` přepsal nastavení (adresu serveru, token) zevnitř.
    Dekodér ani kamera na krabičce neběží. Simulátor dekodéru na notebooku
    se pustí jen s výslovným `BIKODY_AGENT_ALLOW_LOOPBACK=1`.
    """
    try:
        address = ipaddress.ip_address((host or "").strip())
    except ValueError as exc:
        raise ValueError("The target must be a numeric IP address on the local network.") from exc
    if address.version != 4 or address.is_multicast or address.is_unspecified:
        raise ValueError("That target IP address is not allowed.")
    if address.is_link_local:
        raise ValueError("A link-local address is allowed for neither the decoder nor the camera.")
    private_ranges = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if address.is_loopback and not POVOLIT_LOOPBACK:
        raise ValueError("The box does not connect to itself (loopback).")
    if not (address.is_loopback or any(address in network for network in private_ranges)):
        raise ValueError("The box may connect only to the local private network.")
    if not 1 <= int(port) <= 65535:
        raise ValueError("The port must be in the range 1–65535.")
    return str(address), int(port)

#: Posledních pár spojení na železo. Na displeji krabičky u trati je to jediné,
#: podle čeho obsluha pozná, jestli se dekodéry a kamera ozývají — do aplikace
#: se přes rameno nekouká.
_recent: list[dict] = []
_RECENT_MAX = 6


def _remember(host: str, port: int, ok: bool, detail: str = "") -> None:
    entry = {
        "cil": f"{host}:{port}",
        "ok": ok,
        "detail": detail,
        "cas": time.strftime("%H:%M:%S"),
    }
    _recent.insert(0, entry)
    del _recent[_RECENT_MAX:]


def _drop(key: tuple[str, int]) -> None:
    sock = _pool.pop(key, None)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def _connection(host: str, port: int, timeout: float) -> socket.socket:
    """Držené spojení na tuhle adresu, nebo nové."""
    key = (host, int(port))
    sock = _pool.get(key)
    if sock is not None:
        return sock
    sock = socket.create_connection(key, timeout=timeout)
    _pool[key] = sock
    return sock


def _drain(sock: socket.socket) -> None:
    """Zahodí, co v bufferu zbylo z minula.

    Dekodér posílá sám od sebe status každých pár sekund. Kdyby to zůstalo
    ve frontě, přimíchalo by se to k odpovědi na další dotaz — server sice
    cizí rámce přeskakuje, ale zbytečně by se přenášely.
    """
    sock.setblocking(False)
    try:
        while True:
            if not sock.recv(8192):
                break
    except (BlockingIOError, OSError):
        pass
    finally:
        sock.setblocking(True)


# --- co agent umí ----------------------------------------------------------


def tcp_probe(args: dict) -> dict:
    """Poslouchá na té adrese vůbec někdo?

    Držené spojení je samo o sobě odpověď — nové se kvůli kontrolce
    neotevírá, aby se dekodéru neujídaly sloty.
    """
    timeout = float(args.get("timeout") or 2.0)
    host, port = _validated_target(args["host"], int(args["port"]))
    if (host, port) in _pool:
        return {}
    _connection(host, port, timeout)
    return {}


# --- fotofiniš kamera --------------------------------------------------------
#
# Startovky přijímá **software OPTIc3** na počítači u kamery (XML socket,
# výchozí port 4532), ne samotná kamera. Krabička jen hlídá, jestli na té
# adrese někdo poslouchá: spojení otevře a **hned zavře** — nedrží ho jako
# u dekodéru (`tcp_probe`), protože OPTIc3 čeká jednu zprávu na spojení.

#: Co krabička o kameře ví — nastavení ze serveru a poslední zkouška.
_kamera: dict = {"host": "", "port": 0, "export": False, "last": None,
                 "ok": None, "chyba": "", "kdy": "", "zprava": ""}
_kamera_lock = threading.Lock()
KAMERA_KONTROLA_S = 60.0


def zkus_kameru(host: str, port: int) -> tuple[bool, str]:
    """Poslouchá OPTIc3 na té adrese? Otevře a zavře spojení, nic nepošle."""
    try:
        host, port = _validated_target(host, int(port))
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except (OSError, ValueError) as exc:
        return False, str(exc) or exc.__class__.__name__
    return True, ""


def _uloz_stav_kamery(info: dict | None = None, *, ok=None, chyba: str = "", zprava=None) -> None:
    with _kamera_lock:
        if info is not None:
            _kamera.update({k: info.get(k) for k in ("host", "port", "export", "last") if k in info})
        if ok is not None:
            _kamera["ok"] = ok
            _kamera["chyba"] = chyba
            _kamera["kdy"] = time.strftime("%H:%M:%S")
        if zprava is not None:
            _kamera["zprava"] = zprava


def stav_kamery() -> dict:
    with _kamera_lock:
        return dict(_kamera)


def obnov_kameru(worker) -> None:
    """Načte nastavení ze serveru a vyzkouší adresu. Výpadek nevadí."""
    server = getattr(worker, "server", None)
    if server is not None and getattr(worker, "connected", False):
        try:
            _uloz_stav_kamery(server.camera())
        except Exception as exc:  # noqa: BLE001 — starý server nebo výpadek sítě
            log(f"Kamera: nastavení ze serveru nepřišlo ({exc})")
    stav = stav_kamery()
    if stav.get("host"):
        ok, chyba = zkus_kameru(stav["host"], stav.get("port") or 4532)
        _uloz_stav_kamery(ok=ok, chyba=chyba)


def _hlidej_kameru(state: dict, *, konec=None) -> None:
    """Jednou za minutu: nastavení kamery ze serveru a zkouška spojení."""
    while not (konec and konec.is_set()):
        try:
            obnov_kameru(state.get("worker"))
        except Exception as exc:  # noqa: BLE001 — hlídač nesmí umřít
            log(f"Kamera: kontrola selhala ({exc})")
        if konec is not None:
            konec.wait(KAMERA_KONTROLA_S)
        else:
            time.sleep(KAMERA_KONTROLA_S)


def tcp_send(args: dict) -> dict:
    """Pošle bajty a zavře — startovka do kamery nic nevrací.

    Kamera spojení po každé zprávě zavírá sama a slotů se u ní netahá, takže
    se tu nic nedrží.
    """
    timeout = float(args.get("timeout") or 2.0)
    payload = base64.b64decode(args.get("data") or "")
    host, port = _validated_target(args["host"], int(args["port"]))
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
    return {}


def tcp_exchange(args: dict) -> dict:
    """Pošle bajty a vrátí, co přišlo zpátky.

    Čte, dokud spojení `quiet` sekund mlčí nebo dokud nevyprší `timeout`.
    Kde končí odpověď, ví server — agent rámce nerozebírá.

    Jede po **drženém spojení**; když se pod rukama zavře (dekodér se
    restartoval, wifi vypadla), zkusí se jednou znovu s novým.
    """
    timeout = float(args.get("timeout") or 3.0)
    quiet = float(args.get("quiet") or 0.3)
    payload = base64.b64decode(args.get("data") or "")
    host, port = _validated_target(args["host"], int(args["port"]))

    for attempt in (1, 2):
        sock = _connection(host, port, timeout)
        try:
            _drain(sock)
            sock.sendall(payload)
            deadline = time.monotonic() + timeout
            chunks: list[bytes] = []
            while time.monotonic() < deadline:
                sock.settimeout(min(quiet, max(0.05, deadline - time.monotonic())))
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    if chunks:
                        break      # chvíli ticho a něco už máme — konec odpovědi
                    continue
                if not chunk:
                    # Protějšek zavřel. Co dorazilo, platí — spojení se jen
                    # zahodí, aby se příště navázalo nové.
                    _drop((host, port))
                    if chunks:
                        break
                    raise ConnectionError("The peer closed the connection.")
                chunks.append(chunk)
            return {"data": base64.b64encode(b"".join(chunks)).decode("ascii")}
        except (OSError, TimeoutError):
            _drop((host, port))
            if attempt == 2:
                raise


def broadcast_targets() -> list[str]:
    """Kam poslat broadcast — **na každé rozhraní, ne jen do výchozí trasy**.

    `255.255.255.255` je *omezený* broadcast: jádro ho pošle jedním
    rozhraním, které vybere podle směrovací tabulky — tedy tím, kudy vede
    výchozí trasa. Krabička u trati bývá zapojená dvěma dráty: LTE nebo wifi
    do internetu (a tam vede výchozí trasa) a ethernet do sítě dekodérů.
    Dotaz tak odešel do internetu, dekodéry ho nikdy neviděly a hledání
    vracelo prázdno, i když krabička i dekodér šlapaly (Davidův nález
    20. 8. 2026: „krabička u trati nevyhledává IP adresy a MAC adresy
    dekodérů, ač to má umět").

    Řešení je poslat dotaz na **směrovaný broadcast každé podsítě**, kterou
    krabička vidí (192.168.1.0/24 → 192.168.1.255): takový paket už má
    adresáta v konkrétní síti a jádro ho pošle tím správným drátem.
    Omezený broadcast zůstává v seznamu jako záloha pro případ, že se
    rozhraní vypsat nedají.

    Adresy se čtou z `ip -4 -o addr` (Linux — Raspberry Pi u trati) nebo
    `ifconfig` (macOS); bez nich zbývá omezený broadcast. Žádná další
    závislost: krabička jede na čistém Pythonu ze standardní knihovny.
    """
    targets: list[str] = []
    for network, prefix in _local_networks():
        directed = _broadcast_address(network, prefix)
        if directed and directed not in targets:
            targets.append(directed)
    targets.append("255.255.255.255")
    return targets


def _local_networks() -> list[tuple[str, int]]:
    """Adresy a délky prefixů IPv4 rozhraní — kromě loopbacku."""
    networks: list[tuple[str, int]] = []
    for command in (["ip", "-4", "-o", "addr"], ["ifconfig"]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0 or not result.stdout:
            continue
        networks = _parse_networks(result.stdout)
        if networks:
            break
    return networks


def _parse_networks(text: str) -> list[tuple[str, int]]:
    """Z výpisu `ip addr` nebo `ifconfig` vytáhne (adresa, prefix).

    Hledá se `a.b.c.d/len` (Linux) a `inet a.b.c.d netmask 0xffffff00`
    (macOS). Loopback se vynechává — broadcast do sebe nikoho nenajde.
    """
    import re

    found: list[tuple[str, int]] = []

    def add(address: str, prefix: int) -> None:
        if address.startswith("127.") or not 1 <= prefix <= 31:
            return
        if (address, prefix) not in found:
            found.append((address, prefix))

    for match in re.finditer(r"\b(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})\b", text):
        add(match.group(1), int(match.group(2)))
    for match in re.finditer(
        r"inet (\d{1,3}(?:\.\d{1,3}){3}) netmask (0x[0-9a-fA-F]{8})", text
    ):
        add(match.group(1), bin(int(match.group(2), 16)).count("1"))
    return found


def _broadcast_address(address: str, prefix: int) -> str:
    """Směrovaný broadcast podsítě: 192.168.1.7/24 → 192.168.1.255."""
    try:
        octets = [int(part) for part in address.split(".")]
    except ValueError:
        return ""
    if len(octets) != 4 or any(not 0 <= part <= 255 for part in octets):
        return ""
    value = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    host_bits = 32 - prefix
    value |= (1 << host_bits) - 1
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def udp_discover(args: dict) -> dict:
    """Broadcast do místní sítě a sběr odpovědí — hledání dekodérů.

    Jediná věc, kterou agent dělá po UDP. Důvod je ten samý, proč vůbec
    existuje: broadcast musí vyjít **ze sítě, kde dekodéry stojí**. Server
    v datovém centru ho pošle nanejvýš svým sousedům, takže „Dohledat MAC
    adresy" tam vracelo prázdno, i když všechno ostatní přes agenta prošlo
    (nález 19. 8. 2026).

    Posílá se na **každé rozhraní** krabičky (`broadcast_targets`), ne jen
    do výchozí trasy: krabička u trati je typicky dvojdomá — internet jedním
    drátem, dekodéry druhým — a jediný `255.255.255.255` odešel tím prvním
    (Davidův nález 20. 8. 2026). Nedosažitelná cílová adresa hledání
    nezastaví; ostatní rozhraní se zkouší dál.

    Agent protokolu nerozumí — pošle hotové bajty a vrátí, co se ozvalo,
    včetně adresy odesílatele. Rozebrat rámce je věc serveru.

    **Naslouchá se dřív, než se pošle:** dekodér odpovídá okamžitě a odpověď
    na nenavázaný port by spadla do prázdna.
    """
    timeout = float(args.get("timeout") or 3.0)
    payload = base64.b64decode(args.get("data") or "")
    request_port = int(args.get("request_port") or 5403)
    reply_port = int(args.get("reply_port") or 5303)

    replies: list[dict] = []
    seen: set[str] = set()
    targets = broadcast_targets()
    sent: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", reply_port))
        sock.settimeout(timeout)
        for target in targets:
            try:
                sock.sendto(payload, (target, request_port))
            except OSError as exc:
                # Jedno rozhraní bez trasy (odpojený kabel, vypnutá wifi)
                # nesmí zastavit hledání na ostatních.
                log(f"Broadcast na {target} neprošel: {exc}")
                continue
            sent.append(target)
        if not sent:
            return {"replies": [], "error": "The broadcast got through on no interface."}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(deadline - time.monotonic(), 0.05))
            try:
                data, sender = sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break
            host = sender[0]
            if host in seen:
                continue
            seen.add(host)
            replies.append(
                {
                    "host": host,
                    "data": base64.b64encode(data).decode("ascii"),
                    "mac_address": _neighbor_mac(host),
                }
            )
    except OSError as exc:
        # Port 5303 může držet jiný program (MyLaps Toolkit, druhá instance).
        # Hledání se pak nekoná, ale agent kvůli tomu nepadá.
        return {"replies": replies, "error": str(exc)}
    finally:
        sock.close()
    # `sent` říká, kudy se hledalo — bez toho je „nikdo se neozval"
    # nerozeznatelné od „poslalo se to špatným drátem".
    return {"replies": replies, "sent_to": sent}


def _neighbor_mac(host: str) -> str:
    """Plná L2 MAC z ARP/neighbor tabulky, pokud ji operační systém zná."""
    import re

    commands = (["ip", "neigh", "show", host], ["arp", "-n", host])
    pattern = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            continue
        match = pattern.search(result.stdout or "")
        if match:
            return match.group(0).upper()
    return ""


def clock_info(_args: dict) -> dict:
    """Hodiny počítače u trati pro porovnání se serverem a decoderem."""
    import datetime as dt

    now = time.time()
    return {
        "unix_ms": round(now * 1000),
        "utc": dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat(),
        "local": dt.datetime.fromtimestamp(now).astimezone().isoformat(),
    }


def network_info(_args: dict) -> dict:
    """Nedestruktivní diagnostika rozhraní, broadcastů a UDP odpovědního portu."""
    networks = _local_networks()
    port_ok = False
    port_error = ""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 5303))
        port_ok = True
    except OSError as exc:
        port_error = str(exc)
    finally:
        sock.close()
    return {
        "interfaces": [
            {"address": address, "prefix": prefix}
            for address, prefix in networks
        ],
        "broadcasts": broadcast_targets(),
        "reply_port_ok": port_ok,
        "reply_port_error": port_error,
        **clock_info({}),
    }


ACTIONS = {
    "tcp_probe": tcp_probe,
    "tcp_send": tcp_send,
    "tcp_exchange": tcp_exchange,
    "udp_discover": udp_discover,
    "clock_info": clock_info,
    "network_info": network_info,
}


def run_command(server: Server, command: dict) -> None:
    action = ACTIONS.get(command.get("action") or "")
    if action is None:
        server.result(command.get("id"), False, error=f"Neznámý příkaz {command.get('action')!r}.")
        return
    args = command.get("args") or {}
    if not isinstance(args, dict):
        args = {}      # vadný tvar: akce pak selže `KeyError`em níž a ohlásí se
    host = str(args.get("host", ""))
    try:
        port = int(args.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    try:
        data = action(args)
    except (OSError, TimeoutError, ValueError) as exc:
        # Nedostupné železo je běžný stav, ne pád agenta: server chybu ukáže
        # obsluze u rampy stejně, jako by se připojoval sám.
        _remember(host, port, False, str(exc))
        server.result(command.get("id"), False, error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        # **Vadný příkaz nesmí zabít vlákno agenta** (audit 23. 9. 2026).
        # Příkaz bez `host`/`port` hodil `KeyError`, ten prošel až nahoru
        # a vlákno `agent` umřelo — proces s webem žil dál, systemd neměl co
        # restartovat a displej svítil OK nad krabičkou, která nic nedělá.
        detail = f"invalid command ({type(exc).__name__}: {exc})"
        log(f"Příkaz {command.get('action')!r} selhal: {detail}")
        _remember(host, port, False, detail)
        server.result(command.get("id"), False, error=detail)
        return
    _remember(host, port, True)
    server.result(command.get("id"), True, data=data)


# --- proud průjezdů --------------------------------------------------------
#
# Dekodér posílá průjezdy sám (v příručce P3 značka „A" — send autonomously),
# takže na ně netřeba čekat dotazem. Krabička drží spojení otevřené — stejně,
# jako ho v přímém režimu drží server — a každý rámec hned POSTne do aplikace.
# Do 19. 8. 2026 se průjezdy jen stahovaly na dotaz serveru po 1,5 s a každý
# dotaz stál dvě cesty přes internet; od smyčky k obrazovce to dělalo 2–4 s.
#
# Krabička protokolu P3 **nerozumí**: otevírací rámce (watchdog, resend od
# záložky) jí předchystá server v konfiguraci proudu a ona jen řeže příchozí
# bajty na rámce podle SOR/EOR — uvnitř rámce jsou tyhle bajty escapované,
# takže se s obsahem nespletou.

#: Stav proudů, jak ho krabička hlásí serveru. Plní ho `_sync_streams`
#: při každém ohlášení — jeden zápis na smyčku, žádné zamykání navíc.
_STAV_PROUDU: dict = {}

STREAM_SOR = 0x8E
STREAM_EOR = 0x8F

#: Odesílatel bere dostupné rámce ihned, bez čekání na další rámec.
STREAM_MAX_FRAMES = 50
STREAM_RETRY_INITIAL_SECONDS = 0.05
STREAM_RETRY_SECONDS = 1.0
#: Strop opakování, když server dávku **odložil** (`ok: false` s kódem jiným
#: než `KOD_ZAHODIT`, typicky „krabička nemá přiřazený závod"). Server
#: žije a odpověděl — dokud obsluha závod nepřiřadí, odpoví stejně. Do 1.16
#: se ptal každou vteřinu za každou smyčku, klidně celé hodiny; pět vteřin
#: po přiřazení závodu nikdo nepozná.
STREAM_ODLOZENO_SECONDS = 5.0
#: Jak dlouho odesílatel bez práce spí. Rámec ho budí hned (`_ready`), tohle
#: je jen pojistka; do 1.16 vteřina = každou vteřinu tři dotazy do SQLite
#: za smyčku úplně zbytečně.
STREAM_IDLE_SECONDS = 5.0
#: Takt čtení z dekodéru. Data `recv` vrátí hned, timeout jen určuje, jak
#: často se smyčka podívá na zastavení a servis watchdogu (10 s). Do 1.16
#: 0,1 s = deset probuzení za vteřinu na smyčku bez jediného bajtu.
STREAM_RECV_TIMEOUT_SECONDS = 0.5

#: Strop bufferu proudu — rámec má desítky bajtů; víc bez konce rámce je
#: rozsypaný proud, ne data (stejná pojistka jako na serveru).
STREAM_BUFFER_MAX = 256 * 1024

#: Strop souboru přelivu. Rámec zabere ve base64 pár desítek bajtů, takže
#: osm megabajtů je řádově sto tisíc průjezdů — víc, než kolik jich za den
#: projede celý závod. Karta v Raspberry je malá a nesmí se zaplnit.
#: Jak dlouho se čeká, než se rámec podaří odevzdat. Co neprojde do dvou
#: dnů, se maže (Davidovo zadání 12. 9. 2026): průjezd starší než víkend do
#: žádného otevřeného závodu nepatří a karta ho nemá vozit do příští sezóny.
PRELIV_MAX_DNI = 2

PRELIV_MAX_BAJTU = 8 * 1024 * 1024

#: Kolik rámců z přelivu se dotáhne na jeden POST. Živá dávka jde první
#: a tahle za ní, aby doslání historie nezdržovalo aktuální čas na desce.
PRELIV_DAVKA = 200

#: Poslední průjezdy, které server vzal — pro červenou kontrolku na displeji
#: (Davidovo zadání 20. 8. 2026: „nešlo by, aby i krabička měla červenou
#: kontrolku, když přijme průjezd?"). Krabička protokolu nerozumí, takže se
#: nepočítají rámce (dekodér posílá i status), ale `stored` z odpovědi
#: serveru — kontrolka tak svítí jen za průjezdy, které opravdu dojely.
_prujezdy_lock = threading.Lock()
#: `smycka_kdy` = kdy naposledy dorazil rámec z dekodéru (příjem ze smyčky),
#: `server_kdy` = kdy server dávku přijal. Dvě razítka, protože displej podle
#: návrhu Ri5 v2 (20. 8. 2026) ukazuje cestu průjezdu zvlášť: smyčka → server.
_prujezdy = {
    "kdy": 0.0, "celkem": 0, "ze_serveru": None, "naposledy": "",
    "smycka_kdy": 0.0, "server_kdy": 0.0,
    # Rámce, které **leží na disku** a čekají, až server začne brát. Nejsou
    # ztracené — krabička je pošle sama, jen později (`Preliv`).
    "preliv": 0,
    # Rámce, které se **opravdu ztratily**: nevešly se ani do přelivu (plná
    # karta, výpadek delší než strop souboru). Tohle už krabička nedohoní —
    # dekodér si je pamatuje, ale krabička protokolu nerozumí, takže je
    # stáhne jen aplikace přes ni a spouští to člověk (7. 9. 2026).
    "zahozeno": 0, "zahozeno_kdy": "",
}

#: Jak dlouho svítí dioda smyčky/serveru. Je to **puls**, ne stav: delší
#: svícení by z „právě přišel průjezd" udělalo „něco se kdysi stalo".
LED_SVITI_S = 2.2

#: Jak dlouho po průjezdu kontrolka svítí. Displej se obnovuje po 1 s; deset
#: sekund dává obsluze dost času potvrzení bezpečně zahlédnout.
PRUJEZD_SVITI_S = 10.0


def _zaznamenat_pocitadlo(celkem) -> None:
    """Počítadlo průjezdů od serveru — kontrolka svítí i u stahované cesty.

    Krabička protokolu nerozumí a když si průjezdy stahuje server sám, o nich
    vůbec neví: displej pak zůstával tmavý, i když měření běželo (Davidův
    nález 20. 8. 2026). Server posílá součet záložek dekodérů; jeho růst
    znamená průjezd. První odpověď jen založí základ, jinak by kontrolka
    blikla po každém startu agenta.
    """
    if celkem is None:
        return
    try:
        celkem = int(celkem)
    except (TypeError, ValueError):
        return
    with _prujezdy_lock:
        znamy = _prujezdy["ze_serveru"]
        _prujezdy["ze_serveru"] = celkem
        if znamy is None or celkem <= znamy:
            return
        pribylo = celkem - znamy
        _prujezdy["kdy"] = time.monotonic()
        # Stahovaná cesta: o průjezdu víme až od serveru, takže se rozsvítí
        # obě diody naráz. Jinak by u staré krabičky (1.0) zůstala levá
        # dioda navěky tmavá, i když měření běží.
        _prujezdy["smycka_kdy"] = time.monotonic()
        _prujezdy["server_kdy"] = time.monotonic()
        _prujezdy["celkem"] += pribylo
        _prujezdy["naposledy"] = time.strftime("%H:%M:%S")


def _zaznamenat_preliv(ramcu: int) -> None:
    """Kolik rámců čeká na disku, až server začne brát.

    Není to ztráta ani chyba — je to **odložená zásilka**. Na displeji stojí
    zvlášť od zahozených, protože obsluha nemá kvůli tomuhle nikam běžet:
    krabička to dořeší sama, jen potřebuje, aby se server ozval.
    """
    with _prujezdy_lock:
        _prujezdy["preliv"] = max(0, int(ramcu))


def _zaznamenat_zahozene(kolik: int) -> None:
    """Rámce, které se nevešly **ani do přelivu** — spočítat a nezamlčet.

    Tohle je až druhá obrana: při přeplněné frontě jdou starší rámce na disk
    (`Preliv`) a krabička je pošle sama. Sem se dostane jen to, co se nevešlo
    ani tam — plná karta nebo výpadek delší než strop souboru.

    A tady už je ztráta skutečná: dekodér si průjezdy pamatuje, ale krabička
    protokolu P3 nerozumí, takže si je stáhnout neumí. Umí to jen aplikace
    přes ni (`disciplines/timing_decoders.py::fetch_missing_passings` přes
    `tcp_exchange`) a **spouští to člověk** — proto se to musí říct serveru
    i obsluze na displeji.

    Do 7. 9. 2026 se zahazovalo mlčky, s poznámkou „dotáhne si to záložkou".
    Jenže nikdo se nedozvěděl, že se to má udělat.
    """
    if kolik <= 0:
        return
    with _prujezdy_lock:
        _prujezdy["zahozeno"] += kolik
        _prujezdy["zahozeno_kdy"] = time.strftime("%H:%M:%S")


def _zaznamenat_prujezdy(stored: int) -> None:
    if stored <= 0:
        return
    with _prujezdy_lock:
        _prujezdy["kdy"] = time.monotonic()
        _prujezdy["server_kdy"] = time.monotonic()
        _prujezdy["celkem"] += stored
        _prujezdy["naposledy"] = time.strftime("%H:%M:%S")
        # Aktivní POST rozsvítí displej okamžitě. Stejný průjezd se ale za
        # okamžik objeví i v serverovém počítadle záložek; posuneme proto
        # známý základ spolu s ním, aby se na displeji nezapočítal podruhé.
        if _prujezdy["ze_serveru"] is not None:
            _prujezdy["ze_serveru"] += stored


def _zaznamenat_smycku() -> None:
    """Z dekodéru přišel rámec — dioda „smyčka" se rozsvítí okamžitě.

    Krabička neví, jestli je ten rámec průjezd (P3 nezná) a vědět to nemá:
    pro obsluhu u trati je podstatné, že se smyčka **ozvala**. Že se z toho
    stal průjezd, potvrdí až server druhou diodou.
    """
    with _prujezdy_lock:
        _prujezdy["smycka_kdy"] = time.monotonic()


def _prujezdy_stav() -> dict:
    with _prujezdy_lock:
        snapshot = dict(_prujezdy)
    now = time.monotonic()

    def sviti(razitko: float, delka: float) -> bool:
        return bool(razitko) and (now - razitko) < delka

    return {
        "celkem": snapshot["celkem"],
        "cerstvy": sviti(snapshot["kdy"], PRUJEZD_SVITI_S),
        "smycka": sviti(snapshot["smycka_kdy"], LED_SVITI_S),
        "server": sviti(snapshot["server_kdy"], LED_SVITI_S),
        "naposledy": snapshot["naposledy"],
        # Dvě různé zprávy, ne jedna. „Čeká na disku" je odložená zásilka,
        # kterou krabička dořeší sama; „zahozeno" je ztráta, kvůli které
        # musí někdo v aplikaci kliknout. Slít je do jednoho čísla by
        # znamenalo posílat obsluhu k obrazovce i za výpadek, který se
        # spraví sám.
        "preliv": snapshot["preliv"],
        "zahozeno": snapshot["zahozeno"],
        "zahozeno_kdy": snapshot["zahozeno_kdy"],
    }


def _split_frames(buffer: bytearray) -> list[bytes]:
    """Vytáhne celé rámce SOR…EOR a nedokončený zbytek nechá v bufferu."""
    frames: list[bytes] = []
    while True:
        try:
            start = buffer.index(STREAM_SOR)
        except ValueError:
            buffer.clear()
            return frames
        if start:
            del buffer[:start]
        try:
            end = buffer.index(STREAM_EOR, 1)
        except ValueError:
            return frames
        frames.append(bytes(buffer[: end + 1]))
        del buffer[: end + 1]


class StreamLink:
    """Jedno trvalé spojení na dekodér: čte proud a posílá rámce serveru."""

    def __init__(self, server: Server, config: dict):
        self.server = server
        self.config = dict(config)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._posledni_ram = None
        # **Stav proudu, který se hlásí serveru.** Do 13. 9. 2026 o push cestě
        # cloud nevěděl vůbec nic: když nedoručovala, nikde to nebylo vidět
        # a hledalo se to hodinu měřením zvenčí (ostrý závod 13. 9. 2026,
        # medián 86 s). Krabička je jediná, kdo tyhle věci ví — tak je řekne.
        self.stav = {
            "decoder": str(config.get("decoder", "")),
            "host": str(config.get("host", "")),
            "spojeno": False,        # drží se spojení na dekodér?
            "chyba": "",             # poslední důvod, proč ne
            "fronta": 0,             # rámců čeká na odeslání
            "ram_pred_s": None,      # jak dávno přišel rámec z dekodéru
            "ack_ms": None,          # jak dlouho trvalo poslední potvrzení serveru
            "odeslano": 0,           # rámců potvrzených serverem od startu
        }
        self._thread = threading.Thread(
            target=self._run,
            name=f"stream-{config.get('host')}",
            daemon=True,
        )
        #: Odesílatel dávek; `None`, dokud ho `_run` nespustí.
        self._sender: threading.Thread | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._ready.set()

    def is_alive(self) -> bool:
        """Žije čtení **i odesílání**?

        Do 1.14 se hlídalo jen čtecí vlákno. Mrtvý odesílatel pak nechal
        rámce hromadit ve frontě, hlášení říkalo `zije: true` a nikdo proud
        nepřeotevřel (audit 23. 9. 2026). Teď `_sync_streams` takový proud
        zastaví a při dalším dotazu založí znovu.
        """
        if not self._thread.is_alive():
            return False
        sender = self._sender
        return sender is None or sender.is_alive()

    def hlaseni(self) -> dict:
        """Stav proudu pro server — s dopočítaným stářím posledního rámce."""
        zprava = dict(self.stav)
        zprava["ram_pred_s"] = (
            None if self._posledni_ram is None
            else round(time.monotonic() - self._posledni_ram, 1)
        )
        zprava["zije"] = self.is_alive()
        return zprava

    def matches(self, config: dict) -> bool:
        """Stejná smyčka a stejné otevírací rámce — spojení může běžet dál.

        Otevírací rámce se mění se záložkou serveru; přehrávají se jen při
        (re)connectu, takže běžící spojení kvůli nim netřeba trhat.
        """
        if self._stop.is_set():
            return False
        for key in ("decoder", "host", "port", "service", "service_seconds"):
            if self.config.get(key) != config.get(key):
                return False
        return True

    def _run(self) -> None:
        host = str(self.config.get("host", ""))
        port = int(self.config.get("port") or 0)
        decoder_id = str(self.config.get("decoder", ""))
        service = base64.b64decode(self.config.get("service") or b"")
        service_seconds = float(self.config.get("service_seconds") or 10)
        backoff = RECONNECT_MIN
        try:
            host, port = _validated_target(host, port)
        except ValueError as exc:
            _remember(host, port, False, str(exc))
            log(f"Proud {host}:{port} odmítnut: {exc}")
            return

        # Jediný odesílatel žije přes reconnecty dekodéru. Historie tak
        # doletí i tehdy, když dekodér mlčí nebo je odpojený.
        with FrameQueue(preliv_path(decoder_id).with_suffix(".sqlite3")) as pending:
            legacy = Preliv(preliv_path(decoder_id))
            while legacy.ceka():
                frames = legacy.dalsi(STREAM_MAX_FRAMES)
                expired = legacy.prosle()
                if not frames:
                    legacy.zahod_prosle()
                    _zaznamenat_zahozene(expired)
                    break
                if pending.pridej(frames, received=legacy._casy):
                    break  # starý soubor ponechat; migrace se zopakuje po restartu
                legacy.potvrd()
                _zaznamenat_zahozene(expired)
            sender = threading.Thread(target=self._send_loop,
                                      args=(decoder_id, pending), daemon=True)
            self._sender = sender
            sender.start()
            try:
                while not self._stop.is_set():
                    try:
                        with socket.create_connection((host, port), timeout=3.0) as sock:
                            sock.settimeout(1.0)
                            _remember(host, port, True, "passing stream")
                            self.stav["spojeno"] = True
                            self.stav["chyba"] = ""
                            for encoded in self.config.get("open") or []:
                                sock.sendall(base64.b64decode(encoded))
                            backoff = RECONNECT_MIN
                            self._pump(sock, pending, service, service_seconds)
                    except (OSError, TimeoutError, ValueError) as exc:
                        _remember(host, port, False, str(exc))
                        self.stav["spojeno"] = False
                        self.stav["chyba"] = str(exc)[:120]
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, RECONNECT_MAX)
            finally:
                self._stop.set()
                self._ready.set()
                sender.join()

    def _send_loop(self, decoder_id, pending) -> None:
        retry_delay = STREAM_RETRY_INITIAL_SECONDS
        while not self._stop.is_set():
            self._ready.clear()
            try:
                frames = pending.dalsi(STREAM_MAX_FRAMES)
                ceka = pending.ceka()
                self.stav["fronta"] = ceka
                _zaznamenat_preliv(ceka)
                if not frames:
                    self._ready.wait(STREAM_IDLE_SECONDS)
                    continue
                started = time.monotonic()
                answer = self.server.push_passings(decoder_id, frames, pending.casy())
                if not isinstance(answer, dict):
                    raise ValueError("the server answer is not an object")
                if not answer.get("ok"):
                    self.stav["chyba"] = str(answer.get("error") or "the server did not take the batch")[:120]
                    if answer.get("code") == KOD_ZAHODIT:
                        # Trvalá chyba: smyčka k tomuhle klubu nepatří.
                        # Držet dávku znamená jen odložit tutéž odpověď o dva
                        # dny — a pak ji přiznat jako ztrátu, kterou to není.
                        pending.potvrd()
                        _zaznamenat_preliv(pending.ceka())
                        self.stav["fronta"] = pending.ceka()
                        log(f"Průjezdy {decoder_id[:8]}: server smyčku nezná, "
                            f"{len(frames)} rámců zahozeno (nepatří do závodu)")
                        continue
                    # Odložená dávka: server žije a řekl „teď ne". Zkouší se
                    # dál, jen s delším stropem než po chybě spojení.
                    self._stop.wait(min(retry_delay, STREAM_ODLOZENO_SECONDS))
                    retry_delay = min(retry_delay * 2, STREAM_ODLOZENO_SECONDS)
                    continue
                retry_delay = STREAM_RETRY_INITIAL_SECONDS
                pending.potvrd()
                _zaznamenat_prujezdy(int(answer.get("stored") or 0))
                _zaznamenat_preliv(pending.ceka())
                elapsed = (time.monotonic() - started) * 1000
                self.stav["ack_ms"] = round(elapsed)
                self.stav["odeslano"] += len(frames)
                self.stav["fronta"] = pending.ceka()
                # **Loguje se každá dávka, ne jen ta pomalá.** Do 1.13 se
                # psal jen překročený rozpočet, takže z měřicí session byly
                # v logu vidět **jenom výpadky** — medián ani percentil se
                # z toho spočítat nedá a „vejdeme se do 200 ms?" zůstalo bez
                # odpovědi. Řádek na dávku unese i závodní den: dávek jsou
                # jednotky za vteřinu.
                #
                # Pomalá dávka má navíc vykřičník, ať se v `journalctl` pozná
                # očima bez filtrování.
                log(f"Průjezdy {decoder_id[:8]}: {len(frames)} rámců, "
                    f"potvrzení serveru {elapsed:.0f} ms"
                    f"{' !' if elapsed > 200 else ''}")
            except (OSError, ValueError, sqlite3.Error) as exc:
                # Včetně ztraceného ACK: nic se nemaže, duplicity řeší server.
                if isinstance(exc, TimeoutError):
                    # Server po `PRUJEZDY_TIMEOUT_S` neodpověděl — nejspíš
                    # pořád pracuje. Tutéž dávku za 50 ms by dostal podruhé
                    # do rozdělané práce; vteřina mu dá šanci doběhnout.
                    retry_delay = STREAM_RETRY_SECONDS
                self._stop.wait(min(retry_delay, STREAM_RETRY_SECONDS))
                retry_delay = min(retry_delay * 2, STREAM_RETRY_SECONDS)
            except Exception as exc:  # noqa: BLE001
                # Odpověď jiného tvaru než slovník (`AttributeError`) zabila
                # do 1.14 odesílatele a rámce se tiše hromadily ve frontě
                # (audit 23. 9. 2026). Nic se nemaže — dávka se zopakuje.
                self.stav["chyba"] = f"sender: {type(exc).__name__}: {exc}"[:120]
                log(f"Průjezdy {decoder_id[:8]}: nečekaná chyba odesílatele "
                    f"{type(exc).__name__}: {exc}")
                self._stop.wait(STREAM_RETRY_SECONDS)
                retry_delay = STREAM_RETRY_SECONDS

    def _pump(self, sock, pending, service, service_seconds) -> None:
        buffer = bytearray()
        last_service_at = time.monotonic()
        sock.settimeout(STREAM_RECV_TIMEOUT_SECONDS)
        while not self._stop.is_set():
            try:
                chunk = sock.recv(8192)
                if not chunk:
                    raise ConnectionError("The decoder closed the connection.")
                buffer.extend(chunk)
                if len(buffer) > STREAM_BUFFER_MAX:
                    log("Stream out of sync — dropping the buffer")
                    buffer.clear()
                frames = _split_frames(buffer)
                if frames:
                    self._posledni_ram = time.monotonic()
                    _zaznamenat_smycku()
                    lost = pending.pridej([
                        base64.b64encode(raw).decode("ascii") for raw in frames
                    ])
                    _zaznamenat_zahozene(lost)
                    if lost:
                        log(f"Fronta RI: {lost} rámců se nepodařilo uložit; dohledat z dekodéru")
                    _zaznamenat_preliv(pending.ceka())
                    self._ready.set()
            except socket.timeout:
                pass
            now = time.monotonic()
            if now - last_service_at >= service_seconds and service:
                sock.sendall(service)
                last_service_at = now

    def _dosli_preliv(self, decoder_id: str, preliv) -> None:
        """Pošle jednu dávku z přelivu — víc až v dalším průchodu.

        Posun se hýbe **až po přijetí**: kdyby krabička spadla mezi
        odesláním a potvrzením, pošle dávku znovu a server ji zahodí jako
        duplikát (průjezd má svoje číslo). Ztratit ji je horší než poslat
        dvakrát.
        """
        if not preliv.ceka():
            return
        davka = preliv.dalsi(PRELIV_DAVKA)
        # **Prošlé se zahodí, i když se dávka neposílá.** Krabička, která se
        # k serveru dva dny nedostala, nemá co vozit; ztráta se přizná, ať je
        # na displeji i v cloudu vidět (1.10).
        prosle = preliv.prosle()
        if not davka:
            zahozeno = preliv.zahod_prosle()
            if zahozeno:
                _zaznamenat_zahozene(zahozeno)
                _zaznamenat_preliv(preliv.ceka())
                log(f"Přeliv: {zahozeno} rámců starších {PRELIV_MAX_DNI} dnů "
                    f"zahozeno — do žádného otevřeného závodu už nepatří")
            return
        try:
            # **Přeliv čas přijetí neposílá schválně.** Tyhle rámce ležely na
            # disku, protože se nedaly doručit — jsou opožděné z definice
            # a do statistiky latence nepatří. Kdyby se do ní dostaly, každý
            # výpadek sítě by se v ní tvářil jako pomalé doručování.
            answer = self.server.push_passings(decoder_id, davka)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return
        if not answer.get("ok"):
            if answer.get("code") == KOD_ZAHODIT:
                # Trvalá chyba — viz `KOD_ZAHODIT`. Posun se hýbe, protože
                # tahle dávka doručená nikdy nebude.
                preliv.potvrd()
                _zaznamenat_preliv(preliv.ceka())
                log(f"Přeliv: server smyčku {decoder_id[:8]} nezná, "
                    f"{len(davka)} rámců zahozeno (nepatří do závodu)")
                return
            # „Teď ne" — dávka zůstává na disku a zkusí se příště. Posun se
            # neposouvá: potvrdit nedoručené by bylo tiché zahození (1.9).
            return
        preliv.potvrd()
        if prosle:
            _zaznamenat_zahozene(prosle)
            log(f"Přeliv: {prosle} rámců starších {PRELIV_MAX_DNI} dnů zahozeno")
        _zaznamenat_prujezdy(int(answer.get("stored") or 0))
        _zaznamenat_preliv(preliv.ceka())


# --- běh na pozadí ---------------------------------------------------------


class FrameQueue:
    """Trvalá FIFO fronta; mazání pouze po ACK, SQLite WAL + NORMAL sync.

    Přerušený zápis se vrátí zpět; přerušené potvrzení může zopakovat dávku,
    nikdy však nepotvrdí rámce přijaté během HTTP požadavku.

    **`synchronous=NORMAL`, ne `FULL`** (13. 9. 2026, kvůli rozpočtu 0,2 s na
    cestu od smyčky do cloudu): zápis rámce leží na přijímací cestě a `FULL`
    fsyncuje každý commit, což na SD kartě stojí jednotky až desítky
    milisekund. Cena: **výpadek napájení** může sebrat poslední commity, které
    se ještě nedostaly z WAL na disk. Pád procesu ani zabití agenta ne — ty
    WAL přežije. Okno je malé (agent odesílá do milisekund a maže až po ACK),
    ale existuje; kdyby se ukázalo, že se u trati vypíná proud, patří sem
    zpátky `FULL`. Docstring tu do dneška tvrdil `FULL`, i když kód dělal
    `NORMAL` — slib o trvanlivosti, který se nekryl s kódem.
    """

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS frames ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, received REAL NOT NULL, "
                        "frame TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS frames_received ON frames(received)")
        self.db.execute("CREATE TABLE IF NOT EXISTS queue_size (id INTEGER PRIMARY KEY, bytes INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO queue_size SELECT 1, COALESCE(SUM(LENGTH(frame)), 0) FROM frames")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS frame_added AFTER INSERT ON frames "
                        "BEGIN UPDATE queue_size SET bytes = bytes + LENGTH(NEW.frame) WHERE id = 1; END")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS frame_removed AFTER DELETE ON frames "
                        "BEGIN UPDATE queue_size SET bytes = bytes - LENGTH(OLD.frame) WHERE id = 1; END")
        self.db.commit()
        self._offered = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def pridej(self, frames, *, received=None):
        if not frames:
            return 0
        with self._lock:
            try:
                with self.db:
                    used = self.db.execute(
                        "SELECT bytes FROM queue_size WHERE id = 1"
                    ).fetchone()[0]
                    if used + sum(map(len, frames)) > PRELIV_MAX_BAJTU:
                        return len(frames)
                    self.db.executemany("INSERT INTO frames(received, frame) VALUES (?, ?)",
                                        list(zip(received or [time.time()] * len(frames), frames)))
                return 0
            except sqlite3.Error:
                return len(frames)

    def ceka(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]

    def dalsi(self, limit):
        with self._lock:
            with self.db:
                expired = self.db.execute("DELETE FROM frames WHERE received < ?",
                                          (time.time() - PRELIV_MAX_DNI * 86400,)).rowcount
            _zaznamenat_zahozene(expired)
            rows = self.db.execute("SELECT id, frame, received FROM frames ORDER BY id LIMIT ?",
                                   (limit,)).fetchall()
            self._offered = [row[0] for row in rows]
            # Čas přijetí si fronta vede od začátku (kvůli vypršení), jen ho
            # dosud nikdo nečetl. Posílá se s dávkou, aby se na serveru dala
            # latence rozložit na úseky — bez něj je v ní chyba hodin
            # dekodéru neoddělitelně smíchaná s dobou doručení.
            self._offered_casy = [row[2] for row in rows]
            return [row[1] for row in rows]

    def casy(self):
        """Unixové časy (ms), kdy krabička ty rámce převzala od dekodéru."""
        return [int(float(t) * 1000) for t in getattr(self, "_offered_casy", [])]

    def potvrd(self):
        with self._lock:
            with self.db:
                self.db.executemany("DELETE FROM frames WHERE id = ?",
                                    [(pk,) for pk in self._offered])
            self._offered = []


class Preliv:
    """Rámce, které se nevešly do paměťové fronty — **na disk, ne do koše**.

    Do 7. 9. 2026 se při přeplněné frontě mlčky zahazovaly. Přitom krabička
    nemusí rozumět tomu, co v rámci je, aby ho uložila a poslala později:
    je to neprůhledný kus bajtů, který server pošle sám sobě, jen se
    zpožděním. Tohle je jediné dohledání, které krabička zvládne bez
    znalosti protokolu — a zvládne ho **bez obsluhy**, což ruční dohledání
    z dekodéru neumí.

    Soubor je řádkový: `unixový čas přidání` + mezera + base64 rámce. Rámec
    neobsahuje konec řádku ani mezeru, takže se v něm nemá jak splést. Čte se
    sekvenčně podle posunu, protože přepisovat osmimegabajtový soubor po
    každé dávce by kartu v Raspberry umlelo.

    **Co se nepodaří odevzdat do dvou dnů, se maže** (Davidovo zadání
    12. 9. 2026). Průjezd starší než víkend do žádného otevřeného závodu
    nepatří a karta ho nemá vozit do příští sezóny; ztráta se přizná
    (`zahozeno`) a je vidět na displeji i v cloudu.

    Pořadí: **živá dávka jde první, přeliv za ní.** Pro výsledky to nic
    neznamená (počítají se z časů průjezdů, ne z pořadí příchodu) a pro desku
    je to lepší — ukáže aktuální stav a historii dorovná dodatečně, místo
    aby čekala, až se doveze půlhodinový výpadek.
    """

    def __init__(self, cesta: pathlib.Path):
        self.cesta = cesta
        self._posun = 0
        #: Kam by se posun dostal, kdyby server nabídnutou dávku vzal.
        self._nabidnuto = 0
        self._nabidnuto_pocet = 0
        #: Kolik rámců poslední `dalsi()` zahodila jako prošlé (starší dvou dnů).
        self._prosle = 0
        self._lock = threading.Lock()
        #: Počet čekajících rámců. Po restartu se sečte ze souboru — displej
        #: má říct „čeká 800 rámců", ne „čeká 32 kB"; obsluha u trati počítá
        #: jezdce, ne bajty.
        self._ceka = self._pocet_v_souboru()

    @staticmethod
    def _radek(ramec: str, kdy: float | None = None) -> str:
        return f"{int(kdy if kdy is not None else time.time())} {ramec}\n"

    @staticmethod
    def _rozloz(radek: str) -> tuple[int, str]:
        """`(čas přidání, rámec)`. Řádek bez času je z verze ≤1.9 — bere se
        jako čerstvý, protože kdy vznikl, se už zjistit nedá."""
        kdy, _, ramec = radek.strip().partition(" ")
        if not ramec:
            return int(time.time()), kdy
        try:
            return int(kdy), ramec
        except ValueError:
            return int(time.time()), radek.strip()

    def _pocet_v_souboru(self) -> int:
        try:
            with self.cesta.open("rb") as soubor:
                return sum(1 for _ in soubor)
        except OSError:
            return 0

    def pridej(self, radky: list) -> int:
        """Uloží rámce a vrátí, **kolik se jich opravdu ztratilo**.

        Ztráta nastane jen při plném disku nebo přeplněném souboru; taková
        se hlásí serveru a řeší se ručním dohledáním z dekodéru (to jde od
        záložky dál, takže díra se dotáhne celá).
        """
        if not radky:
            return 0
        with self._lock:
            try:
                if self.cesta.exists() and self.cesta.stat().st_size > PRELIV_MAX_BAJTU:
                    return len(radky)
                self.cesta.parent.mkdir(parents=True, exist_ok=True)
                ted = time.time()
                with self.cesta.open("a", encoding="ascii") as soubor:
                    soubor.write("".join(self._radek(radek, ted) for radek in radky))
                self._ceka += len(radky)
                return 0
            except OSError:
                # Karta plná nebo jen pro čtení — víc než přiznat ztrátu se
                # tady udělat nedá.
                return len(radky)

    def ceka(self) -> int:
        """Kolik rámců v přelivu ještě nikdo neodeslal (0 = prázdno)."""
        with self._lock:
            return self._ceka

    def dalsi(self, kolik: int) -> list:
        """Další dávka k odeslání; posun se hýbe až po `potvrd()`."""
        with self._lock:
            try:
                with self.cesta.open("r", encoding="ascii") as soubor:
                    soubor.seek(self._posun)
                    davka, prosle = [], 0
                    self._casy = []
                    hranice = time.time() - PRELIV_MAX_DNI * 86400
                    while len(davka) < kolik:
                        radek = soubor.readline()
                        if not radek.endswith("\n"):
                            # Nedopsaný poslední řádek — dopíše se za chvíli,
                            # teď by se poslal ořezaný.
                            break
                        if not radek.strip():
                            continue
                        kdy, ramec = self._rozloz(radek)
                        if kdy < hranice:
                            # **Starší než dva dny se zahazuje** a počítá jako
                            # ztráta: do žádného otevřeného závodu už nepatří.
                            prosle += 1
                            continue
                        davka.append(ramec)
                        self._casy.append(kdy)
                    self._nabidnuto = soubor.tell()
                    self._nabidnuto_pocet = len(davka) + prosle
                    self._prosle = prosle
                return davka
            except OSError:
                return []

    def prosle(self) -> int:
        """Kolik rámců poslední `dalsi()` zahodila, protože jim vypršel čas."""
        with self._lock:
            return self._prosle

    def zahod_prosle(self) -> int:
        """Posune se za rámce, kterým vypršel čas — i když se nic neodesílalo.

        Bez tohohle by prošlé rámce ležely ve frontě navždy u krabičky, která
        se serverem nemluví: `dalsi()` je sice přeskočí, ale posun se hýbe až
        po `potvrd()`, a ten přijde jen po přijaté dávce.
        """
        self.dalsi(PRELIV_DAVKA)
        with self._lock:
            if not self._prosle:
                return 0
            prosle = self._prosle
        # Posun i počet čekajících se srovnají touž cestou jako po přijetí;
        # rámce v dávce, které prošlé nejsou, se nabídnou znovu příště.
        self.potvrd()
        return prosle

    def potvrd(self) -> None:
        """Server dávku vzal — posunout se za ni a případně soubor smazat."""
        with self._lock:
            self._posun = max(self._posun, self._nabidnuto)
            self._ceka = max(0, self._ceka - self._nabidnuto_pocet)
            self._nabidnuto_pocet = 0
            try:
                if self.cesta.stat().st_size <= self._posun:
                    # Doslané je doslané — soubor smazat, ať karta nedrží
                    # osm megabajtů historie do dalšího závodu.
                    self.cesta.unlink()
                    self._posun = 0
                    self._ceka = 0
            except OSError:
                pass


def preliv_path(decoder_id: str) -> pathlib.Path:
    """Soubor přelivu pro jednu smyčku — vedle nastavení, ne v /tmp.

    Vedle nastavení proto, že přeliv musí přežít restart krabičky: výpadek
    sítě a restart jdou v praxi spolu (někdo přepojuje switch).
    """
    jmeno = "".join(z for z in decoder_id if z.isalnum() or z in "-_")[:36]
    return config_path().with_name(f"preliv-{jmeno or 'smycka'}.txt")



class Worker:
    """Agent běžící ve vlákně: dá se spustit, zastavit a zeptat se na stav.

    Kvůli okénku s ikonou v liště — to musí zůstat obsluze k ruce, zatímco
    agent na pozadí pracuje. Bez vlákna by se dalo jen buď dívat, nebo běžet.
    """

    def __init__(self, server_url: str, token: str, *, on_status=None):
        self.server = Server(server_url, token)
        self.on_status = on_status or (lambda text, connected: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.status = "not started"
        self.latest_version = VERSION
        self.latest_sha256 = ""
        #: id smyčky -> běžící proud průjezdů (StreamLink)
        self._streams: dict[str, StreamLink] = {}
        #: Kdy naposledy přišel příkaz (`time.monotonic`). Start se počítá
        #: jako příkaz: po zapnutí krabičky obsluha typicky nastavuje.
        self._posledni_prikaz = time.monotonic()

    # -- řízení ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._sync_streams([])
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._set("stopped", connected=False)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def umrel(self) -> bool:
        """Vlákno agenta skončilo, aniž by ho někdo zastavil?

        Tohle hlídá `main` (audit 23. 9. 2026): proces s webem by jinak žil
        dál a systemd by neměl co restartovat.
        """
        thread = self._thread
        return bool(thread is not None and not thread.is_alive()
                    and not self._stop.is_set())

    # -- vnitřek -----------------------------------------------------------

    def _set(self, text: str, *, connected: bool) -> None:
        self.status = text
        self.connected = connected
        log(text)
        try:
            self.on_status(text, connected)
        except Exception:  # noqa: BLE001 — chyba v okénku nesmí shodit agenta
            pass

    def _sync_streams(self, wanted: list[dict]) -> None:
        """Srovná běžící proudy s konfigurací ze serveru.

        Nová smyčka v konfiguraci → otevřít; zmizelá → zavřít; změněná
        adresa nebo servis → přeotevřít. Otevírací rámce (mění se se
        záložkou) běžící spojení netrhají — přehrávají se jen po výpadku.
        """
        by_id = {str(item.get("decoder", "")): item for item in wanted if item.get("decoder")}

        for decoder_id in list(self._streams):
            link = self._streams[decoder_id]
            config = by_id.get(decoder_id)
            if config is not None and link.matches(config) and link.is_alive():
                link.config = dict(config)  # čerstvé otevírací rámce pro reconnect
                continue
            link.stop()
            if link.is_alive():
                continue
            del self._streams[decoder_id]
            if config is None:
                log(f"Proud {decoder_id[:8]} ukončen — závod si ho už neříká")

        if self._stop.is_set():
            return
        for decoder_id, config in by_id.items():
            if decoder_id in self._streams:
                continue
            link = StreamLink(self.server, config)
            self._streams[decoder_id] = link
            link.start()

        # Stav se přepisuje celý: smyčka, kterou závod už nechce, ze hlášení
        # zmizí spolu s proudem.
        _STAV_PROUDU.clear()
        for decoder_id, link in self._streams.items():
            # `getattr` schválně: proud smí být i jiný objekt (zkoušky mají
            # vlastní dvojníka) a hlášení stavu nesmí být důvod k pádu odběru.
            hlaseni = getattr(link, "hlaseni", None)
            if callable(hlaseni):
                _STAV_PROUDU[decoder_id] = hlaseni()

    def pauza_pred_dotazem(self, ted: float | None = None) -> float:
        """Kolik počkat, než se krabička zase zeptá na příkazy (viz `PRIKAZY_*`)."""
        ted = time.monotonic() if ted is None else ted
        ticho = ted - self._posledni_prikaz
        if ticho < PRIKAZY_HORKO_S:
            return 0.0
        if not self._streams and ticho >= PRIKAZY_KLID_PO_S:
            return PRIKAZY_PAUZA_KLID_S
        return PRIKAZY_PAUZA_S

    def _run(self) -> None:
        backoff = RECONNECT_MIN
        greeted = False
        self._set(f"connecting to {self.server.base}", connected=False)

        while not self._stop.is_set():
            try:
                if not greeted:
                    hello = self.server.hello()
                    greeted = True
                    self.latest_version = str(hello.get("latest_version") or VERSION)
                    self.latest_sha256 = str(hello.get("agent_sha256") or "")
                    name = hello.get("agent")
                    organization = hello.get("organization")
                    self._set(f"connected as {name} ({organization})", connected=True)

                answer = self.server.poll()
                # Tvar odpovědi se ověřuje tady, ne až `AttributeError`em
                # uvnitř smyčky (audit 23. 9. 2026): cokoli jiného než slovník
                # je vadná odpověď serveru, tedy dočasná chyba s backoffem.
                if not isinstance(answer, dict):
                    raise ValueError("the server answer is not an object")
                _zaznamenat_pocitadlo(answer.get("passings"))
                streams = answer.get("stream") or []
                self._sync_streams([item for item in streams if isinstance(item, dict)]
                                   if isinstance(streams, list) else [])
                commands = answer.get("commands") or []
                for command in commands if isinstance(commands, list) else []:
                    if self._stop.is_set():
                        break
                    if not isinstance(command, dict):
                        log(f"Příkaz v nečitelném tvaru přeskočen: {command!r:.80}")
                        continue
                    args = command.get("args")
                    host = args.get("host", "") if isinstance(args, dict) else ""
                    log(f"Příkaz {command.get('action')} → {host}")
                    run_command(self.server, command)
                backoff = RECONNECT_MIN
                if commands:
                    # Razítko až po vyřízení: výsledek odešel teprve teď
                    # a navazující příkaz server vystaví až po něm.
                    self._posledni_prikaz = time.monotonic()
                pauza = self.pauza_pred_dotazem()
                if pauza and self._stop.wait(pauza):
                    break
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    # Token aplikace (zatím) nezná — přesně tenhle stav má
                    # krabička po zapnutí, než ho někdo opíše do Nastavení
                    # dekodérů. Není to chyba, je to čekání.
                    greeted = False
                    self._set("waiting for approval in the app", connected=False)
                    self._stop.wait(APPROVAL_POLL_SECONDS)
                    continue
                greeted = False
                self._set(f"server answered {exc.code}, retrying", connected=False)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
                greeted = False
                self._set(f"server unavailable ({exc})", connected=False)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            except Exception as exc:  # noqa: BLE001
                # Poslední záchranná síť (audit 23. 9. 2026): cokoli
                # nečekaného se zaloguje a zkusí se znovu. Mrtvé vlákno
                # `agent` by krabičku odstřihlo od serveru bez jediné stopy.
                greeted = False
                log(f"Nečekaná chyba agenta: {type(exc).__name__}: {exc}")
                self._set(f"internal error ({type(exc).__name__}), retrying", connected=False)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)


# --- nastavení na disku ----------------------------------------------------


def _config_base(jmeno_unix: str, jmeno_okna: str) -> pathlib.Path:
    """Složka s nastavením podle zvyklostí systému."""
    if sys.platform.startswith("win"):
        return pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home())) / jmeno_okna
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / jmeno_okna
    return pathlib.Path(
        os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
    ) / jmeno_unix


def config_path() -> pathlib.Path:
    """Kde má agent uložený server a token — podle zvyklostí systému."""
    return _config_base("bikody-agent", "BikodyAgent") / "config.json"


def stara_config_path() -> pathlib.Path:
    """Kde token ležel před přejmenováním na BIKODY (18. 9. 2026).

    **Token je identita krabičky, ne odvozený údaj.** Když ho agent nenajde,
    vyrobí si nový a v aplikaci se ohlásí jako nespárovaný — obsluha ho pak
    musí u trati opsat z displeje. Přejmenovaná složka by to udělala každé
    krabičce v terénu při první aktualizaci.
    """
    return _config_base("event-control-agent", "EventControlAgent") / "config.json"


#: Přenos ze staré složky se dělá jednou za běh procesu — `load_config` volá
#: i obsluha každého HTTP požadavku a prohledávat adresář po sekundě je zbytečné.
_stara_slozka_prenesena = False


def prenes_stara_nastaveni() -> list[str]:
    """Přenese nastavení a přeliv z předchozí složky. Vrátí, co přenesla.

    Kromě tokenu jde o **přeliv** — rámce, které se nevešly do fronty a čekají
    na disku, až se server ozve. Leží vedle nastavení právě proto, aby přežily
    restart krabičky (výpadek sítě a restart chodí v praxi spolu). Přejmenovaná
    složka by je osiřela stejně tiše jako token.

    Nic se **nepřepisuje ani nemaže**: co už v nové složce je, platí, a stará
    zůstane ležet, takže návrat na předchozí verzi agenta je pořád možný.
    """
    stara = stara_config_path().parent
    nova = config_path().parent
    if not stara.is_dir() or stara == nova:
        return []

    preneseno = []
    for zdroj in sorted(stara.iterdir()):
        if zdroj.name != "config.json" and not zdroj.name.startswith("preliv-"):
            continue
        if not zdroj.is_file():
            continue
        cil = nova / zdroj.name
        if cil.exists():
            continue
        try:
            nova.mkdir(parents=True, exist_ok=True)
            cil.write_bytes(zdroj.read_bytes())
            cil.chmod(0o600)
        except OSError:
            continue      # nová složka nemusí být zapisovatelná; číst jde dál
        preneseno.append(zdroj.name)
    return preneseno


def _precti_config(path: pathlib.Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_config() -> dict:
    """Nastavení z disku; po přejmenování i z původní složky."""
    global _stara_slozka_prenesena

    data = _precti_config(config_path())
    if data is not None:
        return data

    if not _stara_slozka_prenesena:
        _stara_slozka_prenesena = True
        if prenes_stara_nastaveni():
            data = _precti_config(config_path())
            if data is not None:
                return data

    # Přenos mohl selhat na právech — číst se dá i ze staré cesty.
    return _precti_config(stara_config_path()) or {}


def configured_server(config: dict) -> str:
    """Adresa aplikace — z nastavení, jinak výchozí `DEFAULT_SERVER`.

    Prázdný řetězec v souboru znamená „nikdo nic nezadal", ne „nikam se
    nehlásit": krabička se staví pro jednu aplikaci a obsluha u trati nemá co
    opisovat adresu. Kdo chce vlastní server, přepíše ji v nastavení.
    """
    return (config.get("server") or "").strip() or DEFAULT_SERVER


def save_config(server_url: str, token: str, *, autostart: bool = False) -> None:
    """Uloží nastavení tak, aby ho nečetl kdokoli — token je heslo do sítě.

    **Atomicky** (audit 23. 9. 2026): dočasný soubor s právy 0600, `fsync`
    a `os.replace`. Přímý `write_text` po výpadku proudu uprostřed zápisu
    nechal prázdný nebo useknutý `config.json`; agent ho pak nepřečetl,
    vyrobil **nový token** a krabička se v aplikaci ohlásila jako
    nespárovaná. Právo 0600 má soubor od prvního bajtu, ne až po `chmod`.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {"server": server_url, "token": token, "autostart": autostart}, indent=2,
    ).encode("utf-8")
    docasny = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(docasny, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as soubor:
            soubor.write(data)
            soubor.flush()
            os.fsync(soubor.fileno())
        try:
            os.chmod(docasny, 0o600)   # umask ani starý soubor práva nerozšíří
        except OSError:
            pass      # Windows práva takhle neumí a nevadí to
        os.replace(docasny, path)
    except BaseException:
        try:
            os.unlink(docasny)
        except OSError:
            pass
        raise
    if hasattr(os, "O_DIRECTORY"):
        # Přejmenování je trvalé až se zapsanou složkou.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


# --- spouštění po startu počítače -----------------------------------------


def autostart_path() -> pathlib.Path:
    """Soubor, kterým se agent přihlásí ke spuštění po startu počítače."""
    if sys.platform.startswith("win"):
        return (
            pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home()))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "BikodyAgent.cmd"
        )
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "LaunchAgents" / "cz.bikody.agent.plist"
    return (
        pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
        / "autostart" / "bikody-agent.desktop"
    )


def _autostart_body(command: list[str]) -> str:
    """Obsah souboru pro automatický start — pro každý systém jeho tvar."""
    quoted = " ".join(f'"{part}"' for part in command)
    if sys.platform.startswith("win"):
        return f"@echo off\r\nstart \"\" {quoted}\r\n"
    if sys.platform == "darwin":
        args = "".join(f"        <string>{part}</string>\n" for part in command)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "    <key>Label</key>\n    <string>cz.bikody.agent</string>\n"
            f"    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n"
            "    <key>RunAtLoad</key>\n    <true/>\n"
            "    <key>KeepAlive</key>\n    <true/>\n"
            "</dict>\n</plist>\n"
        )
    return (
        "[Desktop Entry]\nType=Application\nName=BIKODY — agent u trati\n"
        f"Exec={quoted}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n"
    )


SERVICE_NAME = "bikody-agent.service"


def service_paths() -> tuple[pathlib.Path, bool]:
    """Kam zapsat unit systemd a jestli je systémová. Druhá hodnota = systémová."""
    if os.geteuid() == 0:
        return pathlib.Path("/etc/systemd/system") / SERVICE_NAME, True
    base = pathlib.Path(
        os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
    )
    return base / "systemd" / "user" / SERVICE_NAME, False


def _service_unit(command: list[str]) -> str:
    quoted = " ".join(f'"{part}"' for part in command)
    return (
        "[Unit]\n"
        "Description=BIKODY — agent u trati\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={quoted}\n"
        # Krabička u trati musí vstát sama: spadlý agent znamená závod bez
        # časomíry a nikdo u trati nehlídá, jestli proces ještě žije.
        "Restart=always\n"
        "RestartSec=5\n"
        "StartLimitIntervalSec=0\n"
        f"WorkingDirectory={pathlib.Path.home()}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        if os.geteuid() == 0
        else (
            "[Unit]\n"
            "Description=BIKODY — agent u trati\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            f"ExecStart={quoted}\n"
            "Restart=always\n"
            "RestartSec=5\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    )


def install_service() -> int:
    """Zapíše a zapne službu systemd, která agenta po pádu i po restartu vrátí.

    **Proč to nestačí přes „spouštět po startu"**: na Linuxu je to soubor
    v `~/.config/autostart`, tedy věc plochy — bez přihlášené grafické session
    se nespustí vůbec, a **spadlý proces nikdo nezvedne**. Krabička u trati
    přitom stojí v garáži bez klávesnice a spadlý agent znamená závod bez
    časomíry (David, 19. 8. 2026 — čtyři dny před prvním ostrým závodem).

    Bez roota se instaluje **uživatelská** služba; aby běžela i bez přihlášení,
    je potřeba `loginctl enable-linger`, což skript vypíše.
    """
    if not sys.platform.startswith("linux"):
        print(
            'The service installs only on Linux (the trackside box). '
            'On macOS a LaunchAgent with KeepAlive holds the agent; on Windows '
            'use the "Start when the computer boots" option in the settings.'
        )
        return 1

    program = pathlib.Path(sys.argv[0]).resolve()
    command = [str(program)] if getattr(sys, "frozen", False) else [sys.executable, str(program)]
    path, system = service_paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_service_unit(command), encoding="utf-8")

    scope = [] if system else ["--user"]
    steps = [
        ["systemctl", *scope, "daemon-reload"],
        ["systemctl", *scope, "enable", SERVICE_NAME],
        ["systemctl", *scope, "restart", SERVICE_NAME],
    ]
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Failed: {' '.join(step)}\n{result.stderr.strip()}")
            return result.returncode
        print(f"OK: {' '.join(step)}")

    print(f"\nThe service is in {path}")
    if system:
        print("The agent starts on boot and after a crash. Log: journalctl -u " + SERVICE_NAME + " -f")
    else:
        user = os.environ.get("USER") or "pi"
        print(
            "The agent starts on login and after a crash. To run it without a login:\n"
            f"    sudo loginctl enable-linger {user}\n"
            f"Log: journalctl --user -u {SERVICE_NAME} -f"
        )
    return 0


def uninstall_service() -> int:
    """Vypne a smaže službu — pro notebook, kde má agenta spouštět obsluha."""
    if not sys.platform.startswith("linux"):
        print("The service exists only on Linux.")
        return 1
    path, system = service_paths()
    scope = [] if system else ["--user"]
    for step in (
        ["systemctl", *scope, "disable", "--now", SERVICE_NAME],
        ["systemctl", *scope, "daemon-reload"],
    ):
        subprocess.run(step, capture_output=True, text=True)
    try:
        path.unlink()
    except OSError:
        pass
    print(f"Služba odstraněna ({path}).")
    return 0


def service_state() -> str:
    """Co říká systemd o službě — pro displej krabičky. Prázdné = neinstalovaná."""
    if not sys.platform.startswith("linux"):
        return ""
    path, system = service_paths()
    if not path.exists():
        return ""
    scope = [] if system else ["--user"]
    result = subprocess.run(
        ["systemctl", *scope, "is-active", SERVICE_NAME], capture_output=True, text=True
    )
    return (result.stdout or result.stderr or "").strip()


def set_autostart(enabled: bool) -> pathlib.Path | None:
    """Přihlásí (nebo odhlásí) agenta ke spuštění po startu počítače.

    Píše se do uživatelské složky, ne do systému: u trati nikdo nemá chodit
    pro heslo správce a agent má běžet pod tím, kdo u počítače sedí.
    """
    path = autostart_path()
    if not enabled:
        try:
            path.unlink()
        except OSError:
            pass
        return None

    program = pathlib.Path(sys.argv[0]).resolve()
    command = [str(program)] if getattr(sys, "frozen", False) else [sys.executable, str(program)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_autostart_body(command), encoding="utf-8")
    if not sys.platform.startswith("win"):
        try:
            path.chmod(0o755)
        except OSError:
            pass
    return path


# --- token krabičky --------------------------------------------------------
#
# **Token si vyrábí krabička, ne server.** Na jejím displeji se ukáže a obsluha
# ho opíše do aplikace (Nastavení aplikace → Přihlásit krabičku). Obráceně by se třiačtyřicetiznakový
# řetězec opisoval na dotykovém displeji — a to nikdo nechce. Takhle se píše
# tam, kde je klávesnice.
#
# Abeceda je bez znaků, které se na obrazovce pletou (0/O, 1/I/L), a token je
# po čtveřicích: šest skupin = 120 bitů náhody. Na displeji se láme na dva
# řádky po třech čtveřicích, takže se čte pohodlně i z 3,5" panelu.

TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_GROUPS = 6

#: Tlačítko „Nový token" na displeji jistí dvě klepnutí: nový token odpojí
#: spárovanou krabičku, takže ho nesmí vydat náhodný dotek. První klepnutí
#: odjistí, druhé musí přijít do téhle lhůty.
NOVY_TOKEN_POTVRZENI_S = 15.0
_novy_token_pozadan = 0.0
TOKEN_GROUP_LEN = 4


def generate_token() -> str:
    import secrets

    groups = [
        "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_GROUP_LEN))
        for _ in range(TOKEN_GROUPS)
    ]
    return "-".join(groups)


def normalize_token(raw: str) -> str:
    """Token z displeje — bez mezer a pomlček, velkými písmeny.

    Obsluha ho opisuje z obrazovky, takže pomlčky vynechá, přidá mezery nebo
    napíše malá písmena. Server i agent porovnávají stejně očištěný tvar.
    """
    return "".join(char for char in (raw or "").upper() if char.isalnum())


def maskuj_token(token: str) -> str:
    """Token bez tajemství: první skupina a tečky (`AKUW-••••`).

    Stačí k tomu, aby obsluha poznala, *která* krabička to je, a nestačí
    k tomu, aby se za ni kdokoli jiný vydával.
    """
    token = (token or "").strip()
    if not token or token == "—":
        return token or "—"
    prvni = token.split("-", 1)[0][:TOKEN_GROUP_LEN]
    return f"{prvni}-••••"


def ensure_token(config: dict) -> str:
    """Token krabičky — jednou vyrobený zůstává, dokud ho někdo nezmění."""
    token = (config.get("token") or "").strip()
    if token:
        return token
    token = generate_token()
    save_config(config.get("server", ""), token, autostart=bool(config.get("autostart")))
    # Do logu jen maskovaný: `journalctl` čte víc lidí a strojů než displej
    # (audit 23. 9. 2026). Celý je na displeji krabičky.
    log(f"Vyroben token krabičky: {maskuj_token(token)}")
    return token


# --- displej a obsluha přes prohlížeč --------------------------------------
#
# Agent nemá okno ani ikonu v liště: obojí by znamenalo knihovnu navíc pro
# každý systém zvlášť. Místo toho má **vlastní stránku**. Na krabičce u trati
# běží přes celou obrazovku a je to její displej; z notebooku se otevře přes
# síť. Nastavení (adresa aplikace) je pod ní na `/nastaveni`, aby se na hlavní
# obrazovce nedalo omylem nic přepsat.
#
# Bez závislostí: `http.server` je ve standardní knihovně, styl je vlastní.

WEB_PORT = 8088

_STYLE = """
 /* Displej krabičky podle návrhu `bikody_ri5_v2.html` (David, 20. 8. 2026).
    Návrh stojí na Tailwindu z CDN; tady je přepsaný do vlastního CSS —
    krabička u trati bývá bez internetu a stránka z CDN by se jí nenačetla
    vůbec. Rozměry jdou z `clamp()` stejně jako v návrhu, takže obrazovka
    sedne na monitor i na 3,5" SPI displej. */
 :root {{ color-scheme: dark; }}
 * {{ box-sizing: border-box; }}
 html, body {{ margin:0; width:100%; height:100%; background:#050505; overflow:hidden;
               font-family: Arial, Helvetica, sans-serif; color:#fff; }}
 main {{ width:100vw; height:100vh; padding:16px;
         background: radial-gradient(circle at 50% 20%, rgba(30,34,36,.45), transparent 35%),
                     linear-gradient(180deg, #070809 0%, #020303 100%); }}
 .ramecek {{ width:100%; height:100%; border-radius:28px; border:1px solid #27272a;
             background:rgba(0,0,0,.8); padding:16px; display:flex; flex-direction:column;
             gap:16px; }}

 /* Hlavička: značka vlevo, hodiny a datum vpravo, červená linka pod tím. */
 header {{ display:flex; align-items:center; justify-content:space-between;
           border-bottom:2px solid #dc2626; padding-bottom:12px; }}
 h1 {{ margin:0; font-weight:900; letter-spacing:.11em; line-height:1;
       font-size:clamp(2rem, 6vw, 4.5rem); }}
 h1 .tecka {{ color:#ef4444; }}
 .hodiny {{ text-align:right; }}
 .hodiny .cas {{ font-weight:900; line-height:1; font-size:clamp(1.5rem, 4vw, 3rem); }}
 .hodiny .datum {{ margin-top:4px; color:#d4d4d8; font-size:clamp(.75rem, 1.8vw, 1.2rem); }}
 /* Verze agenta v hlavičce. V patičce byla od začátku, jenže na 3,5"
    displeji u trati si jí nikdo nevšiml — a po nasazení je to první věc,
    kterou obsluha potřebuje ověřit („ideálně bych na obrazovce RI viděl
    i verzi agenta", David 12. 9. 2026). */
 .hodiny .verze {{ margin-top:2px; color:#a1a1aa; letter-spacing:.08em;
                   font-size:clamp(.6rem, 1.3vw, .9rem); }}
 .hodiny .verze strong {{ color:#a3e635; }}

 /* Dvě karty na polovinu: stav serveru a tlačítko nového tokenu. */
 .dvojice {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
 .karta {{ border-radius:24px; border:2px solid {barva}; background:rgba(0,0,0,.4);
           padding:16px; display:flex; flex-direction:column; align-items:center;
           justify-content:center; min-height:145px; }}
 .karta .popisek {{ text-transform:uppercase; letter-spacing:.05em; font-weight:900;
                    font-size:clamp(1rem, 2.6vw, 1.8rem); margin:0; }}
 .vysledek {{ margin-top:12px; display:flex; align-items:center; gap:16px; }}
 .kolecko {{ width:clamp(4rem,7vw,6rem); height:clamp(4rem,7vw,6rem); border-radius:50%;
             border:5px solid {barva}; display:flex; align-items:center; justify-content:center;
             color:{barva}; font-size:clamp(2rem,4.5vw,3.5rem); font-weight:900; }}
 .slovo {{ color:{barva}; font-weight:900; line-height:1; text-shadow:0 0 10px {zare};
           font-size:clamp(2.5rem, 8vw, 6.5rem); }}
 .detail {{ margin:10px 0 0; color:#a1a1aa; font-size:clamp(.7rem,1.6vw,1rem);
            text-align:center; }}

 /* Tlačítko tokenu vypadá jako karta — na dotykovém displeji je to půlka
    obrazovky, ne malý knoflík. Odjištěné je červené a říká, co se stane. */
 .karta-tlacitko {{ border-color:#84cc16; background:rgba(132,204,22,.1); cursor:pointer;
                    font-family:inherit; color:#fff; text-align:center; transition:background .15s; }}
 .karta-tlacitko:hover {{ background:rgba(132,204,22,.2); }}
 .karta-tlacitko .ikona {{ color:#a3e635; line-height:1; font-size:clamp(2.5rem,6vw,4.5rem); }}
 .karta-tlacitko .nadpis {{ margin-top:4px; font-weight:900; letter-spacing:.03em;
                            color:#bef264; font-size:clamp(1.1rem,3vw,2.2rem); }}
 .karta-tlacitko.pozor {{ border-color:#ef4444; background:rgba(239,68,68,.14); }}
 .karta-tlacitko.pozor .ikona,
 .karta-tlacitko.pozor .nadpis {{ color:#fca5a5; }}

 /* Cesta průjezdu: smyčka → server. Dvě diody, mezi nimi šipka. */
 .cesta {{ display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:12px; }}
 .cesta .sipka {{ font-weight:900; color:#d4d4d8; font-size:clamp(2rem,5vw,4rem); }}
 .uzel {{ border-radius:20px; border:1px solid #84cc16; padding:16px; display:flex;
          align-items:center; gap:16px; min-height:105px; }}
 .uzel.server {{ border-color:#0ea5e9; }}
 .uzel .nazev {{ font-weight:900; text-transform:uppercase; letter-spacing:.1em;
                 font-size:clamp(.85rem,2vw,1.2rem); }}
 .uzel .hodnota {{ font-weight:900; color:#71717a; font-size:clamp(1rem,2.3vw,1.5rem); }}
 .uzel .hodnota.zeleno {{ color:#a3e635; }}
 .uzel .hodnota.modro {{ color:#38bdf8; }}
 .uzel .hodnota.oranzovo {{ color:#fbbf24; }}
 .uzel .pod {{ color:#a1a1aa; font-size:clamp(.65rem,1.4vw,.9rem); }}
 .dioda {{ width:28px; height:28px; border-radius:9999px; background:#24272a;
           border:2px solid #4b5563; flex:0 0 auto; transition:all .15s ease; }}
 .dioda.zelena {{ background:#67ff19; border-color:#c0ff9f;
   box-shadow:0 0 8px rgba(103,255,25,.95), 0 0 20px rgba(103,255,25,.8),
              0 0 34px rgba(103,255,25,.45); }}
 .dioda.modra {{ background:#28b7ff; border-color:#8fddff;
   box-shadow:0 0 8px rgba(40,183,255,.95), 0 0 20px rgba(40,183,255,.8),
              0 0 34px rgba(40,183,255,.45); }}
 .dioda.blik {{ animation:blik .5s ease-out; }}
 @keyframes blik {{
   0% {{ transform:scale(1); }}
   20% {{ transform:scale(1.45); filter:brightness(1.8); }}
   100% {{ transform:scale(1); }}
 }}

 /* Token: rámeček s tečkovaným rastrem a nadpisem posazeným do hrany. */
 .tokenblok {{ position:relative; border-radius:18px; border:2px solid #84cc16;
               padding:16px 20px;
               background-image: radial-gradient(rgba(98,255,30,.18) 1px, transparent 1px);
               background-size:7px 7px; }}
 .tokenblok .nadpis {{ position:absolute; top:-14px; left:50%; transform:translateX(-50%);
                       background:#050607; padding:0 14px; font-weight:900;
                       text-transform:uppercase; letter-spacing:.1em;
                       font-size:clamp(.8rem,1.8vw,1.15rem); white-space:nowrap; }}
 .tokenradek {{ display:flex; align-items:center; gap:16px; }}
 .tokenradek .zamek {{ color:#a3e635; font-size:clamp(1.6rem,3.5vw,3rem); }}
 /* Token se láme na dva řádky **po třech skupinách**, ne kdekoli: na
    3,5" displeji se celý na řádek nevejde a zlom uprostřed skupiny by se
    obsluze opisoval s chybou. Zlom nese text (\n) + `pre-line`. */
 code {{ display:block; flex:1; text-align:center; font-family: ui-monospace, "SF Mono", Menlo, monospace;
         font-weight:900; letter-spacing:.08em; color:#a3e635; white-space:pre-line;
         text-shadow:0 0 10px rgba(106,255,0,.5);
         font-size:clamp(1.1rem, 3.2vw, 2.4rem); line-height:1.15; }}

 /* Noha: poslední průjezd vlevo, čas obnovení vpravo. */
 .paticka {{ margin-top:auto; padding-top:12px; border-top:1px solid #27272a;
             display:flex; align-items:center; justify-content:space-between; gap:16px;
             color:#d4d4d8; font-size:clamp(.65rem,1.4vw,.95rem); }}
 .paticka strong {{ color:#fff; }}
 .paticka a {{ color:#93c5fd; font-weight:900; text-decoration:none; }}
 .paticka .vlevo {{ display:flex; align-items:center; gap:8px; min-width:0; }}

 /* Zahozené rámce. Červená proto, že je to **ztráta výsledku**, ne varování:
    dohledat se dá jen z dekodéru a jen dokud si je pamatuje. */
 .zahozeno {{ margin-top:10px; padding:8px 12px; border-radius:8px;
              background:#7f1d1d; color:#fff; font-weight:900;
              font-size:clamp(.6rem,1.3vw,.9rem); text-align:center; }}
 .zahozeno strong {{ color:#fff; }}

 /* Přeliv na disku. **Oranžová, ne červená**: nic se neztratilo, jen to
    čeká na server. Kdyby to svítilo červeně jako ztráta, obsluha by
    u každého výpadku wifi běžela k počítači zbytečně. */
 .preliv {{ margin-top:10px; padding:8px 12px; border-radius:8px;
            background:#78350f; color:#fed7aa; font-weight:900;
            font-size:clamp(.6rem,1.3vw,.9rem); text-align:center; }}
 .preliv strong {{ color:#fff; }}

 /* Fotofiniš kamera (3. 10. 2026) — tlumeně: je to informace, ne poplach. */
 .kamera {{ margin-top:10px; padding:8px 12px; border-radius:8px;
            background:#111827; color:#cbd5e1; font-weight:800;
            font-size:clamp(.6rem,1.3vw,.9rem); text-align:center; }}
 .kamera strong {{ color:#fff; }} .kamera .ok {{ color:#3fb950; }} .kamera .chyba {{ color:#f85149; }}

 /* Malé SPI displeje (MHS35: 480×320). Spodní mez clamp() je stavěná na
    monitor — tady by token, kvůli kterému displej existuje, skončil pod
    spodním okrajem. Ustupuje všechno kromě tokenu a diod. */
 @media (max-height: 420px) {{
   main {{ padding:4px; }}
   .ramecek {{ padding:6px; border-radius:14px; gap:6px; }}
   header {{ padding-bottom:4px; border-bottom-width:1px; }}
   h1 {{ font-size:1.25rem; letter-spacing:.08em; }}
   .hodiny .cas {{ font-size:1rem; }}
   .hodiny .datum {{ font-size:.58rem; margin-top:1px; }}
   .hodiny .verze {{ font-size:.52rem; margin-top:0; letter-spacing:.04em; }}
   .dvojice {{ gap:6px; }}
   .karta {{ min-height:0; padding:6px; border-radius:12px; border-width:1px; }}
   .karta .popisek {{ font-size:.6rem; }}
   .vysledek {{ margin-top:4px; gap:8px; }}
   .kolecko {{ width:2rem; height:2rem; border-width:2px; font-size:1rem; }}
   .slovo {{ font-size:1.7rem; }}
   .detail {{ display:none; }}
   .karta-tlacitko .ikona {{ font-size:1.5rem; }}
   .karta-tlacitko .nadpis {{ font-size:.72rem; margin-top:0; }}
   .cesta {{ gap:6px; }}
   .cesta .sipka {{ font-size:1.2rem; }}
   .uzel {{ min-height:0; padding:6px 8px; gap:8px; border-radius:12px; }}
   .uzel .nazev {{ font-size:.55rem; letter-spacing:.06em; }}
   .uzel .hodnota {{ font-size:.72rem; }}
   .uzel .pod {{ display:none; }}
   .dioda {{ width:16px; height:16px; }}
   /* Token dostane zbylé místo: na 3,5" displeji je to ta věc, kvůli které
      se na obrazovku kouká, a prázdná plocha pod ním by byla plýtvání. */
   .tokenblok {{ padding:8px 10px; border-radius:12px; flex:1;
     display:flex; align-items:center; }}
   .tokenblok .tokenradek {{ flex:1; }}
   .tokenblok .nadpis {{ top:-9px; font-size:.55rem; padding:0 8px; }}
   .tokenradek {{ gap:8px; }}
   .tokenradek .zamek {{ font-size:1rem; }}
   code {{ font-size:1.45rem; letter-spacing:.02em; }}
   .paticka {{ padding-top:5px; font-size:.55rem; gap:6px; }}
 }}
"""

#: Displej se **neobnovuje celou stránkou**. Návrh Ri5 v2 má živé hodiny
#: a diody, které bliknou na průjezd; při `meta refresh` po sekundě by
#: animace nikdy nedoběhla a obrazovka by problikávala. Stav se proto tahá
#: z `/stav` (JSON) a mění se jen to, co se změnilo.
_SCREEN = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{nadpis}</title>
<style>{styl}</style>
</head>
<body><main><section class="ramecek">

 <header>
  <h1>BIKODY<span class="tecka">.</span>COM</h1>
  <div class="hodiny">
   <div class="cas" id="cas">{cas_hodiny}</div>
   <div class="datum" id="datum">{cas_datum}</div>
   <div class="verze">AGENT <strong>{verze}</strong></div>
  </div>
 </header>

 <section class="dvojice">
  <div class="karta">
   <p class="popisek">Stav serveru</p>
   <div class="vysledek">
    <div class="kolecko" id="znak">{znak}</div>
    <div class="slovo" id="slovo">{slovo}</div>
   </div>
   <p class="detail" id="detail">{detail}</p>
  </div>
  {tlacitko}
 </section>

 <section class="cesta">
  <div class="uzel">
   <span class="{dioda_smycka}" id="dioda-smycka"></span>
   <div>
    <div class="nazev">Loop</div>
    <div class="{trida_smycka}" id="text-smycka">{text_smycka}</div>
    <div class="pod">INPUT FROM THE LOOP</div>
   </div>
  </div>
  <div class="sipka">&rarr;</div>
  <div class="uzel server">
   <span class="{dioda_server}" id="dioda-server"></span>
   <div>
    <div class="nazev">Server</div>
    <div class="{trida_server}" id="text-server">{text_server}</div>
    <div class="pod">DATA HANDED TO THE SERVER</div>
   </div>
  </div>
 </section>

 <section class="tokenblok">
  <div class="nadpis">{token_popisek}</div>
  <div class="tokenradek">
   <div class="zamek">&#128274;</div>
   <code id="token">{token}</code>
  </div>
 </section>

 <!-- Zahozené rámce: **jediná ztráta, kterou krabička nedohoní sama.**
      Kreslí se jen když k ní došlo, a hned s tím, co udělat — obsluha
      u trati je ten, kdo dohledání spouští, a bez věty „co teď" je hlášení
      jen zlá zpráva. -->
 <div id="zahozeno-pruh" class="zahozeno" hidden>
  &#9888;&nbsp;DROPPED <strong id="zahozeno-pocet">0</strong> FRAMES
  (<span id="zahozeno-kdy"></span>) — IN THE APP, RE-READ THE PASSINGS FROM THE DECODER
 </div>

 <!-- Přeliv: rámce leží na disku a čekají, až server začne brát. Obsluha
      nemá nikam běžet — tohle je informace „nic se neztratilo", ne úkol.
      Proto oranžově a s větou o tom, že to doletí samo. -->
 <div id="preliv-pruh" class="preliv" hidden>
  &#8987;&nbsp;<strong id="preliv-pocet">0</strong> FRAMES WAITING ON DISK —
  THEY GO OUT BY THEMSELVES ONCE THE SERVER ANSWERS
 </div>

 <!-- Fotofiniš kamera (3. 10. 2026): jen když je nastavená. Říká, jestli
      software OPTIc3 přijímá startovky a kdy odešla poslední. -->
 <div id="kamera-pruh" class="kamera" hidden>
  &#128247;&nbsp;CAMERA <strong id="kamera-adresa"></strong> —
  <span id="kamera-stav"></span><span id="kamera-posledni"></span>
 </div>

 <footer class="paticka">
  <span class="vlevo">&#8635;&nbsp;LAST PASSING: <strong id="posledni">{posledni}</strong></span>
  <span>AGENT <strong id="verze">{verze}</strong></span>
  <a href="/nastaveni">SETTINGS</a>
 </footer>

</section></main>
<script>
// Živý displej: hodiny tikají v prohlížeči, ostatní se tahá z /stav.
// Celá stránka se neobnovuje — animace diod by při obnovení po sekundě
// nikdy nedoběhla a token by problikával.
(function () {{
  "use strict";
  var LED_MS = 2200;

  function dvojmistne(cislo) {{ return (cislo < 10 ? "0" : "") + cislo; }}

  function tik() {{
    var now = new Date();
    var cas = dvojmistne(now.getHours()) + ":" + dvojmistne(now.getMinutes())
            + ":" + dvojmistne(now.getSeconds());
    var datum = dvojmistne(now.getDate()) + "." + dvojmistne(now.getMonth() + 1)
              + "." + now.getFullYear();
    document.getElementById("cas").textContent = cas;
    document.getElementById("datum").textContent = datum;
    return datum + " " + cas;
  }}

  var stav = {{ smycka: false, server: false }};

  function dioda(id, trida, sviti) {{
    var el = document.getElementById(id);
    if (!el) return;
    var chtene = sviti ? "dioda " + trida : "dioda";
    if (el.className.indexOf(trida) === -1 && sviti) chtene += " blik";
    el.className = chtene;
  }}

  function napis(id, text, trida) {{
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = "hodnota" + (trida ? " " + trida : "");
  }}

  function nakresli(data) {{
    dioda("dioda-smycka", "zelena", data.smycka);
    dioda("dioda-server", "modra", data.server);
    napis("text-smycka", data.smycka ? "SIGNAL RECEIVED" : "WAITING FOR A PASSING",
          data.smycka ? "zeleno" : "");
    if (data.server) napis("text-server", "SENT", "modro");
    else if (data.smycka) napis("text-server", "SENDING…", "oranzovo");
    else napis("text-server", "READY", "");

    document.getElementById("posledni").textContent = data.posledni || "--:--:--";
    // Ztráta **není puls**: pruh zůstane svítit, dokud agent běží. Zmizet
    // po dvou sekundách jako dioda by z ní udělalo něco, co se dá přehlédnout.
    var zahozeno = Number(data.zahozeno || 0);
    var pruh = document.getElementById("zahozeno-pruh");
    pruh.hidden = zahozeno <= 0;
    if (zahozeno > 0) {{
      document.getElementById("zahozeno-pocet").textContent = String(zahozeno);
      document.getElementById("zahozeno-kdy").textContent = data.zahozeno_kdy || "";
    }}
    // Přeliv naopak **je** dočasný: až se server ozve, číslo padá k nule
    // a pruh zmizí. Že mizí sám, je ta informace.
    var preliv = Number(data.preliv || 0);
    var prelivPruh = document.getElementById("preliv-pruh");
    prelivPruh.hidden = preliv <= 0;
    if (preliv > 0) {{
      document.getElementById("preliv-pocet").textContent = String(preliv);
    }}
    var kamera = data.kamera;
    var kameraPruh = document.getElementById("kamera-pruh");
    if (kameraPruh) {{
      kameraPruh.hidden = !kamera;
      if (kamera) {{
        document.getElementById("kamera-adresa").textContent = kamera.adresa;
        var kStav = document.getElementById("kamera-stav");
        kStav.textContent = kamera.ok === true ? "OPTIc3 LISTENING"
          : (kamera.ok === false ? "OPTIc3 NOT LISTENING" : "NOT TESTED");
        kStav.className = kamera.ok === true ? "ok" : (kamera.ok === false ? "chyba" : "");
        document.getElementById("kamera-posledni").textContent =
          kamera.posledni ? " · LAST START LIST " + kamera.posledni : "";
      }}
    }}
    document.getElementById("znak").textContent = data.znak;
    document.getElementById("slovo").textContent = data.slovo;
    document.getElementById("detail").textContent = data.detail;
    document.getElementById("token").textContent = data.token;
    document.getElementById("verze").textContent = data.verze;
    tik();

    // Barva stavu serveru se mění podle toho stavu, takže ji nese odpověď
    // `/stav`, ne jen styl vygenerovaný při prvním načtení stránky.
    document.querySelectorAll(".karta:first-child .kolecko, .karta:first-child .slovo")
      .forEach(function (el) {{ el.style.color = data.barva; }});
    var kolecko = document.querySelector(".karta:first-child .kolecko");
    if (kolecko) kolecko.style.borderColor = data.barva;
    var karta = document.querySelector(".karta:first-child");
    if (karta) karta.style.borderColor = data.barva;

    var tlacitko = document.getElementById("token-tlacitko");
    if (tlacitko) {{
      tlacitko.className = "karta karta-tlacitko" + (data.odjisteno ? " pozor" : "");
      var nadpis = tlacitko.querySelector(".nadpis");
      if (nadpis) {{
        nadpis.textContent = data.odjisteno
          ? "TAP AGAIN — THE OLD TOKEN STOPS WORKING"
          : "NEW TOKEN";
      }}
    }}
  }}

  function ptej() {{
    fetch("/stav", {{ cache: "no-store" }})
      .then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (data) {{ if (data) nakresli(data); }})
      .catch(function () {{}});
  }}

  tik();
  window.setInterval(tik, 1000);
  ptej();
  window.setInterval(ptej, 1000);
}})();
</script>
</body></html>"""

_SETTINGS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings — trackside agent</title>
<style>
 :root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
 body {{ margin:0; background:#0b0f14; color:#e6edf3; display:flex; justify-content:center; }}
 main {{ width:min(560px,92vw); padding:24px 0 40px; }}
 h1 {{ font-size:20px; margin:24px 0 4px; }}
 label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.08em;
          color:#8b98a5; margin:16px 0 6px; }}
 input[type=text] {{ width:100%; height:40px; padding:0 12px; border-radius:10px; box-sizing:border-box;
         border:1px solid #1e2a36; background:#0d141b; color:#e6edf3; font-size:15px; }}
 .radek {{ display:flex; align-items:center; gap:8px; margin-top:16px; font-size:14px; color:#c9d5e1; }}
 button {{ margin-top:20px; height:40px; padding:0 20px; border-radius:10px; border:0;
           background:#2f81f7; color:#fff; font-weight:600; font-size:15px; cursor:pointer; }}
 p.hint {{ color:#8b98a5; font-size:12px; line-height:1.5; }}
 code {{ background:#0d141b; padding:2px 5px; border-radius:5px; font-size:12px; }}
 table {{ width:100%; border-collapse:collapse; margin-top:24px; font-size:13px; }}
 th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.08em;
       color:#8b98a5; font-weight:600; padding:0 0 6px; }}
 td {{ padding:6px 0; border-top:1px solid #1e2a36; color:#c9d5e1; }}
 td.stavbunka {{ color:#3fb950; }} td.stavbunka.chyba {{ color:#f85149; }}
 a {{ color:#2f81f7; }}
 .panel {{ margin-top:20px; padding:14px; border:1px solid #1e2a36; border-radius:12px;
           background:#0d141b; font-size:13px; line-height:1.55; }}
 .panel strong {{ color:#fff; }} .ok {{ color:#3fb950; }} .chyba {{ color:#f85149; }}
</style></head>
<body><main>
 <h1>Trackside agent — settings</h1>
 <p class="hint">{stav}</p>
 <form method="post">
  <label for="server">Application address</label>
  <input type="text" id="server" name="server" value="{server}">
  <p class="hint" style="margin:-8px 0 14px">Pre-filled; change it only for your own server.</p>
  <div class="radek">
   <input type="checkbox" id="autostart" name="autostart" {autostart}>
   <label for="autostart" style="margin:0;text-transform:none;letter-spacing:0;font-size:14px">
     Start when the computer boots</label>
  </div>
  <div class="radek">
   <input type="checkbox" id="novytoken" name="novytoken">
   <label for="novytoken" style="margin:0;text-transform:none;letter-spacing:0;font-size:14px">
     Issue a new token (the old one stops working)</label>
  </div>
 <button type="submit">Save</button>
 </form>
 <p class="hint">Box token: <code>{token}</code><br>
   Copy it in the app into <strong>Application settings → Sign in the box</strong>.
   It is stored in <code>{config}</code>.</p>

 <h1 style="font-size:15px;margin-top:28px">Box diagnostics</h1>
 <p class="hint" style="margin-top:4px">Checks the interfaces, broadcast
   addresses, decoder reply port and the computer clock — nothing is erased.</p>
 <form method="post" action="/diagnostika"><button type="submit">Run diagnostics</button></form>
 {diagnostika}

 <h1 style="font-size:15px;margin-top:28px">Update</h1>
 <p class="hint" style="margin-top:4px">Installed <strong>{verze}</strong>, the server offers
   <strong>{nova_verze}</strong>. The original file is kept as <code>.bak</code>.</p>
 {aktualizace_tlacitko}
 {aktualizace_stav}

 <h1 style="font-size:15px;margin-top:28px">Try a connection to the hardware</h1>
 <p class="hint" style="margin-top:4px">Checks the cable and the address <strong>without the
   server</strong> — type the decoder or camera address. The result is added to the table below.</p>
 <form method="post" action="/zkusit">
  <label for="host">Address and port</label>
  <div class="radek" style="margin-top:0">
   <input type="text" id="host" name="host" value="{zkouska_host}" placeholder="192.168.9.25"
          style="flex:1">
   <input type="text" name="port" value="{zkouska_port}" placeholder="5403"
          style="width:96px">
  </div>
  <button type="submit">Try</button>
 </form>

 <h1 style="font-size:15px;margin-top:28px">Photo-finish camera</h1>
 <p class="hint" style="margin-top:4px">Start lists go to the <strong>OPTIc3 software</strong>
   on the camera computer (XML socket, default port 4532) — not to the camera head itself.
   The address is stored in the app for the whole organisation.</p>
 {kamera}

 <h1 style="font-size:15px;margin-top:28px">Service (recommended for the box)</h1>
 <p class="hint" style="margin-top:4px">{sluzba}</p>
 {spojeni}
 <p class="hint"><a href="/">back to the display</a></p>
</main></body></html>"""


#: Poslední ručně zkoušená adresa — displej ji nabídne znovu, obsluha
#: u trati nemá překlepávat IP dekodéru dvakrát.
_posledni_zkouska: dict = {"host": "", "port": ""}
_diagnostika_snapshot: dict | None = None
_aktualizace_stav = ""


def _diagnostika_html() -> str:
    if _diagnostika_snapshot is None:
        return ""
    data = _diagnostika_snapshot
    interfaces = html.escape(", ".join(
        f"{row['address']}/{row['prefix']}" for row in data.get("interfaces", [])
    ) or "no IPv4 interface")
    broadcasts = html.escape(", ".join(data.get("broadcasts", [])) or "none")
    port_class = "ok" if data.get("reply_port_ok") else "chyba"
    port_text = "free" if data.get("reply_port_ok") else (
        html.escape(str(data.get("reply_port_error") or "in use"))
    )
    return (
        '<div class="panel">'
        f"<strong>Interfaces:</strong> {interfaces}<br>"
        f"<strong>Broadcast:</strong> {broadcasts}<br>"
        f'<strong>UDP 5303:</strong> <span class="{port_class}">{port_text}</span><br>'
        f"<strong>Box clock:</strong> {html.escape(str(data.get('local', '—')))}"
        "</div>"
    )


def _stage_update(worker) -> str:
    """Stáhne, ověří a atomicky připraví nový soubor agenta."""
    if worker is None or not worker.connected:
        return "The update cannot be downloaded — the box is not connected to the server."
    if getattr(sys, "frozen", False):
        return "A packaged application cannot be updated as a Python file."
    try:
        payload = worker.server.download_agent()
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return f"The update could not be downloaded: {exc}"
    expected = str(getattr(worker, "latest_sha256", "") or "").lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        return "Update refused: the server sent no valid checksum."
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        return "Update refused: the checksum does not match."
    try:
        compile(payload, "track_agent.py", "exec")
    except SyntaxError as exc:
        return f"Update refused: the downloaded program is not valid ({exc})."
    current = pathlib.Path(__file__).resolve()
    backup = current.with_suffix(current.suffix + ".bak")
    staged = current.with_suffix(current.suffix + ".new")
    try:
        staged.write_bytes(payload)
        if current.exists():
            backup.write_bytes(current.read_bytes())
        staged.replace(current)
    except OSError as exc:
        return f"The update could not be saved: {exc}"
    return "The update is verified and saved. It takes effect after the service restarts."


def _recent_table() -> str:
    """Poslední spojení na dekodéry a kameru — co je vidět v nastavení.

    Každá buňka se escapuje: `cil` i `detail` nesou text zvenčí (adresa
    ze zkoušky spojení, text výjimky, příkaz ze serveru) — audit 23. 9. 2026.
    """
    if not _recent:
        return ""
    rows = "".join(
        "<tr><td>{cas}</td><td>{cil}</td>"
        '<td class="stavbunka{trida}">{text}</td></tr>'.format(
            cas=html.escape(str(entry["cas"])),
            cil=html.escape(str(entry["cil"])),
            trida="" if entry["ok"] else " chyba",
            text="answered" if entry["ok"] else html.escape(str(entry["detail"] or "no answer")),
        )
        for entry in _recent
    )
    return "<table><tr><th>When</th><th>Where</th><th>Result</th></tr>" + rows + "</table>"


def _screen_state(worker, config: dict) -> dict:
    """Co má být na displeji: velký stav a vždy celý, opisovatelný token.

    David 24. 8. 2026 výslovně požaduje celý token i po spárování. Na malém
    displeji je to hlavní provozní informace; bezpečnost síťových příkazů proto
    stojí i na omezení cílů na privátní IPv4 rozsahy (bez loopbacku). Celý
    token ale dostane jen tenhle počítač — viz `_token_pro_klienta`.

    **Zelené OK jen nad živým vláknem** (audit 23. 9. 2026): `connected`
    zůstalo `True` i po smrti vlákna agenta a displej svítil OK nad krabičkou,
    která nic nepřeposílala.
    """
    zije = getattr(worker, "is_running", None)
    connected = bool(worker and worker.connected
                     and (zije() if callable(zije) else True))
    token = (config.get("token") or "").strip()

    if connected:
        return {
            "barva": "#84cc16", "zare": "rgba(80,255,30,.35)",
            "znak": "✓", "slovo": "OK",
            "detail": worker.status if worker else "",
            "token_popisek": "Box token:",
            "token": token or "—",
        }
    if not configured_server(config):
        return {
            "barva": "#eab308", "zare": "rgba(234,179,8,.35)",
            "znak": "!", "slovo": "SET UP",
            "detail": "The application address is missing in the box settings.",
            "token_popisek": "Box token:", "token": token or "—",
        }
    # Červeně, ne žlutě: ČEKÁ znamená „ještě to nejede" a od zeleného OK se
    # musí lišit na první pohled i přes půlku závodiště. Žlutá zůstává
    # výjimečnému NASTAVIT.
    return {
        "barva": "#ef4444", "zare": "rgba(239,68,68,.35)",
        "znak": "…", "slovo": "WAITING",
        "detail": (worker.status if worker else "waiting for approval in the app"),
        "token_popisek": "Copy the token into the app — no dashes:",
        "token": token or "—",
    }


def _token_text(token: str) -> str:
    """Token na displeji: dva řádky po třech čtveřicích.

    Na jednom řádku se 24 znaků na 3,5" displej nevejde čitelně; na dvou
    unese písmo skoro dvojnásobný stupeň. Pomlčkami nedělený text zůstává na
    jednom řádku.

    Zlom je **obyčejný „\n"**, ne `<br>`: obrazovka si stav tahá JSONem
    a text sází přes `textContent`, kde by značka byla vidět jako text.
    Zalomení kreslí CSS (`white-space: pre-line`).
    """
    groups = token.split("-")
    if len(groups) < 4:
        return token
    half = (len(groups) + 1) // 2
    return "-".join(groups[:half]) + "\n" + "-".join(groups[half:])


#: Cesty POST, které **mění stav krabičky** — adresu serveru, token,
#: připravenou aktualizaci. Ty smí jen z tohohle počítače.
#:
#: Proč (2. 9. 2026): stránka je displej krabičky a služba ji pouští na
#: `0.0.0.0`, aby se na ni dalo koukat z notebooku
#: (`deploy/systemd/bikody-agent.service`). Ověření ale žádné neměla,
#: takže **kdokoli na klubové síti mohl agenta přepojit na svůj server** —
#: `save_config(server_url, …)` a hned `Worker(server_url, token).start()`.
#: Token i průjezdy by pak šly jemu. Jištění dvěma klepnutími chrání jen
#: záměnu tokenu, a jen proti náhodnému doteku na displeji.
#:
#: Čtení a **diagnostika zůstávají odkudkoli**: displej má fungovat
#: z notebooku a „Diagnostika sítě" obsluha před závodem potřebuje. Nic
#: z toho nemění, na co je agent připojený.
#:
#: **„Zkusit spojení" už jen místně** (audit 23. 9. 2026): jeho adresa se
#: ukládá a vrací do stránky, takže byla vstupem pro uložené XSS, a z LAN
#: se přes něj dala krabičkou oskenovat klubová síť. Escapování XSS zavírá;
#: tohle zavírá i to skenování. Z notebooku se spojení ověří z aplikace.
POST_JEN_MISTNE = ("/nastaveni", "/novy-token", "/aktualizovat", "/zkusit", "/kamera")

#: Strop těla POST (audit 23. 9. 2026). Formuláře krabičky mají desítky
#: bajtů; `Content-Length` z hlavičky se dřív četl bez meze celý do paměti.
MAX_TELO_POST = 64 * 1024

#: Kolik vteřin smí klient mlčet uprostřed požadavku. Bez meze by pár
#: otevřených a mlčících spojení drželo vlákna serveru napořád.
WEB_TIMEOUT_S = 15.0

#: Hlavička stránek krabičky. Skripty a styly jsou inline (displej nesmí
#: záviset na CDN), takže `'unsafe-inline'` zůstává; zbytek zakazuje cizí
#: zdroje, vložení stránky do rámu a odeslání formuláře jinam.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def _je_z_tohoto_pocitace(adresa: str) -> bool:
    """Přišel požadavek z loopbacku? Prázdné nebo nečitelné = ne."""
    if not adresa:
        return False
    try:
        return ipaddress.ip_address(adresa.strip("[]")).is_loopback
    except ValueError:
        return False


def _meni_stav(cesta: str) -> bool:
    """Mění tenhle POST nastavení krabičky?

    Kromě vyjmenovaných cest sem patří i **prázdná cesta a `/`**: uložení
    nastavení je propad na konci `do_POST`, takže formulář z hlavní stránky
    by se jinak protáhl bez kontroly.
    """
    if any(cesta.startswith(p) for p in POST_JEN_MISTNE):
        return True
    return not cesta.startswith("/diagnostika")


def _cizi_puvod(hlavicky) -> bool:
    """Přišel POST z cizí stránky (CSRF)?

    Kontrola loopbacku nestačí (audit 23. 9. 2026): cizí web otevřený
    v prohlížeči **na krabičce** pošle formulář z `127.0.0.1` taky. Prohlížeč
    ale přiloží `Origin` / `Sec-Fetch-Site`, a ty musí sedět na tuhle
    stránku. Bez hlaviček (curl, skripty na krabičce) se propouští —
    to není prohlížeč, který by šlo podvrhnout cizí stránkou.
    """
    web = (hlavicky.get("Sec-Fetch-Site") or "").strip().lower()
    if web in ("cross-site", "same-site"):
        return True
    puvod = (hlavicky.get("Origin") or "").strip()
    if not puvod:
        return False
    if puvod.lower() == "null":
        return True
    cast = urllib.parse.urlsplit(puvod)
    host = (hlavicky.get("Host") or "").strip().lower()
    return cast.scheme not in ("http", "https") or cast.netloc.lower() != host


def _novy_token_odjisten() -> bool:
    return (time.monotonic() - _novy_token_pozadan) <= NOVY_TOKEN_POTVRZENI_S


def _tlacitko_html() -> str:
    """Tlačítko „Nový token" — v návrhu Ri5 v2 je to celá polovina obrazovky.

    **Jištění dvěma klepnutími zůstává.** Návrh má jedno tlačítko, ale nový
    token okamžitě odpojí krabičku od aplikace a obsluha ho má opsaný —
    náhodný dotek na displeji u trati by závod odstřihl od časomíry. První
    klepnutí proto tlačítko zčervená a řekne, co se stane; druhé ve lhůtě
    token vydá. Po jejím uplynutí se samo vrátí do klidu (displej se ptá
    na `/stav` každou sekundu).
    """
    odjisteno = _novy_token_odjisten()
    trida = "karta karta-tlacitko pozor" if odjisteno else "karta karta-tlacitko"
    nadpis = (
        "TAP AGAIN — THE OLD TOKEN STOPS WORKING" if odjisteno else "NEW TOKEN"
    )
    return (
        '<form method="post" action="/novy-token" style="display:contents">'
        f'<button type="submit" id="token-tlacitko" class="{trida}">'
        '<span class="ikona">&#8635;</span>'
        f'<span class="nadpis">{nadpis}</span>'
        "</button></form>"
    )


def _token_pro_klienta(token: str, plny: bool) -> str:
    """Celý token jen tomuhle počítači, ostatním maskovaný.

    Kiosk displeje krabičky běží na `127.0.0.1` (`kiosk/start-kiosk.sh`)
    a celý token dál ukazuje (David 24. 8. 2026). Stránka ale poslouchá
    na `0.0.0.0`, aby se na ni dalo koukat z notebooku — a do 1.14 tak
    dostal celý token **kdokoli v klubové síti** přes `/stav` (audit
    23. 9. 2026). Stejně to dělá Edge.
    """
    return token if plny else maskuj_token(token)


def _kamera_pro_displej() -> dict | None:
    """Řádek kamery na displeji — jen když je kamera nastavená."""
    stav = stav_kamery()
    if not stav.get("host"):
        return None
    last = stav.get("last") or {}
    return {
        "adresa": f"{stav['host']}:{stav.get('port') or 4532}",
        "ok": stav.get("ok"),
        "posledni": last.get("sent_at") or "",
        "jizda": last.get("heat") or "",
    }


def _stav_json(worker, config: dict, *, plny_token: bool = False) -> bytes:
    """Stav displeje jako JSON — z něj si obrazovka bere všechno živé.

    Displej se **neobnovuje celou stránkou** (návrh Ri5 v2 má tikající hodiny
    a diody, které bliknou na průjezd; obnovení po sekundě by animaci nikdy
    nenechalo doběhnout). Odpovídá se proto malým JSONem a mění se jen to,
    co se opravdu změnilo.
    """
    import json

    state = _screen_state(worker, config)
    prujezdy = _prujezdy_stav()
    return json.dumps(
        {
            "barva": state["barva"],
            "znak": state["znak"],
            "slovo": state["slovo"],
            "detail": state["detail"],
            "token": _token_text(_token_pro_klienta(state["token"], plny_token)),
            "token_popisek": state["token_popisek"],
            "smycka": prujezdy["smycka"],
            "server": prujezdy["server"],
            "celkem": prujezdy["celkem"],
            "posledni": prujezdy["naposledy"],
            "preliv": prujezdy["preliv"],
            "zahozeno": prujezdy["zahozeno"],
            "zahozeno_kdy": prujezdy["zahozeno_kdy"],
            "odjisteno": _novy_token_odjisten(),
            "verze": VERSION,
            "kamera": _kamera_pro_displej(),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _render_screen(worker, config: dict, *, plny_token: bool = False) -> bytes:
    # Všechno, co se sází do HTML, se escapuje (audit 23. 9. 2026): `detail`
    # nese stav agenta a ten text výjimek a odpovědí serveru.
    state = _screen_state(worker, config)
    style = _STYLE.format(barva=state["barva"], zare=state["zare"])
    prujezdy = _prujezdy_stav()
    now = time.localtime()
    page = _SCREEN.format(
        nadpis="BIKODY.COM — trackside box",
        styl=style,
        # První vykreslení nese stav diod samo: než dojde první odpověď
        # `/stav`, byla by obrazovka po restartu kiosku vždycky tmavá —
        # i uprostřed závodu, kdy průjezdy chodí.
        dioda_smycka="dioda zelena" if prujezdy["smycka"] else "dioda",
        dioda_server="dioda modra" if prujezdy["server"] else "dioda",
        text_smycka="SIGNAL RECEIVED" if prujezdy["smycka"] else "WAITING FOR A PASSING",
        trida_smycka="hodnota zeleno" if prujezdy["smycka"] else "hodnota",
        text_server=(
            "SENT" if prujezdy["server"]
            else ("SENDING…" if prujezdy["smycka"] else "READY")
        ),
        trida_server=(
            "hodnota modro" if prujezdy["server"]
            else ("hodnota oranzovo" if prujezdy["smycka"] else "hodnota")
        ),
        znak=html.escape(state["znak"]),
        slovo=html.escape(state["slovo"]),
        detail=html.escape(str(state["detail"])),
        token_popisek=html.escape(state["token_popisek"]),
        token=html.escape(_token_text(_token_pro_klienta(state["token"], plny_token))),
        tlacitko=_tlacitko_html(),
        posledni=html.escape(prujezdy["naposledy"] or "--:--:--"),
        cas=time.strftime("%d.%m.%Y %H:%M:%S", now),
        cas_hodiny=time.strftime("%H:%M:%S", now),
        cas_datum=time.strftime("%d.%m.%Y", now),
        verze=VERSION,
    )
    return page.encode("utf-8")


def _service_hint() -> str:
    """Co na displeji stojí o službě — podle toho, jestli je nainstalovaná.

    Krabička u trati musí po pádu i po restartu vstát sama; „spouštět po
    startu" je na Linuxu jen soubor plochy a spadlý proces nikdo nezvedne.
    """
    if not sys.platform.startswith("linux"):
        return (
            "On this system the service is not installed — the agent is held by the "
            "\"Start when the computer boots\" option above."
        )
    state = service_state()
    if not state:
        return (
            "Not installed. The agent will not come back after a crash. On the box, "
            "turn it on with <code>sudo python3 track_agent.py "
            "--install-service</code> — then it rises after a crash and a reboot."
        )
    if state == "active":
        return "Running as a service (<code>Restart=always</code>) — it returns after a crash and a reboot."
    return (
        f"Installed, but systemd reports <code>{html.escape(state)}</code>. "
        "Log: <code>journalctl -u bikody-agent.service -f</code>"
    )


def _render_settings(worker, config: dict, *, plny_token: bool = False) -> bytes:
    """Stránka nastavení. **Každá vložená hodnota se escapuje.**

    Do 1.14 šla adresa ze „Zkusit spojení" do `value="…"` tak, jak přišla,
    a kdokoli v síti tak do stránky vložil vlastní skript (uložené XSS,
    audit 23. 9. 2026). Ven sem teče i stav agenta (text výjimek), verze
    od serveru a tabulka spojení.
    """
    latest = getattr(worker, "latest_version", VERSION) if worker else VERSION
    update_available = latest and latest != VERSION
    update_button = (
        '<form method="post" action="/aktualizovat"><button type="submit">'
        "Download and stage the update</button></form>"
        if update_available else '<p class="hint ok">The agent is up to date.</p>'
    )
    page = _SETTINGS.format(
        stav=html.escape(str(worker.status if worker else "not started")),
        server=html.escape(configured_server(config)),
        autostart="checked" if config.get("autostart") else "",
        token=html.escape(_token_pro_klienta(config.get("token") or "—", plny_token)),
        config=html.escape(str(config_path())),
        sluzba=_service_hint(),
        zkouska_host=html.escape(str(_posledni_zkouska.get("host", ""))),
        zkouska_port=html.escape(str(_posledni_zkouska.get("port", ""))),
        spojeni=_recent_table(),
        diagnostika=_diagnostika_html(),
        verze=html.escape(VERSION),
        nova_verze=html.escape(str(latest or "unknown")),
        aktualizace_tlacitko=update_button,
        aktualizace_stav=(
            f'<div class="panel">{html.escape(_aktualizace_stav)}</div>'
            if _aktualizace_stav else ""
        ),
        kamera=_kamera_html(),
    )
    return page.encode("utf-8")


def _kamera_html() -> str:
    """Sekce kamery v nastavení. **Každá hodnota se escapuje** (jako zbytek)."""
    stav = stav_kamery()
    e = html.escape
    if stav.get("ok") is True:
        vysledek = f'<span class="ok">OPTIc3 is listening</span> ({e(stav.get("kdy") or "")})'
    elif stav.get("ok") is False:
        vysledek = (f'<span class="chyba">Nobody is listening — enable the XML socket in OPTIc3 '
                    f'(ATL Exchange Settings)</span><br>{e(stav.get("chyba") or "")} '
                    f'({e(stav.get("kdy") or "")})')
    else:
        vysledek = "Not tested yet."
    last = stav.get("last") or {}
    posledni = (f'{e(last.get("sent_at", ""))} — {e(last.get("heat", ""))}'
                f' ({e(last.get("event", ""))})' if last else "none yet")
    prenos = ('<span class="ok">on</span>' if stav.get("export")
              else '<span class="chyba">off — switch it on in the app (organisation settings)</span>')
    zprava = f'<div class="panel">{e(stav["zprava"])}</div>' if stav.get("zprava") else ""
    return f"""<div class="panel">
  <strong>Status:</strong> {vysledek}<br>
  <strong>Sending start lists:</strong> {prenos}<br>
  <strong>Last start list:</strong> {posledni}
 </div>
 {zprava}
 <form method="post" action="/kamera">
  <label for="kamera-host">OPTIc3 computer — address and port</label>
  <div class="radek" style="margin-top:0">
   <input type="text" id="kamera-host" name="host" value="{e(str(stav.get("host") or ""))}"
          placeholder="192.168.9.33" style="flex:1">
   <input type="text" name="port" value="{e(str(stav.get("port") or 4532))}" style="width:96px">
  </div>
  <button type="submit">Save to the app</button>
 </form>
 <form method="post" action="/kamera-test" style="display:inline-block;margin-right:8px">
  <button type="submit">Test the connection</button></form>
 <form method="post" action="/kamera-znovu" style="display:inline-block">
  <button type="submit">Resend the last start list</button></form>"""


def build_web_server(state: dict, *, host: str, port: int):
    """Displej a nastavení krabičky. `state` drží workera, ať jde vyměnit za chodu."""
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        # `StreamRequestHandler` nastaví soketu timeout (audit 23. 9. 2026).
        timeout = WEB_TIMEOUT_S

        def log_message(self, *_args):
            pass                       # vlastní log stačí, přístupy nikoho nezajímají

        def _mistni(self) -> bool:
            return _je_z_tohoto_pocitace(
                self.client_address[0] if self.client_address else ""
            )

        def handle(self):
            """Klient, který odejde uprostřed odpovědi, není chyba.

            Displej krabičky si stránku obnovuje sám a prohlížeč v kiosku se
            po restartu odpojí bez rozloučení. Standardní knihovna z toho
            sype dvacetiřádkový traceback do logu — a v logu krabičky u trati
            má být vidět, co dělají dekodéry, ne tohle.
            """
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _send(self, body: bytes, status: int = 200, headers=()):
            self.send_response(status)
            # Content-Type se posílá **jednou**: `/stav` vrací JSON a dvě
            # hlavičky se stejným jménem by prohlížeč vyhodnotil podle první,
            # tedy jako HTML.
            hlavicky = list(headers)
            if not any(name.lower() == "content-type" for name, _ in hlavicky):
                hlavicky.insert(0, ("Content-Type", "text/html; charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            if not any(name.lower() == "content-security-policy" for name, _ in hlavicky):
                hlavicky.append(("Content-Security-Policy", CSP))
                hlavicky.append(("X-Content-Type-Options", "nosniff"))
            for name, value in hlavicky:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):              # noqa: N802 — jméno určuje knihovna
            config = load_config()
            mistni = self._mistni()
            if self.path.startswith("/nastaveni"):
                self._send(_render_settings(state.get("worker"), config, plny_token=mistni))
                return
            if self.path.startswith("/stav"):
                # Živý stav displeje. Bez cache: obrazovka se ptá po sekundě
                # a kiosk prohlížeč by jinak servíroval první odpověď pořád.
                self._send(
                    _stav_json(state.get("worker"), config, plny_token=mistni),
                    headers=[
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Cache-Control", "no-store"),
                    ],
                )
                return
            self._send(_render_screen(state.get("worker"), config, plny_token=mistni))

        def _odmitni(self, status: int, text: str) -> None:
            # Tělo se nečte, takže spojení se po odpovědi zavře — zbytek
            # dat v soketu by se jinak přečetl jako další požadavek.
            self.close_connection = True
            self._send(
                text.encode("utf-8"),
                status=status,
                headers=[("Content-Type", "text/plain; charset=utf-8"),
                         ("Connection", "close")],
            )

        def do_POST(self):             # noqa: N802
            global _aktualizace_stav, _diagnostika_snapshot
            # Kontroly **před čtením těla** (audit 23. 9. 2026): odmítnutý
            # požadavek nemá co krabičce posílat do paměti. Do 1.14 se
            # nejdřív četlo celé tělo podle hlavičky, bez stropu.
            if _meni_stav(self.path) and not self._mistni():
                log(f"Odmítnut zápis z {self.client_address[0]}: {self.path}")
                self._odmitni(
                    403,
                    "The box settings are changed on the box itself. "
                    "From another computer the page is read-only.",
                )
                return
            if _cizi_puvod(self.headers):
                log(f"Odmítnut POST z cizí stránky ({self.headers.get('Origin')}): {self.path}")
                self._odmitni(403, "Cross-origin requests are not accepted.")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._odmitni(400, "Invalid Content-Length.")
                return
            if length < 0 or length > MAX_TELO_POST:
                self._odmitni(413, "The request is too large.")
                return
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", errors="replace")
            )
            saved = load_config()

            if self.path.startswith("/diagnostika"):
                _diagnostika_snapshot = network_info({})
                self._send(b"", status=303, headers=[("Location", "/nastaveni")])
                return

            if self.path.startswith("/aktualizovat"):
                _aktualizace_stav = _stage_update(state.get("worker"))
                self._send(b"", status=303, headers=[("Location", "/nastaveni")])
                return

            if self.path.startswith("/kamera"):
                # Kamera (3. 10. 2026): uložení adresy do aplikace, zkouška
                # spojení na OPTIc3 a znovuposlání poslední startovky.
                worker = state.get("worker")
                server = getattr(worker, "server", None)
                try:
                    if self.path.startswith("/kamera-test"):
                        obnov_kameru(worker)
                        _uloz_stav_kamery(zprava="")
                    elif self.path.startswith("/kamera-znovu"):
                        if server is None:
                            raise OSError("the box is not connected to the app")
                        odpoved = server.camera_resend()
                        _uloz_stav_kamery(zprava=(
                            f"Start list sent: {odpoved.get('heat', '')}" if odpoved.get("ok")
                            else f"Not sent: {odpoved.get('error', '')}"))
                        obnov_kameru(worker)
                    else:
                        host = (form.get("host", [""])[0] or "").strip()
                        port = int((form.get("port", ["4532"])[0] or "4532").strip())
                        _validated_target(host, port)
                        if server is None:
                            raise OSError("the box is not connected to the app")
                        _uloz_stav_kamery(server.camera_save(host, port), zprava="Saved to the app.")
                        ok, chyba = zkus_kameru(host, port)
                        _uloz_stav_kamery(ok=ok, chyba=chyba)
                except urllib.error.HTTPError as exc:
                    _uloz_stav_kamery(zprava=f"The app refused it (HTTP {exc.code}).")
                except (OSError, ValueError) as exc:
                    _uloz_stav_kamery(zprava=f"Failed: {exc}")
                self._send(b"", status=303, headers=[("Location", "/nastaveni")])
                return

            if self.path.startswith("/zkusit"):
                # Test spojení **bez serveru**: obsluha u trati potřebuje před
                # závodem vědět, že kabel a adresa sedí, i když je internet
                # zrovna mimo (David, 19. 8. 2026).
                host = (form.get("host", [""])[0] or "").strip()
                raw_port = (form.get("port", [""])[0] or "").strip()
                _posledni_zkouska["host"] = host[:64]
                _posledni_zkouska["port"] = raw_port[:8]
                try:
                    port = int(raw_port)
                except ValueError:
                    _remember(host or "—", 0, False, "the port is not a number")
                else:
                    # `tcp_probe` výsledek nevrací — úspěch i selhání zapisuje
                    # `_remember`, takže se objeví v tabulce níž. Výjimka tady
                    # nesmí spadnout do HTTP odpovědi: displej by místo
                    # výsledku ukázal chybu serveru.
                    try:
                        tcp_probe({"host": host, "port": port, "timeout": 3.0})
                        log(f"Zkouška spojení {host}:{port} → odpovědělo")
                    except Exception as exc:  # noqa: BLE001
                        log(f"Zkouška spojení {host}:{port} → {exc}")
                self._send(b"", status=303, headers=[("Location", "/nastaveni")])
                return

            if self.path.startswith("/novy-token"):
                global _novy_token_pozadan
                if _novy_token_odjisten():
                    # Druhé klepnutí ve lhůtě — teď doopravdy.
                    _novy_token_pozadan = 0.0
                    token = generate_token()
                    save_config(
                        configured_server(saved), token,
                        autostart=bool(saved.get("autostart")),
                    )
                    log("A new token was issued from the box display")
                    worker = state.get("worker")
                    if worker is not None:
                        worker.stop()
                        worker = Worker(configured_server(saved), token)
                        worker.start()
                        state["worker"] = worker
                else:
                    _novy_token_pozadan = time.monotonic()
                self._send(b"", status=303, headers=[("Location", "/")])
                return

            server_url = (form.get("server", [""])[0] or "").strip() or configured_server(saved)
            autostart = "autostart" in form

            # Nový token se vyrábí jen na výslovné přání: obsluha ho má
            # opsaný v aplikaci a tichá výměna by krabičku odpojila.
            token = generate_token() if "novytoken" in form else ensure_token(saved)

            save_config(server_url, token, autostart=autostart)
            set_autostart(autostart)

            worker = state.get("worker")
            if worker is not None:
                worker.stop()
            if server_url and token:
                worker = Worker(server_url, token)
                worker.start()
                state["worker"] = worker
            self._send(b"", status=303, headers=[("Location", "/nastaveni")])

    class TichyServer(http.server.ThreadingHTTPServer):
        """Odpojený klient se nehlásí jako chyba serveru.

        `handle()` v handleru pokryje běžný případ, ale výjimka umí vzniknout
        i dřív, než se handler vůbec dostane ke slovu. Log krabičky u trati má
        zůstat čitelný — je to jediné, podle čeho se u trati hledá závada.
        """

        daemon_threads = True

        def handle_error(self, request, client_address):
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    return TichyServer((host, port), Handler)


#: Jak často hlídač kontroluje, že vlákno agenta žije.
HLIDAC_S = 5.0


def _hlidej_workera(state: dict, *, konec=None) -> None:
    """Umřelé vlákno agenta = konec procesu s nenulovým kódem.

    Webový server běží v hlavním vlákně, takže smrt vlákna `agent` proces
    neukončila: displej svítil, systemd neměl co restartovat a krabička
    mlčky nepřeposílala (audit 23. 9. 2026). `Restart=always` v unitu
    proces zvedne a agent naběhne čistý. Worker vyměněný za nový (nový
    token, jiný server) se nepočítá — ten starý byl zastaven záměrně.
    """
    konec = konec or (lambda: os._exit(1))
    while True:
        time.sleep(HLIDAC_S)
        worker = state.get("worker")
        umrel = getattr(worker, "umrel", None)
        if callable(umrel) and umrel():
            log("The agent thread died — exiting so the service restarts it")
            konec()
            return


def serve_web(state: dict, *, host: str, port: int) -> None:
    server = build_web_server(state, host=host, port=port)
    threading.Thread(target=_hlidej_kameru, args=(state,), name="kamera", daemon=True).start()
    shown = host if host != "0.0.0.0" else "adresa-teto-krabicky"
    log(f"Displej krabičky: http://{shown}:{port}/")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trackside agent for BIKODY")
    parser.add_argument(
        "--server",
        default=os.environ.get("BIKODY_SERVER", ""),
        help=f"Application address (default {DEFAULT_SERVER})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BIKODY_AGENT_TOKEN", ""),
        help="Agent token from Application settings",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Forwarding only, without the settings page (service, systemd).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=WEB_PORT,
        help=f"Port of the settings page (default {WEB_PORT}).",
    )
    parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help=(
            "Which address to serve the page on. The default is this computer only; "
            "the trackside box needs 0.0.0.0 so it can be reached from another machine."
        ),
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help=(
            "Writes and enables a systemd service with automatic restart — "
            "the trackside box rises by itself after a crash and a reboot."
        ),
    )
    parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="Disables and removes the service (for a laptop, where the operator starts the agent).",
    )
    args = parser.parse_args(argv)

    if args.install_service:
        return install_service()
    if args.uninstall_service:
        return uninstall_service()

    saved = load_config()
    server_url = args.server or configured_server(saved)
    # Token si krabička vyrobí sama a ukáže ho na displeji; obsluha ho opíše
    # v aplikaci do Nastavení aplikace. Opačný směr by znamenal opisovat na
    # dotykovém displeji, což nikdo nechce.
    token = args.token or ensure_token(saved)

    log(f"Agent {VERSION} is starting")

    if args.headless:
        # Čistý přeposílač: nastavení přišlo z prostředí nebo ze souboru a
        # měnit se nemá. Tak běží služba na serveru.
        if not server_url or not token:
            parser.error("Missing --server or --token (they can also be passed through the environment).")
        worker = Worker(server_url, token)
        worker.start()
        try:
            while worker.is_running():
                time.sleep(0.5)
        except KeyboardInterrupt:
            worker.stop()
            log("Done.")
            return 0
        # Vlákno skončilo samo = chyba; nenulový kód, ať systemd restartuje.
        return 1 if worker.umrel() else 0

    # Jinak se agent obsluhuje stránkou: token se vloží v prohlížeči, ne
    # přepisováním souborů. Na krabičce u trati je to jediná obsluha, kterou má.
    state: dict = {}
    if server_url and token:
        worker = Worker(server_url, token)
        worker.start()
        state["worker"] = worker
    else:
        log("No application address yet — add it in the box settings.")

    threading.Thread(target=_hlidej_workera, args=(state,), name="hlidac",
                     daemon=True).start()
    try:
        serve_web(state, host=args.web_host, port=args.web_port)
    except KeyboardInterrupt:
        pass
    finally:
        if state.get("worker") is not None:
            state["worker"].stop()
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
