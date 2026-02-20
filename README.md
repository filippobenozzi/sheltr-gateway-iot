# AlgoDomo IoT (Python, seriale `/dev/ttyS0`)

Applicazione Python leggera con:

- pagina configurazione: `/config`
- pagina controllo: `/control` (entita divise per stanza)
- API `GET` autenticate da token (in query)
- invio comandi via seriale (`/dev/ttyS0` di default, 9600 8N1)

Tutti i dati vengono salvati in JSON locale.

## Interfaccia programmazione schede

Da `/config` ora puoi programmare tutto senza editare JSON manualmente:

- nome interno scheda
- indirizzo
- tipo (`light`, `shutter`, `thermostat`)
- range canali
- nome di ogni canale

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

- Config: `/etc/algodomoiot/config.json` (con systemd)
- Stato: `/etc/algodomoiot/state.json` (con systemd)

In locale (senza systemd):

- `data/config.json`
- `data/state.json`

## Avvio locale

```bash
python3 app.py
```

Server default: `http://localhost:8080`

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
- Polling singola scheda:
  - `GET /api/cmd/poll?token=...&address=1`
- Programmazione indirizzo (modalità Prog):
  - `GET /api/cmd/program-address?token=...&address=5`

## Installazione Raspberry Pi + systemd

File inclusi:

- service: `deploy/algodomoiot.service`
- env: `deploy/algodomoiot.env`
- installer: `install_raspberry.sh`

Installazione:

```bash
cd /percorso/algodomoiot
sudo ./install_raspberry.sh
```

L'installer:

- copia app in `/opt/algodomoiot`
- crea JSON in `/etc/algodomoiot`
- abilita/avvia `algodomoiot.service`
- aggiunge utente servizio al gruppo `dialout` (se presente)
- disabilita e mette in `mask` `serial-getty@ttyS0.service` e `serial-getty@serial0.service`
- rimuove la console seriale da `cmdline.txt` (con backup automatico)

Se l'installer modifica `cmdline.txt`, a fine installazione segnala di fare reboot.

Pagine:

- `http://<IP_RASPBERRY>:8080/config`
- `http://<IP_RASPBERRY>:8080/control`
