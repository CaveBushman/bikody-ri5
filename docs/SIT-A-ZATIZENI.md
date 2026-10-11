# Síťový provoz krabičky a zátěž serveru

Výkonnostní průchod po BMX víkendu 3.–4. 10. 2026 (agent 1.17). Tabulky
níže zachycují tehdejší stav před prodloužením čekání v Cloudu; aktuální
stav popisuje část „Cloud — čekání na příkazy“ na konci. Výchozí čísla jsou z nginx logu produkce za oba dny:

| Adresa | Dotazů | Doba na serveru |
|---|---|---|
| `POST /api/agent/commands/` | 27 897 | ~1,1 s (p95 1,11 s) |
| `POST /api/agent/passings/` | 10 904 | p95 0,22 s |
| `POST /api/agent/result/` | 1 949 | — |

## Co krabička posílá a jak často

Všechno jde **po drženém HTTPS spojení**, jedno na vlákno (od agenta 1.7).
TLS se navazuje jen po startu, po výpadku, a když nginx spojení zavře
(po 1000 dotazech nebo po 75 s nečinnosti).

| Vlákno | Dotaz | Do 1.16 | Od 1.17 |
|---|---|---|---|
| agent | `commands/` (dlouhý dotaz, server drží ≤ 1 s) | hned znovu: **~55/min** vždy | v klidu pauza 1 s: **~29/min**; bez proudu a bez příkazu 5 min pauza 2 s: **~19/min**; 3 s po příkazu hned |
| agent | `result/` | 1 na příkaz | beze změny |
| agent | `hello/` | po každém (re)connectu; před schválením 1× za 5 s | beze změny |
| proud (1 na smyčku) | `passings/` | dávka hned, jak je co poslat; ≤ 50 rámců; 1 dotaz najednou | beze změny |
| kamera | `camera/` | 1×/min | beze změny |

Odhad pro závodní den, kdy příkaz chodí v průměru jednou za ~16 s (1 949
výsledků proti 27 897 dotazům): **~35 dotazů/min místo ~55**, tedy o třetinu
až polovinu méně; za stejný víkend ~15–18 tisíc dotazů místo 27 897. Worker
serveru je jednou krabičkou obsazen ~50 % času místo ~100 %.

## Proč zrovna tahle čísla

Přestávka se přičítá k doručení **prvního** příkazu po klidu. Nejtěsnější
rozpočet má na serveru `tcp_exchange` (čtení z dekodéru, 1,5 s + 2 s):

* pauza 1 s + čtení až 1,5 s + 2× cesta po LTE (0,4 s) = 3,3 s < 3,5 s;
* pauza 2 s jen v hlubokém klidu (žádný proud = nikdo neměří, žádný příkaz
  5 minut); s běžným čtením (~0,35 s) je to 3,2 s;
* kontrolky dekodérů (`tcp_probe`, 6 s), startovka do kamery (`tcp_send`,
  6 s), hledání MAC (7 s) mají rezervy víc.

Okno 3 s po příkazu drží krabičku „vzhůru“ přes navazující příkazy
(kontrolky jdou jedna po druhé) i přes záchranné stahování po 1,5 s, když
push nedoručuje — tam by pauza zpomalila jedinou cestu, která zrovna vozí
průjezdy.

**Průjezdů se pauza netýká.** Jdou vlastním vláknem hned, jak přijdou
z dekodéru; dávkuje se samo — co přijde během běžícího dotazu, odejde
v dalším pohromadě. Pevné čekání na „víc rámců“ by každému průjezdu přidalo
zpoždění a ušetřilo málo.

## Pomalý server (3–17 s o víkendu)

* Každé vlákno má nejvýš **jeden dotaz na cestě** — krabička server
  nezahltí souběžnými dotazy.
* Timeout dávky průjezdů 15 s → **20 s**. Server potvrzoval i za 17 s;
  krabička to vzdala a poslala **tutéž dávku znovu** do rozdělané práce.
  Po timeoutu se teď čeká vteřinu, ne 50 ms. Duplicitu server dál sráží
  otiskem — tohle jen šetří jeho práci, když nestíhá.
