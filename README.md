# AlgoDomo IoT (Python)

Applicazione Python leggera con:

- pagina configurazione: `/config`
- pagina controllo: `/control`
- API `GET` autenticate da token (in query)

Protocollo usato: Algo_Domo 1.6 (`0x49 ... 0x46`, pacchetto 14 byte).

## Avvio locale

```bash
python3 app.py
```

Server default: `http://localhost:8080`

## Configurazione

Da `/config` puoi impostare:

- gateway TCP (`host`, `port`, `timeoutMs`)
- token API (`apiToken`)
- schede e ingressi (`boards[].inputs[]`)
- entita (`lights`, `shutters`, `thermostats`)

### Preset tapparelle (richiesta utente)

Preset applicato di default e disponibile col pulsante `Preset 4 tapparelle`:

- Tapparella 1: IN3 SU, IN1 GIU
- Tapparella 2: IN4 SU, IN2 GIU
- Tapparella 3: IN7 SU, IN5 GIU
- Tapparella 4: IN8 SU, IN6 GIU

Ogni input invia comando `0x5c` verso la scheda target con `g3=canale` e `g4=azione` (`0x55` SU, `0x44` GIU).

## API GET (token obbligatorio)

Esempio: `?token=IL_TUO_TOKEN`

- Stato per stanza:
  - `GET /api/status?token=...&refresh=1`
- Luce:
  - `GET /api/cmd/light?token=...&id=light-1&action=on`
  - `action`: `on|off|toggle|pulse|toggle_no_ack`
- Tapparella:
  - `GET /api/cmd/shutter?token=...&id=shutter-1&action=up`
  - `action`: `up|down|stop`
- Termostato:
  - `GET /api/cmd/thermostat?token=...&id=thermo-1&set=21.5`
- Polling singola scheda:
  - `GET /api/cmd/poll?token=...&address=1`
- Invio configurazione ingressi (`0x55`) per tutte le schede:
  - `GET /api/cmd/apply-inputs?token=...`
- Programmazione indirizzo in modalita `Prog`:
  - `GET /api/cmd/program-address?token=...&address=5`

## Comandi protocollo implementati

- Polling esteso: `0x40`
- Relè 1..8: `0x51,0x52,0x53,0x54,0x65,0x66,0x67,0x68`
- Config ingressi: `0x55`
- Tapparelle: `0x5c`
- Set termostato: `0x5a`

## Installazione Raspberry Pi + systemd

Nel repo trovi:

- service: `deploy/algodomoiot.service`
- env override: `deploy/algodomoiot.env`
- installer: `install_raspberry.sh`

Esegui sul Raspberry:

```bash
cd /percorso/algodomoiot
sudo ./install_raspberry.sh
```

L'installer:

- copia app in `/opt/algodomoiot`
- crea config in `/etc/algodomoiot`
- installa e avvia `algodomoiot.service`

Pagine poi raggiungibili su:

- `http://<IP_RASPBERRY>:8080/config`
- `http://<IP_RASPBERRY>:8080/control`

## File principali

- `app.py`
- `public/config.html`
- `public/control.html`
- `data/config.json`
- `data/state.json`
- `deploy/algodomoiot.service`
- `install_raspberry.sh`
