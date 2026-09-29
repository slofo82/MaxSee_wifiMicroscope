# WiFi Microscope V5

Windows aplikácia pre bezdrôtové pripojenie a zobrazenie obrazu WiFi mikroskopu **Max-See** na PC.

Aplikácia prijíma video stream cez UDP, skladá jednotlivé fragmenty frameov, kontroluje ich integritu, dekóduje JPEG a zobrazuje posledný platný obraz.

Najdôležitejšia vlastnosť V5: **receiver a GUI sú oddelené**, takže pomalé prekresľovanie GUI nevytvára frontu starých obrazov a nespôsobuje postupný video lag.

## Funkcie

- WiFi pripojenie k Max-See mikroskopu
- editovateľná IP adresa mikroskopu
- LIVE video
- FPS, UDP packets/s, Mbit/s a latency
- WiFi connected/SSID/signal/IPv4/network profile
- lost fragments, duplicates, out-of-order, frame gaps
- corrupt frames a JPEG decode errors
- zahadzovanie starších frameov
- automatický WiFi monitoring a reconnect
- microscope re-init po obnovení WiFi
- Snapshot
- lokálne REC
- textový log a CSV štatistiky
- voliteľný detailný packet log

---

## 1. Pripojenie k mikroskopu

Mikroskop vytvára vlastnú WiFi sieť. Windows PC sa musí pripojiť **priamo na WiFi sieť mikroskopu**.

Predvolená konfigurácia v tomto projekte:

```text
SSID:              Max-See_dc7a
Microscope IP:     192.168.29.1
Command port:      20000
Receiver port:     10900
MIC_VIDEO_PORT:    10850
```

### Windows sieť musí byť Private

Windows môže novú WiFi sieť označiť ako **Public**. Pre lokálnu komunikáciu s mikroskopom nastavte túto sieť ako:

**Settings → Network & Internet → Wi-Fi → Properties → Network profile → Private**

Dôvodom je hlavne Windows Firewall a pravidlá pre lokálnu komunikáciu.

Aplikácia sama nemení firewall ani automaticky neprepína Public/Private profil.

---

## 2. Požiadavky

- Windows 10 alebo Windows 11
- WiFi adaptér
- Python 3.10 alebo novší

Overenie:

```powershell
python --version
```

---

## 3. Python knižnice

Použité externé knižnice:

- `opencv-python`
- `Pillow`

Inštalácia:

```powershell
python -m pip install opencv-python Pillow
```

Odporúčané je použiť virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install opencv-python Pillow
```

Ostatné moduly používané projektom sú súčasťou Pythonu.

---

## 4. Spustenie

Najprv pripojte Windows PC k WiFi mikroskopu.

Potom:

```powershell
cd C:\cesta\k\projektu
python wifi_mikroskop_v5_gui.py
```

Otvorí sa GUI.

Predvolenú IP adresu:

```text
192.168.29.1
```

je možné zmeniť priamo v GUI.

---

# 5. Architektúra

```text
                 MAX-SEE MIKROSKOP
                        │
                        │ UDP
                        ▼
             ┌──────────────────────┐
             │   RECEIVER PROCESS    │
             │                      │
             │ UDP receive          │
             │ packet parser        │
             │ frame assembly       │
             │ integrity checks     │
             │ JPEG decode          │
             │ statistics           │
             │ WiFi monitor         │
             └──────────┬───────────┘
                        │
                   Queue(maxsize=1)
                        │
                        ▼
             ┌──────────────────────┐
             │       TK GUI         │
             │                      │
             │ live video           │
             │ FPS / packets        │
             │ WiFi status          │
             │ controls             │
             └──────────────────────┘
```

Receiver a GUI bežia v **oddelenej procesovej vrstve**.

UDP receiver preto nemusí čakať na:

- vykresľovanie obrazu,
- resize okna,
- Snapshot,
- GUI,
- nahrávanie.

---

# 6. Prečo sa zahadzujú staršie framey?

Pri live videu nechceme, aby GUI postupne zobrazovalo staré obrázky.

Bez zahadzovania by mohla vzniknúť fronta:

```text
100 → 101 → 102 → 103 → 104 → ...
```

Ak GUI nestíha, používateľ by mohol vidieť frame 101, zatiaľ čo receiver už prijal napríklad 120.

To spôsobí niekoľkosekundový **lag**.

V5 preto používa:

```python
Queue(maxsize=1)
```

a starší GUI frame sa pri potrebe zahodí.

Výsledok:

```text
Receiver: 100 101 102 103 104 105 106
                         │
                         ▼
                    GUI queue
                         │
                         ▼
                  najnovší frame