* „Teď ne“ od serveru (`ok: false`, třeba krabička bez přiřazeného
  závodu) se opakuje se stropem **5 s** místo 1 s: do 1.16 to bylo
  60 dotazů/min za každou smyčku, klidně hodiny.
* Spadlé spojení se pozná hned (RST) a zopakuje na novém; na timeout čeká
  jen spojení, které mlčí.

## Krabička sama

* **Probouzení:** nečinný odesílatel spí 5 s místo 1 s (rámec ho budí
  hned) a čtení z dekodéru má takt 0,5 s místo 0,1 s — data přicházejí
  okamžitě, takt jen hlídá zastavení a servis watchdogu (10 s).
* **Paměť:** všechno má strop — buffer proudu 256 kB, fronta na disku 8 MB
  a 2 dny, dávka 50 rámců, posledních spojení 6.
* **Karta:** fronta je SQLite ve WAL se `synchronous=NORMAL`, takže se
  **nefsyncuje po průjezdu** (jen při checkpointu WAL). Zápis + potvrzení
  dávky je ~30 kB do WAL; při ~20 dávkách/min ~35 MB/h — na kartu A2 nic.
  Do žurnálu jde řádek na dávku (záměrně od 1.13).
* **Displej:** stránka se nenačítá znovu; JS se ptá `/stav` po sekundě
  (malý JSON po loopbacku) a hodiny tikají v prohlížeči. Beze změny.

## Co by pomohlo víc, ale není to na krabičce

* **Delší držení dotazu na serveru — již provedeno v Cloudu 6. 10. 2026:**
  5 s s kontrolou sdílené cache po 0,2 s. Původní návrh 20–25 s se
  nepoužil kvůli lhůtě ukončení procesu při nasazení a čerstvosti hlášení
  krabičky. Podrobnosti jsou v aktuální části níže.
* **Server řídí takt** (nepovinné pole v odpovědi `commands/`, např.
  „příští dotaz za N s“): server ví, jestli má někdo otevřenou obrazovku
  dekodérů. Změna smlouvy — neuděláno.
* **Jedna dávka pro víc smyček** (Hill + Finish v jednom POSTu) by
  zhruba půlila `passings/`. Změna smlouvy — neuděláno.

## Agent 1.19 — fronta při výpadku (10. 10. 2026)

`FrameQueue.ceka()` se volá při příjmu i opakovaně při odeslání. Původní
`COUNT(*)` musí načítat stránky celé tabulky nebo indexu; s narůstající
frontou roste režie pod zámkem sdíleným s přijímačem. Nově počet udržují
SQLite triggery v samostatném řádku `queue_count`, ve stejné transakci
jako rámce. Čtení je konstantní. Po ACK se stav načte jednou pro displej
i diagnostiku. Při opětovném otevření se už ani nepřepočítává součet bajtů.

Existující fronta dostane počítadlo při prvním otevření v jedné transakci.
Původní `queue_size` zachovává schéma, takže frontu umí otevřít i agent
1.18; jeho zápisy dál udržují nové počítadlo přes triggery. Limity, WAL,
mazání až po ACK a okamžité odesílání průjezdů zůstávají stejné.

Lokální mikrobenchmark (macOS, Python 3.14, dočasná SQLite na lokálním
disku): 60 000 rámců po 128 znacích, medián pěti sérií po 2 000 voláních
`ceka()`: agent 1.18 **14,30 µs**, agent 1.19 **2,27 µs** (asi **6,3×**).
Jde pouze o čtení počtu, ne o zrychlení celé cesty průjezdu; výsledek
na Raspberry Pi a SD kartě je potřeba změřit zvlášť.

Další doporučená práce: měření p95 ACK při reálném výpadku a obnově LTE
na RI5 a rozlišení času sítě, příjmu dávky a zpracování v Cloudu. Pevné
čekání na dávku nepřidávat: zhoršilo by dobu doručení čerstvého průjezdu.

## Cloud — čekání na příkazy (ověřeno 10. 10. 2026)

Cloud již od 6. 10. drží prázdný dotaz **5 s**, před čekáním vrací spojení
do databázového poolu a každých **0,2 s** čte ze sdílené cache stav práce.
Lokální příkaz budí vlákno ihned, příkaz z jiného procesu při následující
kontrole. Při klidu a sekundové pauze agenta vychází přibližně 10 dotazů
za minutu (bez započítání síťové režie), v hlubokém klidu s pauzou 2 s
přibližně 8,6. Jde o výpočet z časování, nikoli produkční měření.

