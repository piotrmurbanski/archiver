#!/usr/bin/env python3
"""Generate printable case inserts from JSON data."""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path
from textwrap import wrap


LAYOUTS = {
    "dvd": {
        "page_width_mm": 297,
        "page_height_mm": 210,
        "insert_height_mm": 183,
        "left_flap_width_mm": 0,
        "back_width_mm": 129.5,
        "spine_width_mm": 14,
        "front_width_mm": 129.5,
        "right_flap_width_mm": 0,
        "title_size_pt": 24,
        "subtitle_size_pt": 11.5,
        "body_size_pt": 10.5,
        "columns": "1fr 0.9fr",
        "front_tag": "DVD",
        "duplex_spine": False,
    },
    "small_case": {
        "page_width_mm": 297,
        "page_height_mm": 210,
        "insert_height_mm": 120,
        "reverse_height_mm": 117,
        "left_flap_width_mm": 7,
        "back_width_mm": 120,
        "spine_width_mm": 7,
        "front_width_mm": 120,
        "right_flap_width_mm": 7,
        "reverse_left_width_mm": 7,
        "reverse_back_width_mm": 117,
        "reverse_right_width_mm": 7,
        "title_size_pt": 18,
        "subtitle_size_pt": 9.5,
        "body_size_pt": 8.5,
        "columns": "1fr 0.92fr",
        "front_tag": "ARCHIWUM",
        "duplex_spine": True,
    },
    "small_case_calibrated": {
        "page_width_mm": 297,
        "page_height_mm": 210,
        "insert_height_mm": 125.2,
        "reverse_height_mm": 122.6,
        "left_flap_width_mm": 7.3,
        "back_width_mm": 120,
        "spine_width_mm": 7,
        "front_width_mm": 125.2,
        "right_flap_width_mm": 7,
        "reverse_left_width_mm": 7.3,
        "reverse_back_width_mm": 142,
        "reverse_right_width_mm": 7.3,
        "title_size_pt": 18,
        "subtitle_size_pt": 9.5,
        "body_size_pt": 8.5,
        "columns": "1fr 0.92fr",
        "front_tag": "ARCHIWUM",
        "duplex_spine": True,
    },
}


def get_layout(name: str) -> dict[str, float | str | bool]:
    if name not in LAYOUTS:
        raise ValueError(f"Unknown layout '{name}'. Available: {', '.join(sorted(LAYOUTS))}")

    layout = dict(LAYOUTS[name])
    layout["insert_width_mm"] = (
        layout["left_flap_width_mm"]
        + layout["back_width_mm"]
        + layout["spine_width_mm"]
        + layout["front_width_mm"]
        + layout["right_flap_width_mm"]
    )
    layout["page_margin_left_mm"] = (layout["page_width_mm"] - layout["insert_width_mm"]) / 2
    layout["page_margin_top_mm"] = (layout["page_height_mm"] - layout["insert_height_mm"]) / 2
    layout["reverse_height_mm"] = layout.get("reverse_height_mm", layout["insert_height_mm"])
    layout["reverse_left_width_mm"] = layout.get("reverse_left_width_mm", layout["left_flap_width_mm"])
    layout["reverse_back_width_mm"] = layout.get("reverse_back_width_mm", layout["back_width_mm"])
    layout["reverse_right_width_mm"] = layout.get("reverse_right_width_mm", layout["right_flap_width_mm"])
    layout["reverse_width_mm"] = (
        layout["reverse_left_width_mm"]
        + layout["reverse_back_width_mm"]
        + layout["reverse_right_width_mm"]
    )
    layout["reverse_page_margin_left_mm"] = (layout["page_width_mm"] - layout["reverse_width_mm"]) / 2
    layout["reverse_page_margin_top_mm"] = (layout["page_height_mm"] - layout["reverse_height_mm"]) / 2
    layout["back_left_mm"] = layout["left_flap_width_mm"]
    layout["spine_left_mm"] = layout["left_flap_width_mm"] + layout["back_width_mm"]
    layout["front_left_mm"] = (
        layout["left_flap_width_mm"] + layout["back_width_mm"] + layout["spine_width_mm"]
    )
    layout["right_flap_left_mm"] = (
        layout["left_flap_width_mm"]
        + layout["back_width_mm"]
        + layout["spine_width_mm"]
        + layout["front_width_mm"]
    )
    return layout