```

Cieľom je **nízka latencia**, nie zobrazenie každého prijatého frameu.

---

# 7. UDP video protokol

Táto časť je popísaná podľa implementácie receivera v tomto projekte.

Receiver používa:

```python
sock_rtv.bind(("", PORT_RTV))
```

teda lokálny UDP port:

```text
10900
```

Packet sa prijíma pomocou:

```python
recvfrom(65535)
```

Receiver zároveň kontroluje zdrojovú IP adresu a spracováva packet iba vtedy, keď pochádza z aktuálne nastavenej `MIC_IP`.

---

## 7.1 Štruktúra packetu

Z packetu receiver používa:

```text
byte 0-1       Frame ID
byte 2         header
byte 3         Fragment ID
byte 4-7       ďalšie header údaje
byte 8-end     payload
```

Zjednodušene:

```text
┌────────────────────┬──────────────────────┐
│ 8-byte header      │ JPEG fragment        │
│ bytes 0..7         │ bytes 8..end         │
└────────────────────┴──────────────────────┘
```

Nie všetky polia prvých 8 byteov sú v tomto projekte jednoznačne identifikované. Receiver používa tie, ktoré potrebuje na skladanie frameu.

---

# 8. Frame ID a Fragment ID

Jeden obraz nie je poslaný v jednom UDP packete.

Napríklad:

```text
Frame 250:
    fragment 0
    fragment 1
    fragment 2
    fragment 3
    ...

Frame 251:
    fragment 0
    fragment 1
    fragment 2
    ...
```

`Frame ID` identifikuje obraz.

`Fragment ID` identifikuje časť daného obrazu.

Receiver preto najprv priradí packet k správnemu frameu a následne skladá jeho fragmenty.

---

# 9. Skladanie frameu

Zjednodušený priebeh:

```text
UDP packet
     │
     ├── Frame ID
     ├── Fragment ID
     └── payload
           │
           ▼
      frame assembly
           │
     ┌─────┼─────┐
     ▼     ▼     ▼
   frag0 frag1 frag2 ...
           │
           ▼
       JPEG data
           │
           ▼
      SOI / EOI
           │
           ▼
     cv2.imdecode()
           │
           ▼
      validný frame
```

Ak je frame nekompletný alebo poškodený, nepoužije sa ako nový obraz.

---

# 10. Stratené fragmenty

Receiver sleduje chýbajúce fragmenty.

Príklad:

```text
0
1
2
4
5
```

Fragment `3` chýba.

Takýto problém sa eviduje v:

```text
lost_fragments
```

Samostatne sa sledujú:

```text
duplicates
out_of_order
frame_gaps
```

---

# 11. Duplicate

Príklad:

```text
0
1
2
2
3
4
```

Fragment `2` bol prijatý dvakrát.

Receiver ho eviduje ako:

```text
duplicate
```

---

# 12. Out-of-order

UDP negarantuje poradie packetov.

Môže prísť napríklad:

```text
0
1
3
2
4
```

Takáto situácia sa eviduje ako:

```text
out_of_order
```

---

# 13. JPEG

Po zostavení dát receiver hľadá JPEG:

```text
SOI = FF D8
EOI = FF D9
```

Následne sa dáta dekódujú pomocou:

```python
cv2.imdecode(...)
```

Úspešné dekódovanie:

```text
jpeg_ok
```

Neúspešné:

```text
jpeg_failed
```

Poškodený frame sa nepoužije ako nový obraz.

---

# 14. Posledný platný frame

Dôležitý princíp:

```text
valid frame 100
       │
       ▼
corrupt frame 101
       │
       X
       │
       ▼