Navazující úprava 10. 10.:

* Pořadí a kurzor fronty se čtou jedním `get_many` místo dvou `get`:
  prázdný výběr stojí jedno čtení cache místo dvou, výběr dostupného
  příkazu dvě čtení místo tří (včetně obsahu).
* Lokální událost se registruje před prvním výběrem fronty. Příkaz,
  který přijde do mezery mezi výběrem a čekáním, už neztratí probuzení;
  při nedostupné cache by jinak čekal až do konce pětisekundové lhůty.
* Pokud první kontrola sdílené cache už najde práci, odpoví se ihned,
  bez zbytečného čekání dalších 0,2 s. Již nastavená lokální událost
  nevyžaduje další čtení cache.

Tato změna je pouze v Cloudu: nevyžaduje další verzi agenta ani změnu
protokolu. Limit čekání se nemění; systemd dává na zastavení procesu 15 s
a stav proudů i počítadla se přenáší spolu s příkazy.

## Opakovatelné měření na RI5 (11. 10. 2026)

Nástroj `scripts/measure-performance.py` používá jen standardní knihovnu
Pythonu. Spouštějte z kořene aktuálního checkoutu BIKODY Ri5.

### Počítání fronty na konkrétním disku

```bash
python3 scripts/measure-performance.py queue --directory .
```

Vytvoří vlastní dočasný adresář na zvoleném svazku, naplní zkušební frontu
60 000 rámci po 128 znacích a porovná původní `COUNT(*)` s udržovaným
počítadlem. Výstup JSON uvádí medián pěti sérií po 2 000 čteních, verzi
agenta, Pythonu a platformu. Pořadí metod střídá a po sobě databázi smaže.
Živou frontu neotevírá, nevolá Cloud a nerestartuje služby.

`--directory` zvolte na stejné SD kartě jako data agenta, s právem zápisu;
výchozí systémový dočasný adresář může být na Linuxu v RAM. Jde o zahřáté
čtení SQLite, nikoli test zápisu, odolnosti SD nebo celé cesty průjezdu.
Měření samo krátce zatíží CPU a disk, proto ho provádějte mimo měřenou jízdu.

Lokální ověření 11. 10. na macOS arm64 / Python 3.14.6: **12,39 → 2,32 µs**
na čtení, přibližně **5,3×**. Fyzické RI5 nebylo připojeno; jeho výsledek
z tohoto čísla nelze odvodit.

### Potvrzení skutečných dávek z logu

```bash
journalctl -u bikody-agent --since '1 hour ago' --no-pager -o cat > ri5-agent.log
python3 scripts/measure-performance.py ack ri5-agent.log
```

Log musí být přístupný uživateli spouštějícímu `journalctl`; podle nastavení
krabičky může být nutné spustit tento příkaz přes `sudo`. Lze číst i stdin:

```bash
journalctl -u bikody-agent --since '1 hour ago' --no-pager -o cat \
  | python3 scripts/measure-performance.py ack
```

JSON obsahuje počet úspěšných dávek a rámců, medián, p95, p99, maximum
a počet potvrzení přes 200 ms, souhrnně i podle identifikátoru dekodéru
z logu. Percentily jsou podle dávek metodou nearest rank, ne vážené
počtem rámců. Různé velikosti dávek a různá období proto porovnávejte
s uvedením počtu vzorků.

ACK je čas od zahájení odeslání dávky do potvrzení a místního zpracování
ACK. Nezahrnuje čekání rámce před odesláním; neúspěšné pokusy a ztracená
potvrzení v této statistice nejsou. Nelze z ní určit jednosměrnou latenci
ani ztrátovost. Prázdný log nebo log bez úspěšných dávek končí chybou
s návratovým kódem 2, nikdy tvrzením o nulové latenci.

Testy nástroje ověřují percentily, oddělení neúspěšných hlášení, odmítnutí
chybějících dat a neplatných parametrů i zachování cizí databáze vedle
zkušebního adresáře. Fyzické měření zbývá po připojení RI5.
