"""Turn the markdown docs into PDFs.

    python docs/build_pdf.py

Needs Chrome or Edge (already on any Windows machine) and the `markdown`
package. The mermaid diagrams have to be drawn by a real browser, so the
script builds an HTML page, serves it on a throwaway local port, and prints
it with headless Chrome. Mermaid is fetched from a CDN, so you need internet
the first time you run this.
"""

import http.server
import re
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
SOURCES = ["specification-and-design.md"]

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  @page {{ size: Letter; margin: 0.8in 0.75in; }}
  body {{ font: 10.5pt/1.5 "Segoe UI", Arial, sans-serif; color: #111; margin: 0; }}
  h1 {{ font-size: 21pt; margin: 0 0 .3em; }}
  h2 {{ font-size: 14pt; margin: 1.6em 0 .4em; border-bottom: 1px solid #ccc; padding-bottom: .2em; }}
  h3 {{ font-size: 11.5pt; margin: 1.2em 0 .3em; }}
  h1, h2, h3 {{ break-after: avoid; }}
  p, li {{ orphans: 2; widows: 2; }}
  code {{ font: 9.5pt "Consolas", monospace; background: #f4f4f4; padding: .5pt 3pt; border-radius: 2px; }}
  table {{ border-collapse: collapse; width: 100%; margin: .8em 0; font-size: 9pt; }}
  th, td {{ border: 1px solid #d0d0d0; padding: 4pt 6pt; text-align: left; vertical-align: top; }}
  th {{ background: #f2f2f2; }}
  tr {{ break-inside: avoid; }}
  hr {{ border: 0; border-top: 1px solid #ddd; margin: 1.5em 0; }}
  .mermaid {{ break-inside: avoid; margin: 1em 0; text-align: center; }}
  .mermaid svg {{ max-width: 100%; height: auto; }}
</style></head>
<body>
{body}
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: false, theme: "neutral",
                        fontFamily: "Segoe UI, Arial, sans-serif" }});
  await mermaid.run();
</script>
</body></html>
"""


def find_browser():
    for path in BROWSERS:
        if Path(path).exists():
            return path
    sys.exit("No Chrome or Edge found. Edit BROWSERS in this script.")


def to_html(md_path):
    text = md_path.read_text(encoding="utf-8")

    # The first heading becomes the PDF's title in Properties and the viewer
    # title bar, so don't leave it as the filename.
    heading = re.search(r"^# (.+)$", text, flags=re.M)
    title = heading.group(1).strip() if heading else md_path.stem

    # Pull the mermaid blocks out first. If we let the markdown converter see
    # them it turns them into <code> and mermaid never picks them up.
    diagrams = []

    def stash(match):
        diagrams.append(match.group(1))
        return f"\n@@DIAGRAM{len(diagrams) - 1}@@\n"

    text = re.sub(r"```mermaid\n(.*?)```", stash, text, flags=re.S)
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])

    for i, diagram in enumerate(diagrams):
        html = html.replace(
            f"<p>@@DIAGRAM{i}@@</p>", f'<pre class="mermaid">{diagram}</pre>'
        )
    return html, title, len(diagrams)


def serve(directory):
    """Serve `directory` on a free port. Chrome is happier with http than file://."""
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(directory), **kw
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def build(md_path, browser):
    html, title, diagram_count = to_html(md_path)
    pdf_path = md_path.with_suffix(".pdf")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "doc.html").write_text(
            PAGE.format(title=title, body=html), encoding="utf-8"
        )
        server, port = serve(tmp)
        profile = tmp / "profile"

        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={profile}",
                "--no-pdf-header-footer",
                # Lets the CDN fetch and the mermaid render finish before printing.
                "--virtual-time-budget=20000",
                f"--print-to-pdf={pdf_path}",
                f"http://127.0.0.1:{port}/doc.html",
            ],
            check=True,
            capture_output=True,
        )
        server.shutdown()

    size_kb = pdf_path.stat().st_size // 1024
    print(f"{pdf_path.name}: {size_kb} KB, {diagram_count} diagrams")
    return pdf_path


if __name__ == "__main__":
    browser = find_browser()
    print(f"Using {Path(browser).name}")
    for name in SOURCES:
        build(DOCS / name, browser)
