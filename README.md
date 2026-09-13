# Event Control — krabička u trati

*Ri5 = **R**aspberry P**i 5**. Není to překlep, neopravovat.*

Raspberry Pi s displejem, které stojí u časomíry a **přeposílá data mezi
aplikací a železem na trati**. Aplikace Event Control běží na serveru
v datovém centru; dekodéry MyLaps a cílová kamera mají privátní adresy
v klubové síti (`192.168.x.y`), na které server nedosáhne. Krabička se serveru
sama hlásí a spojení naváže za něj.

```
   klubová síť u trati                        internet            datové centrum
 ┌───────────────────────┐                                     ┌────────────────┐
 │ dekodéry 192.168.9.x  │◄── TCP ──┐                          │  Event Control │
 │ kamera   192.168.1.x  │◄── TCP ──┤                          │     server     │
 └───────────────────────┘          │                          └────────────────┘
                            ┌───────┴────────┐   odchozí HTTPS          ▲
                            │    krabička    │─────────────────────────►│
                            └────────────────┘
```

**Ven jde jen odchozí HTTPS.** Na routeru pořadatele se nic neotevírá, nikde se
nenastavuje přesměrování portů a krabička nepotřebuje veřejnou adresu.

## Proč krabička, a ne program na notebooku

Program `track_agent.py` běží i na notebooku (Windows, macOS, Linux) a je to
tentýž soubor. Krabička má ale tři věci navíc, které se u trati počítají:

* **běží pořád** a nezávisí na tom, kdo má zapnutý notebook a jestli ho
  nezavřel;
* **je vidět** — na displeji svítí stav a nikdo se nemusí ptát, jestli to jede;
* **nikdo do ní nesahá** — nespouští se na ní nic jiného, takže nemá jak
  přestat fungovat.

## Co je potřeba koupit

| Díl | Poznámka |
|---|---|
| Raspberry Pi 5 (4 GB stačí) | 8 GB je zbytečné, agent je jeden proces |
| Aktivní chladič nebo krabička s ventilátorem | Pi 5 se u trati zahřeje |
| Napájení 27 W USB-C | originální; poddimenzované zdroje dělají restarty |
| microSD 32 GB (A2) nebo NVMe | SD stačí, zápisů je minimum |
| Displej | oficiální 7" dotykový, nebo malý HDMI (dotyk není potřeba — nic se na něm nepíše) |
| Ethernet kabel | wifi funguje, ale u trati je drát spolehlivější |

Displej je kvůli obsluze, ne kvůli funkci: krabička bez displeje dělá totéž
a stav se dá otevřít z notebooku v síti.

## Instalace

1. Do Raspberry Pi Imageru vyberte **Raspberry Pi OS (64-bit)**, v nastavení
   zapněte SSH a vyplňte uživatele. Kartu nastrčte do Pi a nastartujte.
2. Zkopírujte na Pi tenhle adresář (nebo `git clone`) a spusťte:

   ```bash
   sudo bash deploy.sh
   ```

   Jeden příkaz nastaví všechno: balíčky, agenta jako službu, displej,
   watchdog i časové pásmo. Trvá to minutu a nic se neptá. **Adresa aplikace
   se nezadává** — krabička se hlásí na `bikody.com`. Vlastní server se dá
   přidat přepínačem `--server`. Deploy současně nastaví jednoúčelový
   `multi-user.target` a zapne obě systemd služby, takže po každém zapnutí
   napájení naběhne agent i displej bez přihlášení. Pokud Pi dříve startovalo
   do plochy, změna se projeví po prvním restartu.

   *Proč `bash` a ne `./deploy.sh`: kopírování na Pi umí sebrat souboru právo
   ke spuštění (scp, rozbalený archiv, klon s vypnutým `core.fileMode`) a
   `sudo ./deploy.sh` pak hlásí „command not found". Přes `bash` to jde vždy;
   kdo chce, může si právo vrátit: `chmod +x deploy.sh`.*
3. Na displeji se ukáže **token krabičky** — šest čtveřic na dvou řádcích. Opište ho
   v aplikaci do *Nastavení aplikace* → **Přihlásit krabičku**. To je jediný
   krok, který po instalaci zbývá.
4. Do pěti vteřin naskočí na displeji velké zelené **OK**.

Co skript umí navíc:

```bash
sudo bash deploy.sh --hostname krabicka-brno \
                    --static-ip 192.168.9.10/24 --gateway 192.168.9.1
bash deploy.sh --dry-run          # jen vypíše, co by udělal, a nic nezmění
sudo bash deploy.sh --no-kiosk    # krabička bez displeje
sudo bash deploy.sh --no-pull     # nesahat na git (offline, vlastní úpravy)
```

**Deploy si nejdřív stáhne čerstvou verzi z gitu** (`git pull --ff-only`) —
displej, agent i skript se mění spolu, takže nasazení ze staré kopie nasadí
staré věci a na trati to nikdo nepozná. Když se něco přitáhlo a změnil se
i `deploy.sh`, skript se spustí znovu z nové verze (bash čte soubor po
částech, přepsat běžící skript uprostřed běhu je past). Místní změny
v adresáři pull přeskočí a řekne to — nic nepřepisuje pod rukama.

