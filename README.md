# Sheltr IoT (Python, seriale `/dev/ttyS0`)

Applicazione Python leggera con:

- pagina configurazione: `/config`
- pagina controllo: `/` (entita divise per stanza, `/control` compatibile)
- API `GET` autenticate da token (in query)
- invio comandi via seriale (`/dev/ttyS0` di default, 9600 8N1)
- integrazione `newt` (configurabile da UI + service systemd)
- client MQTT verso Home Assistant (configurabile da UI + service systemd)
- integrazione RTC I2C per l'ora di sistema (configurabile da UI)
- gestione rete Raspberry (wifi/ethernet da UI)

Tutti i dati vengono salvati in JSON locale.

## Interfaccia programmazione schede

Da `/config` ora puoi programmare tutto senza editare JSON manualmente:

- nome interno scheda
- indirizzo
- tipo (`light`, `shutter`, `thermostat`)
- range canali
- nome e stanza di ogni canale
- parametri `newt` (`enabled`, `id`, `secret`, `endpoint`)
- parametri `mqtt` (`enabled`, broker, topic, discovery, polling, auth)
- parametri `rtc` (`enabled`, `model`, `bus`, `address`)
- rete Raspberry (`mode` ethernet/wifi + credenziali wifi)
- pulsanti di manutenzione: restart servizio, restart newt, restart mqtt, applica rete

## Formato configurazione (salvato in JSON)

In `config.json` ogni scheda ha solo:

- `name` (nome interno)
- `address` (indirizzo scheda)
- `channelStart`, `channelEnd` (range canali)
- `kind` (`light`, `shutter`, `thermostat`)
- `channels[]` con `channel` + `name` + `room` (stanza per ordinamento grafico)

Esempio:

```json
{
  "id": "tapparelle-1",
  "name": "Scheda Tapparelle",
  "address": 2,
  "kind": "shutter",
  "channelStart": 1,
  "channelEnd": 4,
  "channels": [
    {"channel": 1, "name": "Tapparella 1", "room": "Sala"},
    {"channel": 2, "name": "Tapparella 2", "room": "Cucina"}
  ]
}
```

## File JSON locali

- Config: `/etc/sheltr/config.json` (con systemd)
- Stato: `/etc/sheltr/state.json` (con systemd)

In locale (senza systemd):

- `data/config.json`
- `data/state.json`

## Avvio locale

```bash
python3 app.py
```

Server default: `http://localhost`

## API GET (token obbligatorio)

- Stato:
  - `GET /api/status?token=...&refresh=1`
- Luce:
  - `GET /api/cmd/light?token=...&id=<board-id-cN>&action=on`
  - `action`: `on|off|toggle|pulse|toggle_no_ack`
- Tapparella:
  - `GET /api/cmd/shutter?token=...&id=<board-id-cN>&action=up`
  - `action`: `up|down|stop`
- Termostato:
  - `GET /api/cmd/thermostat?token=...&id=<board-id-cN>&set=21.5`
  - `GET /api/cmd/thermostat?token=...&id=<board-id-cN>&power=on|off`
  - `GET /api/cmd/thermostat?token=...&id=<board-id-cN>&mode=winter|summer`
- Polling singola scheda:
  - `GET /api/cmd/poll?token=...&address=1`
- Programmazione indirizzo (modalità Prog):
  - `GET /api/cmd/program-address?token=...&address=5`
- Info sistema:
  - `GET /api/system/info?token=...`
- Admin:
  - `GET /api/admin/restart?token=...&service=app|newt|mqtt`
  - `GET /api/admin/apply-network?token=...`
  - `GET /api/admin/apply-rtc?token=...`
  - `GET /api/admin/sync-rtc?token=...&mode=from-rtc|to-rtc`

## Installazione Raspberry Pi + systemd

File inclusi:

- service: `deploy/sheltr.service`
- service newt: `deploy/newt.service`
- service mqtt: `deploy/sheltr-mqtt.service`
- env: `deploy/sheltr.env`
- env newt: `deploy/newt.env`
- env mqtt: `deploy/mqtt.env`
- installer: `install_raspberry.sh`

Installazione:

```bash
cd /percorso/sheltr
sudo ./install_raspberry.sh
```

L'installer:

- copia app in `/opt/sheltr`
- crea JSON in `/etc/sheltr`
- abilita/avvia `sheltr.service`
- installa/abilita `newt.service` (avvio effettivo quando `NEWT_ENABLED=1` e credenziali presenti)
- installa/abilita `sheltr-mqtt.service` (avvio effettivo quando `MQTT_ENABLED=1` e parametri validi)
- installa strumenti I2C (`i2c-tools`), `hwclock` (`util-linux-extra`/`util-linux`) e script amministrativo RTC
- aggiunge utente servizio al gruppo `dialout` (se presente)
- disabilita e mette in `mask` `serial-getty@ttyS0.service` e `serial-getty@serial0.service`
- rimuove la console seriale da `cmdline.txt` (con backup automatico)

Se l'installer modifica `cmdline.txt`, a fine installazione segnala di fare reboot.

Pagine:

- `http://<IP_RASPBERRY>/` (control)
- `http://<IP_RASPBERRY>/config`
- gestione servizi: `sudo sheltr-stack enable-all|disable-all|status`
