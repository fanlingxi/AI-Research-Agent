"""Standalone render worker: run only in the environment containing pypdfium2."""

import sys
from pathlib import Path


def main():
    import pypdfium2

    pdf_path, output, count = sys.argv[1:]
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    with pypdfium2.PdfDocument(pdf_path) as document:
        for index in range(int(count)):
            page = document[index]
            bitmap = page.render(scale=150 / 72)
            bitmap.to_pil().save(folder / f"{index + 1:03d}.png")
            bitmap.close()
            page.close()


if __name__ == "__main__":
    main()
