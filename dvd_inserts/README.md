# Wkladki DVD

Generator tworzy drukowalne wkładki na arkuszu A4 i wspiera dwa układy:
- `dvd` dla standardowego pudełka DVD
- `small_case` dla mniejszego pudełka kalibrowanego pod bieżący projekt

## Jak użyć

1. Edytuj plik `example_inserts.json` albo przygotuj własny JSON w tym samym formacie.
2. Wygeneruj HTML:

   ```bash
   python3 dvd_inserts/generate_dvd_insert.py dvd_inserts/example_inserts.json dvd_inserts/output/wkladki_dvd.html
   ```

3. Opcjonalnie wygeneruj PDF w LibreOffice:

   ```bash
   libreoffice --headless --convert-to pdf --outdir dvd_inserts/output dvd_inserts/output/wkladki_dvd.html
   ```

## Bieżący szablon dla archiwum

Przygotowany workflow dla kolejnych płyt używa układu `small_case` i może wygenerować:
- osobne strony dla frontu i tyłu
- jedną stronę A4 z frontem i tyłem obok siebie

Przykład dla pojedynczej płyty:

```bash
python3 dvd_inserts/generate_dvd_insert.py \
  --layout small_case \
  --sheet-mode single-page \
  dvd_inserts/singles/DISC-0001.json \
  dvd_inserts/output/DISC-0001.html
```

Eksport do PDF:

```bash
google-chrome --headless --disable-gpu \
  --print-to-pdf=dvd_inserts/output/DISC-0001.pdf \
  file:///home/piotr/sandbox/archiver/dvd_inserts/output/DISC-0001.html
```

## Format danych

Każdy element listy opisuje jedną wkładkę:

```json
{
  "title": "Nazwa kolekcji",
  "subtitle": "Krótki opis",
  "side_label": "Tekst na paskach bocznych",
  "discs": ["Plyta 1: ...", "Plyta 2: ..."],
  "highlights": ["Uwagi 1", "Uwagi 2"]
}
```
