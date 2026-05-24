# Archiver

Lokalne narzedzie do planowania archiwizacji danych z NAS na plyty M-Disc 100 GB.

Aktualny zakres MVP:
- tygodniowy skan katalogow z NAS
- lokalna baza SQLite ze stanem plikow
- planowanie kolejnej plyty wedlug limitu pojemnosci
- automatyczne planowanie plyty po przekroczeniu progu pojemnosci
- lokalne notyfikacje po `scan`, `approve`, `stage`, `burn` i `verify`
- automatyczny mount plyty i `verify` zaraz po nagraniu
- generowanie indeksu `CSV` i manifestu `JSON` dla kazdej planowanej plyty
- logowanie do pliku z rotacja oraz na stdout
- przygotowanie `staging/`, budowa ISO przez `xorriso` i nagrywanie przez `growisofs`
- weryfikacja zawartosci po zamontowaniu plyty
- prosty lokalny Web UI do podgladu kolejki i historii

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
ARCHIVER_BACKUPS_DIR=/home/piotr/sandbox/archiver/backups
ARCHIVER_ROOTS=/mnt/NASz
ARCHIVER_MANIFESTS_DIR=/home/piotr/sandbox/archiver/manifests
ARCHIVER_DISC_SIZE_GB=100
ARCHIVER_TEST_DISC_SIZE_GB=
ARCHIVER_FILL_RATIO=0.93
ARCHIVER_WEB_HOST=0.0.0.0
ARCHIVER_WEB_PORT=8765
ARCHIVER_AUTO_PLAN=true
ARCHIVER_AUTO_VERIFY=false
ARCHIVER_AUTO_BACKUP_AFTER_VERIFY=true
ARCHIVER_BACKUP_KEEP=2
ARCHIVER_VERIFY_RETRY_COUNT=10
ARCHIVER_VERIFY_RETRY_DELAY_SECONDS=6
ARCHIVER_VERIFY_MOUNT_WAIT_SECONDS=20
ARCHIVER_LOG_DIR=/home/piotr/sandbox/archiver/logs
ARCHIVER_LOG_LEVEL=INFO
```

`ARCHIVER_FILL_RATIO=0.93` oznacza planowanie paczek do 93 GiB netto dla plyty 100 GB.
Jesli chcesz testowac workflow na tanszych, mniejszych nosnikach, ustaw:

```bash
ARCHIVER_TEST_DISC_SIZE_GB=25
```

Wtedy planowanie bedzie liczone pod mniejsza plyte testowa, bez zmiany docelowego `ARCHIVER_DISC_SIZE_GB=100`.
Przy `plan` powstaja pliki `manifests/DISC-XXXX.csv` i `manifests/DISC-XXXX.json`.
CSV ma sluzyc jako prosty indeks do przeszukiwania po sciezkach i nazwach plikow.
CSV jest celowo uproszczony i zawiera tylko najwazniejsze kolumny do przegladania:
- `source_folder`, czyli nazwa udzialu lub katalogu zrodlowego, np. `Public` albo `Kasia_priv`
- `relative_path`
- `archive_kind` z uproszczonym typem pliku: `doc`, `movie`, `pic`, `raw`

Pelniejszy zestaw metadanych pozostaje w pliku `JSON`.
Jesli `ARCHIVER_AUTO_PLAN=true`, tygodniowy skan sam zaplanuje nowa plyte po przekroczeniu progu.

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

Backup bazy SQLite:

```bash
archiver backup-db
```

Domyslnie backup trafia do `ARCHIVER_BACKUPS_DIR` z nazwa typu:

```text
archive-20260524T101530Z.db
```

Mozesz tez podac wlasna sciezke:

```bash
archiver backup-db --output /home/piotr/sandbox/archiver/backups/manual-before-replan.db
```

Po udanym `verify` aplikacja moze tez automatycznie tworzyc backup bazy i trzymac tylko ostatnie kopie:

```bash
ARCHIVER_AUTO_BACKUP_AFTER_VERIFY=true
ARCHIVER_BACKUP_KEEP=2
```

Skan katalogow:

```bash
archiver scan
```

Jesli po skanie uzbiera sie co najmniej `ARCHIVER_FILL_RATIO * ARCHIVER_DISC_SIZE_GB`,
narzedzie automatycznie utworzy nowa partie i wysle lokalna notyfikacje.

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

Jesli chcesz uruchomic wszystko jedna komenda, uzyj:

```bash
archiver start
```

Ta komenda:
- uruchamia Web UI od razu
- nie odpala skanu automatycznie
- pozwala uruchomic skan recznie z poziomu przegladarki
- pokazuje tez progress planowania i hashowania w formie `X/Y`
- pokazuje tez progress stage w formie `X/Y`

## Systemd

Przykladowe pliki uslug sa w katalogu `systemd/`.

## Stage, Burn, Verify

Po zaplanowaniu i zatwierdzeniu plyty workflow jest taki:

```text
scan -> plan -> approve -> stage -> burn -> verify
```

Z poziomu GUI dostepny jest tez przycisk `Zrob wszystko do nagrania`, ktory wykonuje:

```text
scan -> plan -> approve -> stage
```

Celowo zatrzymuje sie przed `burn`.

W praktyce `burn` domyslnie konczy sie po nagraniu. `verify` uruchamiasz osobno po ponownym wsunieciu plyty albo z przycisku w GUI.
Krok `verify` odczytuje dane bezposrednio z napedu, bez recznego mountowania plyty.
Jesli lokalny plik `iso/DISC-XXXX.iso` nadal istnieje, verify z napedu najpierw porownuje cala plyte sekwencyjnie z obrazem ISO, co jest duzo szybsze niz sprawdzanie kazdego pliku osobno. Fallback do trybu plik-po-pliku zostaje tylko na wypadek braku lokalnego ISO.

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

Jesli kiedys wlaczysz `ARCHIVER_AUTO_VERIFY=true`, automatyczny verify po `burn` wykona do `ARCHIVER_VERIFY_RETRY_COUNT` prob mountowania plyty, z opoznieniem `ARCHIVER_VERIFY_RETRY_DELAY_SECONDS` sekund miedzy probami.

W tej wersji `stage` kopiuje pliki do katalogu roboczego i doklada na plyte:

```text
/index/DISC-0001.csv
/index/DISC-0001.json
/photos/YYYY/MM/<source_folder>/...
/videos/YYYY/MM/<source_folder>/...
/doc/YYYY/MM/<source_folder>/...
```

Pod struktura roku i miesiaca zachowywany jest tez zrodlowy udzial oraz oryginalna sciezka wzgledna pliku. To zapobiega kolizjom nazw typu dwa rozne pliki `_DSC0001.NEF` w tym samym miesiacu.

Przed rozpoczeciem `stage` narzedzie sprawdza, czy na dysku pod katalogiem staging jest co najmniej tyle wolnego miejsca, ile wynosi pojemnosc plyty z `ARCHIVER_DISC_SIZE_GB`. Jesli miejsca jest za malo, proces przerywa sie przed kopiowaniem plikow.
Po zakonczeniu `stage` narzedzie sprawdza tez, czy liczba skopiowanych plikow zgadza sie z planem plyty, zanim uzna staging za poprawny.

Do `burn` potrzebny jest `xorriso` oraz ustawienie:

```bash
ARCHIVER_OPTICAL_DEVICE=/dev/sr0
ARCHIVER_VERIFY_MOUNT=/home/piotr/sandbox/archiver/mnt/archiver-disc
ARCHIVER_STAGING_DIR=/home/piotr/sandbox/archiver/staging
ARCHIVER_ISO_DIR=/home/piotr/sandbox/archiver/iso
```

Przed startem `growisofs` narzedzie:
- buduje ISO
- sprawdza, czy obraz miesci sie na wlozonym nosniku
- zapisuje diagnostyke napedu i nosnika (`xorriso -toc`, `dvd+rw-mediainfo`)

Jesli `growisofs` wypisze komunikaty typu `Input/output error`, `write failed` albo `FLUSH CACHE failed`, burn zostanie oznaczony jako `burn_failed`, nawet jesli proces zwroci kod `0`.

`verify` jest osobnym krokiem od `burn` i oznacza pliki jako `verified` dopiero po zgodnosci zawartosci plyty z oczekiwanym archiwum.
Po udanym `verify` katalog `staging/DISC-XXXX/` jest automatycznie usuwany.
Jesli automatyczny verify po `burn` sie nie powiedzie, plyta dostaje status `verify_failed` i mozna powtorzyc tylko sam krok verify.

## Notyfikacje

Domyslnie Archiver probuje uzyc `notify-send`. Na Ubuntu zwykle wystarczy:

```bash
sudo apt-get install -y libnotify-bin
```

Mozesz tez podac wlasna komende:

```bash
ARCHIVER_NOTIFY_COMMAND=/usr/local/bin/archiver-notify
```

Komenda dostaje dwa argumenty:

```text
<title> <body>
```

## Logi

Archiver loguje jednoczesnie:
- do stdout
- do pliku z rotacja

Domyslnie plik logu jest tutaj:

```text
logs/archiver.log
```

Mozesz to zmienic przez:

```bash
ARCHIVER_LOG_DIR=/home/piotr/sandbox/archiver/logs
ARCHIVER_LOG_LEVEL=INFO
ARCHIVER_LOG_MAX_BYTES=10485760
ARCHIVER_LOG_BACKUP_COUNT=5
```

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
