"""Optional local renderer, run only by the integrator in a PDFium environment."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("output", type=Path)
    parser.add_argument("pages", help="Comma-separated 1-based page numbers")
    parser.add_argument("--dpi", type=float, default=150)
    args = parser.parse_args()
    import pypdfium2 as pdfium

    args.output.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(args.pdf)
    for number in map(int, args.pages.split(",")):
        page = document[number - 1]
        bitmap = page.render(scale=args.dpi / 72)
        bitmap.to_pil().save(args.output / f"{number:03d}.png")
        bitmap.close()
        page.close()
    document.close()


if __name__ == "__main__":
    main()
