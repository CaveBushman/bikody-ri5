# Ovladač displeje

`mhs35.dtbo` je device-tree overlay pro 3,5" SPI displej **MHS35**
(řadič ILI9486, dotyk ADS7846). Pochází z projektu
[goodtft/LCD-show](https://github.com/goodtft/LCD-show) (GPL-3.0);
tady je jeho kopie, aby se krabička dala postavit jedním skriptem
a bez spouštění cizích instalátorů, které přepisují půl systému.

Instaluje ho `deploy.sh`: nakopíruje overlay do
`/boot/firmware/overlays/` a do `config.txt` přidá

```
dtparam=spi=on
dtoverlay=mhs35:rotate=90
```

Ovladač samotný (`fb_ili9486`, fbtft) je součástí jádra Raspberry Pi OS,
nic dalšího se neinstaluje. Displej dává jen framebuffer `/dev/fb0` —
žádné KMS/GPU — proto kiosk na těchhle panelech kreslí přes X/fbdev
(viz `kiosk/start-kiosk.sh`).

Projeví se po restartu; `deploy.sh` na to na konci upozorní.
