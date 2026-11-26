"""Local check of the project's parser on any PDF. Developer tool; never used by the app.

Runs the configured parser (RoutingParser: pdfium for text pages, Tesseract for scanned pages)
twice on the same file and reports, without printing document text:
  - pages, characters per page, parser_id, seconds per run
  - whether the two runs produced identical text (reproducibility)
With --dump DIR it also writes each page's text to DIR so you can read it yourself.
Use a gitignored folder for DIR when the PDF holds real data, and never paste the output anywhere.

Run:  uv run --env-file .env python scripts/ocr_check.py path/to/file.pdf [--dpi 300] [--dump DIR]
"""

import argparse
import asyncio
import hashlib
import os
import time
from pathlib import Path


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--dpi", type=int, help="override OCR_DPI for this run")
    ap.add_argument("--dump", type=Path, help="write each page's text to this folder")
    args = ap.parse_args()
    if args.dpi:
        os.environ["OCR_DPI"] = str(args.dpi)

    from chatmed.config import Settings
    from chatmed.parsing import build_parser

    parser = await build_parser(Settings())  # type: ignore[call-arg]
    data = args.pdf.read_bytes()

    runs = []
    for _ in range(2):
        started = time.perf_counter()
        doc = await parser.parse(data)
        runs.append((doc, time.perf_counter() - started))

    doc, seconds = runs[0]
    print(f"file sha256 : {doc.sha256[:16]}...")
    print(f"parser_id   : {doc.parser_id}")
    print(f"pages       : {len(doc.pages)}")
    for page in doc.pages:
        print(f"  page {page.number}: {len(page.text):>6} chars")
    print(f"seconds/run : {seconds:.1f} and {runs[1][1]:.1f}")
    digests = {hashlib.sha256(repr(r[0].pages).encode()).hexdigest() for r in runs}
    print(f"identical   : {len(digests) == 1}")

    if args.dump:
        args.dump.mkdir(parents=True, exist_ok=True)
        for page in doc.pages:
            out = args.dump / f"{args.pdf.stem}.page{page.number}.txt"
            out.write_text(page.text, encoding="utf-8")
        print(f"page texts  : written to {args.dump}")


if __name__ == "__main__":
    asyncio.run(main())