Pouštět se dá opakovaně — je to nastavení, ne instalace. **Token se přitom
nikdy nepřepíše**, protože ho obsluha má opsaný v aplikaci.

Token vyrábí krabička, ne aplikace: na dotykovém displeji se nic nepíše,
opisuje se tam, kde je klávesnice. **Celý token zůstává čitelný i po
přihlášení** — na displeji 480×320 je ve dvou řádcích a skupiny se nelámou.
Krabička proto patří do důvěryhodné místní sítě a fyzicky pod dohled.

## Co se stane po zapnutí zdroje

Nic se nespouští ručně a nikdo se nikam nepřihlašuje. Tohle je výchozí režim,
který `deploy.sh` vynutí i na Pi původně nainstalovaném s desktopem:

1. Systemd nastartuje **agenta** (`event-control-agent`) — hlásí se aplikaci
   a přeposílá data. Běží jako systémová služba, takže na ploše nezávisí.
2. Systemd nastartuje **displej** (`event-control-kiosk@<uživatel>`) — přes
   `cage` (minimální Wayland kompozitor) pustí prohlížeč na holé obrazovce.
   Proto se nemusí zapínat automatické přihlášení ani instalovat plocha.
3. Displej počká, až se agent ozve, a teprve pak otevře jeho stránku. Chybová
   stránka prohlížeče totiž vypadá jako rozbitá krabička.
4. Když prohlížeč spadne, systemd ho do tří vteřin zvedne. Když zamrzne celé
   Pi, restartuje ho **hardwarový watchdog** do půl minuty.

Obojí se dá zkontrolovat:

```bash
systemctl status event-control-agent
systemctl status event-control-kiosk@$USER
```

Agent na displeji nezávisí: i s černou obrazovkou jede časomíra dál.

## Co je na displeji

```
                 BIKODY.COM
              STAV SERVERU:
                ✓   OK          ← zelená: hlásí se aplikaci
     připojen jako … (organizace)
             TOKEN KRABIČKY:
             AKUW–…–7G59
   AKTUALIZOVÁNO: 16.08.2026 21:11:43
```

Tři stavy, které displej ukazuje:

| Stav | Co znamená |
|---|---|
| **NASTAVIT** (žlutá) | V nastavení krabičky je smazaná adresa aplikace. Běžně nenastane — adresa je předvyplněná. |
| **ČEKÁ** (červená) | Token je vidět celý; opište ho v aplikaci. Červeně, aby se od OK lišilo přes půl závodiště. |
| **OK** (zelená) | Krabička se hlásí aplikaci a přeposílá data. |

## Jak poznat, že to jede

* Na displeji krabičky svítí zelené **OK** a text *připojen jako … (organizace)*.
* V nastavení krabičky přibývají řádky **posledních spojení** — kdy, kam a jak
  to dopadlo. To je u trati nejrychlejší způsob, jak poznat, že dekodéry
  a kamera odpovídají.
* V aplikaci v **Nastavení aplikace** je u agenta zelená tečka a jméno stroje.
* V horní liště aplikace svítí kontrolky **Hill**, **Finish** a **Kamera**.
* Tlačítko **Dohledat MAC adresy** projde i z produkce.
* **Průjezdy naskakují do Parsingu** — to je ta hlavní věc, kvůli které
  krabička je. Agent 1.3 je aktivně posílá nejpozději po 0,3 s ticha;
  serverové dohledání od záložky je jen pojistka po výpadku. Když se nic
  neobjevuje, zkontrolujte verzi agenta, běžící měření a datum závodu.

Když krabička neběží, aplikace se chová jako dřív a spojení zkouší navázat
sama — u trati to nefunguje, ale nic se nerozbije.

## Displej krabičky

Vzhled podle návrhu `bikody_ri5_v2.html` (David, 20. 8. 2026): značka a hodiny
v hlavičce, dvě karty na polovinu (stav serveru a tlačítko **NOVÝ TOKEN**),
cesta průjezdu **smyčka → server** se dvěma diodami, token v rámečku
s rastrem a v nohou poslední průjezd s časem obnovení.

Tři věci, ve kterých se implementace od návrhu **záměrně** liší:

* **žádný Tailwind z CDN.** Krabička u trati bývá bez internetu a stránka
  z CDN by se jí nenačetla vůbec; třídy návrhu jsou přepsané do vlastního CSS
  a rozměry drží `clamp()` stejně jako v návrhu — obrazovka sedne na monitor
  i na 3,5" SPI displej (480×320).
* **token se spárované krabičce nezobrazuje celý** (jen `F4D8-…-TR7Q`).
  Do schválení je to jediné, proč se na displej dívat; potom je to klíč do
  klubové sítě vystavený celý den na obrazovce u trati.
