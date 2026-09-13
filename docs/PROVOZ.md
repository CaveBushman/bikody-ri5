# Krabička v závodní den

Krátký seznam pro obsluhu u trati. Podrobnosti jsou v `README.md`.

## Před závodem (večer nebo ráno)

1. **Zapojit** — napájení, ethernet do stejného switche jako dekodéry. Agent
   i displej jsou výchozí systemd služby: nic se nepřihlašuje ani nespouští,
   krabička najede sama.
2. **Podívat se na displej.** Do minuty má svítit zelené **OK** a text
   *připojen jako … (organizace)*.
3. **V aplikaci** otevřít *Nastavení aplikace* — u agenta má být zelená tečka
   a jméno krabičky. V horní liště mají svítit **Hill**, **Finish** a **Kamera**.
4. **Dohledat MAC adresy** — když projde, vidí krabička na dekodéry.

Když něco z toho nesvítí, jde o síť, ne o aplikaci: viz *Když to nejde* níž.

## Aktualizace

Nejdřív se nasazuje **server**, potom krabička — ta si agenta stahuje ze
serveru, takže obrácené pořadí by jí dalo starou verzi:

```bash
sudo bash scripts/update.sh     # stačí; deploy.sh dělá totéž a navíc projde nastavení
sudo bash deploy.sh             # navíc `git pull` — displej i skript se mění spolu s agentem
```

`update.sh` mění **jen agenta** (stáhne ho ze serveru). Když se změnil
i displej nebo samo nastavení krabičky, je potřeba `deploy.sh` — ten si
nejdřív přitáhne čerstvý repozitář a pak projde celé nastavení.

## Během závodu

Krabička nepotřebuje obsluhu. Na displeji je pořád vidět stav, takže se dá
jedním pohledem poznat, jestli spojení drží.

Když vypadne wifi nebo se přepojí kabel, agent se sám připojí znovu —
v aplikaci se to projeví jen tím, že kontrolky na chvíli zšednou.

**Průjezdy se při výpadku neztrácejí.** Co se nevejde do paměti, krabička
odloží na kartu a pošle to sama, až se aplikace ozve; na displeji je u toho
oranžový pruh *„… RÁMCŮ ČEKÁ NA DISKU — DOLETÍ SAMY"*. Nikam se kvůli němu
nechodí, jen se čeká, až zmizí.

Červený pruh *„ZAHOZENO …"* je něco jiného: tam už se ztratilo a musí se to
dohledat rukou — v aplikaci v **Časomíře** tlačítkem *Dohledat průjezdy
z dekodéru*. Stane se to jen po hodně dlouhém výpadku nebo při plné kartě.

## Po závodě

Nic. Krabička může zůstat zapojená; když se vypne, nic se neztratí —
průjezdy jsou v dekodérech i v aplikaci.

## Když to nejde

| Co je vidět | Co s tím |
|---|---|
| Displej ukazuje **NASTAVIT** | Někdo smazal adresu aplikace — doplnit v nastavení krabičky (`bikody.com`). |
| Displej ukazuje **ČEKÁ** | Token z displeje ještě není v aplikaci — opsat ho do *Nastavení aplikace* → **Přihlásit krabičku**. |
| **ČEKÁ** i po opsání | Překlep. Token se čte bez ohledu na pomlčky, mezery a malá písmena, ale znaky musí sedět. |
| *server není k dispozici* | Krabička nemá internet. Zkontrolovat kabel a router. |
| Kontrolky Hill/Finish jsou červené | Krabička nevidí dekodéry: jiná síť, vypnutý switch, nebo špatná IP v *Nastavení dekodérů*. |
| Kamera červená | Software kamery neběží, nebo má jinou adresu než v *Nastavení aplikace*. |
| Oranžový pruh **… RÁMCŮ ČEKÁ NA DISKU** | Nic. Aplikace zrovna nebere a krabička si průjezdy odložila; pošle je sama, až se ozve. Číslo pak padá k nule. |
| Červený pruh **ZAHOZENO …** | Průjezdy se ztratily (dlouhý výpadek, plná karta). Dohledat je v aplikaci v *Časomíře* tlačítkem **Dohledat průjezdy z dekodéru** — dokud si je dekodér pamatuje. |
| Displej je černý | Agent běží dál, časomíra jede. Displej se sám zvedne do tří vteřin; když ne, `sudo systemctl restart event-control-kiosk@$USER`. |
| Ventilátor jede naplno „bez důvodu" | Podívat se, kdo sype log: `journalctl --since '-1 min' \| wc -l`. Tisíce řádků za minutu = nějaká služba v havarijní smyčce (typicky Raspberry Pi Connect — `rpi-connect off`). |

Log krabičky (přes SSH):

```bash
journalctl -u event-control-agent -f
```

## Náhradní řešení, když krabička chybí

Program agenta se dá spustit na notebooku u trati — je to tentýž soubor:

```bash
python3 track_agent.py
```

Stáhne se v aplikaci v *Nastavení aplikace* odkazem **Stáhnout agenta**.
Nastavení se pak zadává na `http://127.0.0.1:8088/`.