stále sa zobrazuje frame 100
```

Až ďalší platný frame nahradí predchádzajúci:

```text
frame 102
    │
    ▼
JPEG OK
    │
    ▼
zobraziť 102
```

To zabraňuje zobrazovaniu poškodených obrázkov.

---

# 15. Command protokol

Commandy používajú samostatný UDP port:

```text
PORT_CMD = 20000
```

V zdrojovom kóde sú definované tieto commandy.

### CMD_INIT_1

```text
4A 48 43 4D 44 D0 01
```

Python:

```python
CMD_INIT_1 = b"\x4A\x48\x43\x4D\x44\xD0\x01"
```

### CMD_INIT_2

```text
4A 48 43 4D 44 20 00 00 00 00 00
```

Python:

```python
CMD_INIT_2 = b"\x4A\x48\x43\x4D\x44\x20\x00\x00\x00\x00\x00"
```

### CMD_STOP

```text
4A 48 43 4D 44 D0 02
```

Python:

```python
CMD_STOP = b"\x4A\x48\x43\x4D\x44\xD0\x02"
```

Kompletný význam všetkých byteov command protokolu nie je v zdrojovom kóde jednoznačne určený; README preto uvádza ich skutočný obsah, ale nepriraďuje neoverené významy jednotlivým byteom.

---

# 16. Keep-alive

Po pripojení sa `CMD_INIT_1` posiela približne každú sekundu.

Slúži na udržanie komunikácie/streamu aktívneho.

---

# 17. WiFi monitoring

Aplikácia na Windows používa:

```text
netsh wlan show interfaces
```

a získava:

- connected state
- SSID
- WiFi interface
- signal

Ďalej získava Windows network informácie pre:

- NetworkCategory
- IPv4 connectivity
- IPv4 adresu

---

# 18. Automatické obnovenie WiFi

Pri strate WiFi aplikácia sleduje stav:

```text
CONNECTED
    │
    ▼
  LOST
    │
    ▼
reconnect attempts
    │
    ▼
CONNECTED
    │
    ▼
stable
    │
    ▼
microscope re-init
```

Po obnovení spojenia sa teda mikroskop znovu inicializuje až po stabilizácii spojenia.

---

# 19. Stream watchdog

V konfigurácii sú:

```text
STREAM_SLOW_MS = 500 ms
STREAM_LOST_MS = 3000 ms
```

Zjednodušene:

```text
< 500 ms       OK
500–3000 ms    SLOW
> 3000 ms      LOST
```

Ak je WiFi odpojená, stream stav reflektuje samostatne aj tento problém.

---

# 20. GUI

GUI obsahuje:

### Connection

- Microscope IP
- Connect / Apply IP
- Microscope INIT
- Reconnect WiFi

### LIVE VIDEO

Zobrazuje najnovší validný frame.

### Connection Status

- Stream
- WiFi
- SSID
- Signal
- Network
- IPv4

### Live Statistics

- FPS
- GUI FPS
- UDP packets/s
- Mbit/s
- latency
- lost fragments
- loss estimate
- corrupt frames
- dropped old frames
- frame ID
- re-init count

### Controls

- Start/Stop REC
- Snapshot
- Detailed packet log

---

# 21. Logovanie

Program vytvára:

```text
microscope_v5.log
microscope_v5_stats.csv
```

CSV obsahuje napríklad:

```text
timestamp
packet_rate
mbit_s
frames_s
latest_frame_id
latency_ms
complete_frames
incomplete_frames
corrupt_frames
lost_fragments
duplicates
out_of_order
frame_gaps
jpeg_ok
jpeg_failed
replaced_frames
wifi_connected
wifi_ssid
wifi_category
wifi_ipv4
stream_status
```

---

# 22. Diagnostika

## GUI beží, ale nie je obraz

Skontrolujte:

1. PC je pripojené k WiFi mikroskopu.
2. Sieť je nastavená ako Private.
3. IP adresa v GUI je správna.
4. Mikroskop je zapnutý.
5. Windows Firewall neblokuje komunikáciu.
6. UDP port `10900` nie je blokovaný iným programom.

## UDP packets/s = 0

Receiver nedostáva video packety.

Skontrolujte WiFi, IP a firewall.

## UDP packety chodia, ale rastie Corrupt frames

Sledujte:

```text
lost_fragments
duplicates
out_of_order
JPEG failed
```

To môže ukázať problém s kvalitou UDP streamu alebo fragmentáciou.

## Video má lag

Sledujte:

```text
Latency
Dropped old
FPS
```

V5 zámerne nedrží veľkú frontu frameov.

---

# 23. Známe limity protokolu

Toto README dokumentuje protokol podľa implementácie receivera v projekte.

Nie všetky byte-y video headera a commandov sú reverse-engineeringom jednoznačne identifikované.

Isté podľa implementácie sú najmä:

- Frame ID sa používa pri skladaní frameov.
- Fragment ID sa používa pri skladaní fragmentov.
- Payload začína na byte 8.
- JPEG SOI/EOI sa vyhľadávajú.
- JPEG sa dekóduje cez OpenCV.
- Poškodené/neúplné framey sa nepoužijú ako posledný platný obraz.

Nie je vhodné tvrdiť význam neidentifikovaných header byteov bez ďalšieho zachytenia a analýzy packetov.

---

# 24. Vývojový princíp V5

Najdôležitejšie pravidlo projektu:

> **Receiver nesmie čakať na GUI.**

Sieťový stream môže mať vysoký packet rate a GUI môže byť dočasne pomalé.

Preto:

```text
UDP receive
    ↓
