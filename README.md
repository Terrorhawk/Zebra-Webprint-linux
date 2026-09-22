# Zebra Browser Print Emulation Bridge for Linux

Een lichtgewicht Python-service die de officiële **Zebra Browser Print API** (Zebra WebPrint JavaScript SDK) emuleert op Linux-systemen (zoals Linux Mint, Ubuntu, Debian). 

Hiermee kun je online webapplicaties en ticketing-systemen die gebruikmaken van de Zebra Browser Print JS SDK rechtstreeks laten communiceren met lokaal of via het netwerk aangesloten Zebra-printers (zoals de Zebra HC100, ZD420, GX420d, etc.), zonder dat de officiële Windows/macOS Zebra Browser Print software nodig is.

---

## 💡 Waarom dit project?

De officiële Zebra Browser Print client is alleen beschikbaar voor Windows en macOS. Wanneer je een Linux-gebaseerde Kiosk, Check-In Station of kassa-terminal inricht, herkent het JavaScript-clientbestand van de webpagina (`zebra-browserprint.js`) de lokale printer niet.

Deze bridge vangt alle HTTP/HTTPS API-verzoeken van de browser op, simuleert de exacte responsstructuur van Zebra Browser Print, stuurt afdrukopdrachten (ZPL) via CUPS door en peilt de **actuele hardwarestatus** (zoals een lege cassette, geopende klep of netwerkstoring) rechtstreeks op de printer.

---

## ✨ Features

* **Volledige SDK Emulatie:** Ondersteunt de `/available`, `/default`, `/write` en `/read` endpoints van de Zebra Browser Print API.
* **Dual-Port HTTP & HTTPS:** Luistert gelijktijdig op poort `9100` (HTTP) en poort `9102` (HTTPS met SSL-certificaat) voor naadloze integratie met beveiligde webapps.
* **CORS Support:** Inclusief correcte `Access-Control-Allow-*` HTTP-headers om Cross-Origin problemen in de browser te voorkomen.
* **Slimme Hardware Status Monitoring:** 
  * Onderschept statuscommando's (`~HQES`, `~HS`, `~HI`, `~HD`).
  * Sluist het verzoek via een TCP-socket direct door naar poort 9100 van een netwerkprinter (of `/dev/usb/lp0` bij USB).
  * Ontleedt hexadecimale bitfields in de `~HQES` respons om actieve hardwarefouten (zoals *Paper Out* of *Door Open*) te detecteren.
  * Zorgt voor een correcte `reject()` of `resolve()` afhandeling in de JavaScript Promise van de webapp.
* **CUPS Integratie:** Maakt gebruik van de standaard Linux `lp` print-queue met RAW ZPL-passthrough.

---

## 🏗️ Hoe het werkt

1. **Printer Discovery (`GET /default` / `GET /available`):**  
   Het script vraagt via `lpstat -a` de aanwezige CUPS-printers op en filtert op Zebra-apparaten.
2. **Printopdrachten (`POST /write`):**  
   Ontvangen ZPL-data of JSON-payloads worden direct naar de CUPS raw queue gestuurd via `lp -o raw -d <printer>`.
3. **Statuspeiling (`~HQES` Passthrough):**  
   Wanneer het JavaScript van de website vraagt om een statuscheck (`~HQES`), maakt het script een actieve TCP-verbinding met de printer op poort 9100. Is de printer fysiek in storing of uitgeschakeld? Dan stuurt de bridge een leeg antwoord terug op `/read`, waardoor de JavaScript Promise in de browser netjes afhaakt en een foutmelding toont.

---

## 📋 Vereisten

* **Besturingssysteem:** Linux (Ubuntu, Linux Mint, Debian)
* **Python:** Python 3.x (standaard aanwezig op vrijwel alle Linux-distributies)
* **Afdruksysteem:** CUPS geïnstalleerd en geconfigureerd met de Zebra printer.
* **Pakketten:** `python3-pil` (optioneel, voor eventuele grafische statusafhandeling) en `openssl`.

---

## 🚀 Installatie & Setup

### 1. CUPS Printer Configuratie
Zorg ervoor dat je Zebra-printer is toegevoegd aan CUPS en een naam heeft waar "Zebra" of "HC100" in voorkomt (bijvoorbeeld `Zebra1`).

### 2. Certificaten aanmaken voor HTTPS (Poort 9102)
Aangezien moderne webapplicaties via HTTPS draaien, verwachten ze een veilige SSL-verbinding op poort `9102`. Genereer een self-signed certificaat op de Linux-machine:

```bash
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/ssl/private/zebra-key.pem \
  -out /etc/ssl/certs/zebra-cert.pem \
  -subj "/CN=localhost"

3. Script plaatsen

Download zebra_bridge.py en plaats deze in /usr/local/bin/:
Bash

sudo cp zebra_bridge.py /usr/local/bin/zebra_bridge.py
sudo chmod +x /usr/local/bin/zebra_bridge.py

⚙️ Automatisch starten als Systemd Service

Maak een systemd service-bestand aan om de bridge automatisch te laten starten bij het opstarten van het systeem:
Bash

sudo nano /etc/systemd/system/zebra-bridge.service

Plak de volgende inhoud in het bestand:
Ini, TOML

[Unit]
Description=Zebra WebPrint Browser Print Bridge Service
After=network.target cups.service

[Service]
Type=simple
User=root
WorkingDirectory=/usr/local/bin
ExecStart=/usr/bin/python3 /usr/local/bin/zebra_bridge.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target

Herlaad systemd, activeer de service en start deze op:
Bash

sudo systemctl daemon-reload
sudo systemctl enable zebra-bridge.service
sudo systemctl start zebra-bridge.service

Controleer de status van de service:
Bash

sudo systemctl status zebra-bridge.service

🔍 Logboeken inzien

Je kunt de realtime verzoeken en printer-statuspeilingen bekijken via journalctl:
Bash

sudo journalctl -u zebra-bridge.service -f

📄 Licentie

Dit project is beschikbaar onder de MIT-licentie. Voel je vrij om dit script aan te passen, te verbeteren of uit te breiden voor jouw specifieke toepassingen!
