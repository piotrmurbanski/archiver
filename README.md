# Archiver

Lokalne narzedzie do planowania archiwizacji danych z NAS na plyty M-Disc 100 GB.

Aktualny zakres MVP:
- tygodniowy skan katalogow z NAS
- lokalna baza SQLite ze stanem plikow
- planowanie kolejnej plyty wedlug limitu pojemnosci
- generowanie indeksu `CSV` i manifestu `JSON` dla kazdej planowanej plyty
- prosty lokalny Web UI do podgladu kolejki i historii

Nagrywanie i pelna weryfikacja po nagraniu sa przygotowane w modelu danych, ale nie zostaly jeszcze zintegrowane z `xorriso` lub `growisofs`.

## Instalacja

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Opcjonalnie dla szybszych haszy:

```bash
pip install -e .[hash]
```

## Konfiguracja

Aplikacja czyta ustawienia z pliku `.env` lub zmiennych srodowiskowych:

```bash
ARCHIVER_DB_PATH=/home/piotr/sandbox/archiver/data/archive.db
ARCHIVER_ROOTS=/mnt/NASz
ARCHIVER_MANIFESTS_DIR=/home/piotr/sandbox/archiver/manifests
ARCHIVER_DISC_SIZE_GB=100
ARCHIVER_FILL_RATIO=0.93
ARCHIVER_WEB_HOST=127.0.0.1
ARCHIVER_WEB_PORT=8765
```

`ARCHIVER_FILL_RATIO=0.93` oznacza planowanie paczek do 93 GiB netto dla plyty 100 GB.
Przy `plan` powstaja pliki `manifests/DISC-XXXX.csv` i `manifests/DISC-XXXX.json`.
CSV ma sluzyc jako prosty indeks do przeszukiwania po sciezkach i nazwach plikow.

W przypadku NAS, ktory jest codziennie offline w nocy, ustaw skan poza oknem niedostepnosci:

```bash
ARCHIVER_SCAN_HOUR=10
```

Skan nie traktuje chwilowej niedostepnosci `/mnt/NASz` jako utraty danych. Jesli root jest offline, zadanie konczy sie komunikatem `scan skipped`.

## Uzycie

Inicjalizacja bazy:

```bash
archiver init-db
```

Skan katalogow:

```bash
archiver scan
```

Planowanie kolejnej plyty:

```bash
archiver plan
```

Podsumowanie stanu:

```bash
archiver status
```

Po zaplanowaniu plyty dokumenty trafiaja na plycie do katalogu `doc/YYYY/MM/`, a multimedia odpowiednio do `photos/YYYY/MM/` i `videos/YYYY/MM/`.

Lokalny interfejs:

```bash
archiver web
```

Potem otworz `http://127.0.0.1:8765`.

## Systemd

Przykladowe pliki uslug sa w katalogu `systemd/`.

## Stage, Burn, Verify

Po zaplanowaniu i zatwierdzeniu plyty workflow jest taki:

```text
scan -> plan -> approve -> stage -> burn -> verify
```

Przygotowanie stagingu:

```bash
archiver stage DISC-0001
```

Nagranie:

```bash
archiver burn DISC-0001
```

Weryfikacja po zamontowaniu plyty:

```bash
archiver verify DISC-0001 --mount-path /mnt/archiver-disc
```

W tej wersji `stage` kopiuje pliki do katalogu roboczego i doklada na plyte:

```text
/index/DISC-0001.csv
/index/DISC-0001.json
/photos/YYYY/MM/...
/videos/YYYY/MM/...
/doc/YYYY/MM/...
```

Do `burn` potrzebny jest `xorriso` oraz ustawienie:

```bash
ARCHIVER_OPTICAL_DEVICE=/dev/sr0
ARCHIVER_VERIFY_MOUNT=/mnt/archiver-disc
ARCHIVER_STAGING_DIR=/home/piotr/sandbox/archiver/staging
ARCHIVER_ISO_DIR=/home/piotr/sandbox/archiver/iso
```

`verify` oznacza pliki jako `verified` dopiero po zgodnosci hashy z zawartoscia plyty.

## Mount NAS przez automount

Dla NAS wylaczanego codziennie w nocy uzyj `systemd automount`, zeby `/mnt/NASz` podlaczal sie dopiero przy dostepie:

```fstab
//192.168.0.40/share /mnt/NASz cifs credentials=/home/piotr/.nas-credentials,uid=1000,gid=1000,iocharset=utf8,nofail,x-systemd.automount,_netdev 0 0
```

Po zmianie:

```bash
sudo systemctl daemon-reload
sudo mount -a
```