* **„NOVÝ TOKEN" je jištěný dvěma klepnutími.** Návrh má jedno; nový token
  ale okamžitě odpojí krabičku od aplikace, takže první klepnutí tlačítko
  zčervená a řekne, co se stane, a druhé ve lhůtě token vydá. Náhodný dotek
  na displeji u trati tak závod neodstřihne od časomíry.

Displej se **neobnovuje celou stránkou**: hodiny tikají v prohlížeči a stav
(diody, stav serveru, token, poslední průjezd) se tahá z `/stav` po sekundě.
Obnovování celé stránky by animaci diod nikdy nenechalo doběhnout.

Demo tlačítko „SIMULOVAT PRŮJEZD" z návrhu v krabičce **není** — diody
rozsvěcuje skutečný provoz: rámec z dekodéru levou, přijetí serverem pravou.

## Údržba

```bash
sudo systemctl status event-control-agent     # stav
journalctl -u event-control-agent -f          # log
sudo bash scripts/update.sh                   # nová verze agenta ze serveru
sudo systemctl restart event-control-agent    # restart
```

Agent drží malou sadu síťových operací — připojit se, poslat bajty, vrátit,
co přišlo, hledat decodery a **držet proud průjezdů**: od verze 1.1 drží
spojení na dekodér sám a každý
průjezd hned pošle do aplikace; verze 1.2 hlídá skutečný 0,3s limit odeslání
(do verze 1.1 mohl sekundový socket timeout odeslání zdržet). Do verze 1.0
se průjezdy jen stahovaly na dotaz serveru po 1,5 s a od smyčky k obrazovce
to trvalo 2–4 s. Verze 1.3 na displeji obnovuje výraznou červenou kontrolu
každou sekundu, ukazuje čas posledního potvrzeného průjezdu a neopakuje stejný
aktivně odeslaný průjezd v počítadle. Ani proud ale
neznamená, že agent protokolu rozumí: otevírací rámce P3 (watchdog, resend od
záložky) mu **předchystá server** v konfiguraci a on jen řeže příchozí bajty
na rámce. Znalost protokolů (MyLaps P3, XML cílové kamery) zůstává na serveru,
takže **aktualizace aplikace neznamená aktualizaci krabiček**. `update.sh` se
hodí jen tehdy, když se mění samotný způsob spojení — jako právě u proudu.

**Pořadí při aktualizaci: nejdřív server, pak krabička.** `deploy.sh`
i `update.sh` si agenta stahují ze serveru (`/bmx/api/agent/download/`),
takže krabička dostane přesně tu verzi, se kterou nasazená aplikace počítá;
kopie v repozitáři slouží jen pro stavbu bez sítě. Starší agent (1.0) se
s novým serverem nerozbije — server mu nechá rychlé stahování po 1,5 s.

## Síť

Krabička musí **vidět dekodéry a kameru** a **dostat se ven na HTTPS**. Nic víc.

Pevná adresa se hodí jen proto, abyste na její stránku trefili z notebooku;
spojení navazuje vždycky ona směrem ven. Nastaví se buď rezervací v DHCP na
routeru (jednodušší), nebo na krabičce:

```bash
sudo bash scripts/set-static-ip.sh 192.168.9.10/24 192.168.9.1
```

**Adresy dekodérů a kamery se do krabičky nezadávají** — patří do aplikace
(*Nastavení dekodérů*, *Nastavení aplikace*). Dvě místa pravdy by si jednou
přestala odpovídat.

## Bezpečnost

Token je **heslo do vaší klubové sítě**: kdo ho má, může přes krabičku otevřít
TCP spojení kamkoliv v ní. Proto:

* celý token je na displeji kvůli provozní čitelnosti; krabička proto nepatří
  do veřejné wifi pro diváky ani na místo bez fyzického dohledu;
* nastavení je v `/opt/event-control-agent/config.json` s právy `600`;
* nový token se vyrábí jen na výslovné přání — tlačítkem **Nový token** na
  displeji (jištěné dvěma klepnutími) nebo v nastavení krabičky — a musí se
  pak znovu opsat v aplikaci;
* stránka krabičky je dostupná v celé místní síti (proto se na ni dostanete
  z notebooku) — nepatří tedy do veřejné wifi pro diváky.

## Návrh displeje

Podoba obrazovky vychází z `docs/navrh-displeje.html` — zadání, jak má
krabička vypadat. Skutečný displej je vlastní stránka agenta (bez Tailwindu
z CDN: u trati se nespoléhá na nic, co se stahuje).

## Odkud se bere agent

Zdrojem pravdy je hlavní repozitář aplikace
([CaveBushman/event-control](https://github.com/CaveBushman/event-control)),
soubor `tools/track_agent.py` — tam se agent vyvíjí a tam je pokrytý testy.
Tady je jeho kopie v `agent/track_agent.py`, aby se krabička dala postavit
i bez přístupu k němu. `scripts/update.sh` si stáhne aktuální verzi přímo
z vašeho serveru.