decode
    ↓
latest valid frame
    ↓
Queue(maxsize=1)
    ↓
GUI
```

nie:

```text
UDP receive
    ↓
GUI
    ↓
wait for GUI
    ↓
next UDP packet
```

Tým sa minimalizuje riziko, že GUI spôsobí oneskorenie alebo postupné nahromadenie starého videa.

---

## Súbory

Hlavný program:

```text
wifi_mikroskop_v5_gui.py
```

Spustenie:

```powershell
python wifi_mikroskop_v5_gui.py
```

Logy:

```text
microscope_v5.log
microscope_v5_stats.csv
```

## Poznámka

Projekt je určený ako lokálny monitorovací/komunikačný nástroj pre Max-See WiFi mikroskop. Dokumentácia commandov a packetov vychádza zo správania implementovaného receivera, nie z oficiálnej verejnej špecifikácie výrobcu.

---

# 25. Licencia a zverejnenie

Tento projekt je zverejnený pod licenciou **MIT**. Licencia umožňuje projekt používať, upravovať a ďalej distribuovať vrátane komerčného použitia, pri zachovaní textu licencie a copyrightu.

Zdrojový kód tohto repozitára je nezávislý projekt a nie je oficiálnym softvérom výrobcu mikroskopu.

> **Disclaimer:** This project is an independent reverse-engineering and monitoring implementation for a Max-See WiFi microscope. It is not affiliated with or endorsed by Max-See.

### Tretie strany

- Názov **Max-See** a prípadné ochranné známky patria svojim príslušným vlastníkom.
- Tento repozitár neobsahuje oficiálny proprietárny zdrojový kód výrobcu.
- Ak sa do projektu neskôr pridajú fotografie, manuály, datasheety, ukážkové súbory alebo iný materiál tretích strán, treba pre každý taký materiál rešpektovať jeho vlastnú licenciu alebo autorské práva.
- Python knižnice používané projektom majú vlastné licencie; tento MIT súbor sa na ne nevzťahuje.

### Odporúčaná štruktúra GitHub repozitára

```text
wifi-microscope-v5/
├── wifi_mikroskop_v5_gui.py
├── README.md
├── LICENSE
└── .gitignore
```

## 26. Bezpečnostná a technická poznámka

Projekt komunikuje priamo s lokálnou IP adresou mikroskopu a používa protokol, ktorý bol analyzovaný experimentálne. Nejde o oficiálne zdokumentované API výrobcu. Používanie projektu môže závisieť od konkrétnej verzie firmvéru mikroskopu.

Pred zverejnením vlastných packet capture súborov, logov alebo screenshotov odporúčame skontrolovať, či neobsahujú osobné údaje, názvy lokálnych WiFi sietí, IP adresy alebo inú citlivú konfiguráciu.