def load_inserts(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Input JSON must contain a list of inserts.")
    return data


def html_list(items: list[str], css_class: str) -> str:
    if not items:
        return ""
    lines = [f'<ul class="{css_class}">']
    for item in items:
        lines.append(f"<li>{escape(item)}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def shorten_subtitle(subtitle: str) -> str:
    if subtitle.lower().startswith("archiwum "):
        return subtitle[9:]
    return subtitle


def spine_lines(title: str, subtitle: str) -> str:
    joined = " ".join(part for part in [title, shorten_subtitle(subtitle)] if part).strip()
    parts = wrap(joined, width=18)[:4]
    return "".join(f"<span>{escape(part)}</span>" for part in parts)


def side_label_lines(text: str) -> str:
    text = text.strip()
    if " " in text:
        head, tail = text.split(" ", 1)
        tail_parts = wrap(tail, width=12)[:5]
        lines = [f"<span>{escape(head)}</span>", '<span class="label-gap"></span>']
        lines.extend(f"<span>{escape(part)}</span>" for part in tail_parts)
        return "".join(lines)
    else:
        parts = wrap(text, width=12)[:6]
    return "".join(f"<span>{escape(part)}</span>" for part in parts)


def render_front_page(item: dict[str, object], layout_name: str, wrap_page: bool = True) -> str:
    title = str(item.get("title", "Tytul kolekcji"))
    subtitle = str(item.get("subtitle", "")).strip()
    discs = item.get("discs", [])
    highlights = item.get("highlights", [])
    layout = get_layout(layout_name)

    if not isinstance(discs, list):
        raise ValueError(f"'discs' for '{title}' must be a list.")
    if not isinstance(highlights, list):
        raise ValueError(f"'highlights' for '{title}' must be a list.")

    disc_items = [str(entry) for entry in discs]
    highlight_items = [str(entry) for entry in highlights]
    subtitle_block = f'<p class="subtitle">{escape(subtitle)}</p>' if subtitle else ""
    left_flap = '<div class="flap left-flap"></div>' if layout["left_flap_width_mm"] else ""
    right_flap = '<div class="flap right-flap"></div>' if layout["right_flap_width_mm"] else ""
    spine_content = ""
    side_label_text = str(item.get("side_label", title)).strip()
    side_label = side_label_lines(side_label_text)
    if not layout["duplex_spine"]:
        spine_content = f'<div class="spine-text">{spine_lines(title, subtitle)}</div>'

    if layout["duplex_spine"]:
        content = f"""
      <div class="insert front-only-insert">
        <div class="insert-outline"></div>
        <div class="panel front front-only-panel">
          <div class="front-frame">
            <div class="eyebrow">Kolekcja</div>
            <h1>{escape(title)}</h1>
            {subtitle_block}
            <div class="front-tag">{escape(str(layout["front_tag"]))}</div>
          </div>
        </div>
      </div>
        """.strip()
        if not wrap_page:
            return content
        return f"""
    <section class="page">
      {content}
    </section>
    """.strip()

    content = f"""
      <div class="insert">
        <div class="insert-outline"></div>
        {left_flap}
        <div class="panel back">
          <div class="eyebrow">Archiwum</div>
          <h1>{escape(title)}</h1>
          {subtitle_block}
          <div class="columns">
            <div>
              <h2>Zawartosc</h2>
              {html_list(disc_items, "disc-list")}
            </div>
            <div>
              <h2>Opis</h2>
              {html_list(highlight_items, "highlight-list")}
            </div>
          </div>
        </div>
        <div class="spine">{spine_content}</div>
        <div class="panel front">
          <div class="front-frame">
            <div class="eyebrow">Kolekcja</div>
            <h1>{escape(title)}</h1>
            {subtitle_block}
            <div class="front-tag">{escape(str(layout["front_tag"]))}</div>
          </div>
        </div>
        {right_flap}
      </div>
    """.strip()
    if not wrap_page:
        return content
    return f"""
    <section class="page">
      {content}
    </section>
    """.strip()


def render_reverse_page(item: dict[str, object], layout_name: str, wrap_page: bool = True) -> str:
    layout = get_layout(layout_name)
    if not layout["duplex_spine"]:
        return ""

    title = str(item.get("title", "Tytul kolekcji"))
    subtitle = str(item.get("subtitle", "")).strip()
    discs = item.get("discs", [])
    highlights = item.get("highlights", [])

    if not isinstance(discs, list):
        raise ValueError(f"'discs' for '{title}' must be a list.")
    if not isinstance(highlights, list):
        raise ValueError(f"'highlights' for '{title}' must be a list.")

    disc_items = [str(entry) for entry in discs]
    highlight_items = [str(entry) for entry in highlights]
    side_label_text = str(item.get("side_label", title)).strip()
    side_label = side_label_lines(side_label_text)

    content = f"""
      <div class="insert reverse-insert">
        <div class="insert-outline"></div>
        <div class="reverse-spine">
          <div class="narrow-flap-text narrow-flap-text-light">{side_label}</div>
        </div>
        <div class="panel back reverse-back">
          <div class="eyebrow">Archiwum</div>
          <h1>{escape(title)}</h1>
          <p class="subtitle">{escape(subtitle)}</p>
          <div class="columns reverse-columns">
            <div>
              <h2>Zawartosc</h2>
              {html_list(disc_items, "disc-list")}
            </div>
            <div>
              <h2>Opis</h2>
              {html_list(highlight_items, "highlight-list")}
            </div>
          </div>
        </div>
        <div class="reverse-flap">
          <div class="narrow-flap-text narrow-flap-text-light">{side_label}</div>
        </div>
      </div>
    """.strip()
    if not wrap_page:
        return content
    return f"""
    <section class="page reverse-page">
      {content}
    </section>
    """.strip()


def render_document(
    inserts: list[dict[str, object]], layout_name: str, sheet_mode: str = "separate-pages"
) -> str:
    layout = get_layout(layout_name)
    pages: list[str] = []
    if sheet_mode == "single-page" and layout["duplex_spine"]:
        for item in inserts:
            front = render_front_page(item, layout_name, wrap_page=False)
            reverse = render_reverse_page(item, layout_name, wrap_page=False)
            pages.append(
                f"""
    <section class="page single-sheet-page">
      <div class="single-sheet">
        <div class="sheet-front">{front}</div>
        <div class="sheet-back">{reverse}</div>
      </div>
    </section>
                """.strip()
            )
    else:
        for item in inserts:
            pages.append(render_front_page(item, layout_name))
            reverse = render_reverse_page(item, layout_name)
            if reverse:
                pages.append(reverse)

    return f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Wkladki</title>
  <style>
    @page {{
      size: A4 landscape;
      margin: 0;
    }}

    * {{
      box-sizing: border-box;
    }}

    html, body {{
      margin: 0;
      padding: 0;
      font-family: "Liberation Sans", Arial, sans-serif;
      color: #1f2933;
      background: #ffffff;
    }}

    .page {{
      width: {layout["page_width_mm"]}mm;
      height: {layout["page_height_mm"]}mm;
      padding: {layout["page_margin_top_mm"]}mm {layout["page_margin_left_mm"]}mm;
      position: relative;
      background: #ffffff;
      page-break-after: always;
      overflow: hidden;
    }}

    .page:last-child {{
      page-break-after: auto;
    }}

    .single-sheet-page {{
      padding: 16mm 10.5mm;
    }}

    .single-sheet {{
      display: flex;
      align-items: flex-start;
      gap: 6mm;
    }}

    .sheet-front,
    .sheet-back {{
      position: relative;
    }}

    .sheet-back .reverse-insert {{
      margin-top: 1.5mm;
      margin-left: -6mm;
    }}

    .insert {{
      width: {layout["insert_width_mm"]}mm;
      height: {layout["insert_height_mm"]}mm;
      display: grid;
      grid-template-columns:
        {layout["left_flap_width_mm"]}mm
        {layout["back_width_mm"]}mm
        {layout["spine_width_mm"]}mm
        {layout["front_width_mm"]}mm
        {layout["right_flap_width_mm"]}mm;
      border: 0.25mm solid #94a3b8;
      position: relative;
      background: white;
      box-shadow: none;
    }}

    .insert-outline {{
      position: absolute;
      inset: 0;
      border: 0.2mm solid #64748b;
      pointer-events: none;
      z-index: 4;
    }}

    .insert::before,
    .insert::after,
    .insert .left-flap::before,
    .insert .right-flap::before,
    .reverse-insert::before,
    .reverse-insert::after {{
      content: "";
      position: absolute;
      top: -2.5mm;
      bottom: -2.5mm;
      width: 0;
      border-left: 0.2mm dashed rgba(71, 85, 105, 0.55);
      pointer-events: none;
    }}

    .insert .left-flap::before {{
      left: 0;
    }}

    .insert::before {{
      left: {layout["spine_left_mm"]}mm;
    }}

    .insert::after {{
      left: {layout["front_left_mm"]}mm;
    }}

    .insert .right-flap::before {{
      left: 0;
    }}

    .panel {{
      padding: 8mm 8.5mm;
      position: relative;
      overflow: hidden;
    }}

    .back {{
      background: #ffffff;
    }}

    .front {{
      background: #ffffff;
    }}

    .spine,
    .reverse-spine,
    .reverse-flap {{
      background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%);
      color: white;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      border-left: 0.25mm solid rgba(255, 255, 255, 0.15);
      border-right: 0.25mm solid rgba(255, 255, 255, 0.15);
    }}

    .spine-text {{
      writing-mode: vertical-rl;
      transform: rotate(180deg);
      display: flex;
      gap: 1mm;
      text-transform: uppercase;
      letter-spacing: 0.25mm;
      font-size: 6.2pt;
      font-weight: 700;
      text-align: center;
      line-height: 1.05;
    }}

    .spine-text span {{
      display: block;
    }}

    .flap {{
      position: relative;
      background: #ffffff;
    }}

    .dark-flap {{
      background: #0f172a;
    }}

    .eyebrow {{
      font-size: 6.5pt;
      text-transform: uppercase;
      letter-spacing: 0.8mm;
      color: #52606d;
      margin-bottom: 3mm;
      font-weight: 700;
    }}

    h1 {{
      margin: 0;
      font-size: {layout["title_size_pt"]}pt;
      line-height: 1.05;
      font-weight: 800;
      max-width: 92%;
    }}

    .subtitle {{
      margin: 3mm 0 0;
      font-size: {layout["subtitle_size_pt"]}pt;
      line-height: 1.35;
      max-width: 92%;
      color: #334e68;
    }}

    .columns {{
      display: grid;
      grid-template-columns: {layout["columns"]};
      gap: 5mm;
      margin-top: 6mm;
      align-items: start;
    }}

    h2 {{
      margin: 0 0 3mm;
      font-size: 7.5pt;
      text-transform: uppercase;
      letter-spacing: 0.45mm;
      color: #102a43;
    }}

    ul {{
      margin: 0;
      padding-left: 4mm;
    }}

    li {{
      margin-bottom: 1.5mm;
      font-size: {layout["body_size_pt"]}pt;
      line-height: 1.25;
    }}

    .front-frame {{
      height: 100%;
      border: 0.4mm solid rgba(16, 42, 67, 0.18);
      border-radius: 2mm;
      padding: 8mm 7mm;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background: #ffffff;
    }}

    .front-tag {{
      align-self: flex-start;
      border: 0.35mm solid #102a43;
      padding: 1.8mm 4mm;
      border-radius: 999px;
      font-size: 7pt;
      font-weight: 700;
      letter-spacing: 0.45mm;
      text-transform: uppercase;
    }}

    .reverse-page {{
      background: #ffffff;
      padding: {layout["reverse_page_margin_top_mm"]}mm {layout["reverse_page_margin_left_mm"]}mm;
    }}

    .reverse-insert {{
      width: {layout["reverse_width_mm"]}mm;
      height: {layout["reverse_height_mm"]}mm;
      grid-template-columns:
        {layout["reverse_left_width_mm"]}mm
        {layout["reverse_back_width_mm"]}mm
        {layout["reverse_right_width_mm"]}mm;
      background: #ffffff;
    }}

    .reverse-insert::before {{
      left: {layout["reverse_left_width_mm"]}mm;
    }}

    .reverse-insert::after {{
      left: {layout["reverse_left_width_mm"] + layout["reverse_back_width_mm"]}mm;
    }}

    .reverse-fill {{
      background: #ffffff;
    }}

    .front-only-insert {{
      width: {layout["front_width_mm"]}mm;
      grid-template-columns: {layout["front_width_mm"]}mm;
    }}

    .front-only-insert::before {{
      display: none;
    }}

    .front-only-insert::after {{
      display: none;
    }}

    .front-only-panel {{
      width: {layout["front_width_mm"]}mm;
      height: {layout["insert_height_mm"]}mm;
    }}

    .front-only-panel .front-frame {{
      padding: 10mm;
    }}

    .narrow-flap-text {{
      writing-mode: vertical-rl;
      transform: rotate(180deg);
      transform-origin: center;
      display: flex;
      gap: 0.1mm;
      text-transform: uppercase;
      letter-spacing: 0;
      font-size: 5mm;
      font-weight: 700;
      text-align: center;
      line-height: 1;
      color: #334e68;
      white-space: nowrap;
    }}

    .narrow-flap-text span {{
      display: block;
    }}

    .narrow-flap-text .label-gap {{
      height: 2.2mm;
    }}

    .narrow-flap-text-light {{
      color: #ffffff;
    }}

    .reverse-back {{
      width: {layout["reverse_back_width_mm"]}mm;
      height: {layout["reverse_height_mm"]}mm;
    }}

    .reverse-columns {{
      margin-top: 4.5mm;
      gap: 4mm;
    }}

    .reverse-back h1 {{
      font-size: 15pt;
    }}

    .reverse-back .subtitle {{
      font-size: 8.5pt;
      margin-top: 2.5mm;
    }}

    .reverse-back li {{
      font-size: 7.4pt;
      margin-bottom: 1.1mm;
      line-height: 1.2;
    }}

    .reverse-flap {{
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }}
  </style>
</head>
<body>
{"\n".join(pages)}
</body>
</html>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json")
    parser.add_argument("output_html")
    parser.add_argument("--layout", default="dvd", choices=sorted(LAYOUTS))
    parser.add_argument("--sheet-mode", default="separate-pages", choices=["separate-pages", "single-page"])
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    source = Path(args.input_json)
    target = Path(args.output_html)
    inserts = load_inserts(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_document(inserts, args.layout, args.sheet_mode), encoding="utf-8")
    print(f"Generated {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
