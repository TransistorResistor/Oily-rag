"""Extract page URLs from pages/*.json and write them to an HTML file."""
import json
from pathlib import Path

PAGES_DIR = Path(__file__).parent / "pages"
OUTPUT_FILE = Path(__file__).parent / "urls.html"


def collect_links():
    links = []
    for path in sorted(PAGES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        url = data.get("url")
        if not url:
            continue
        title = data.get("title", path.stem)
        links.append((title, url))
    return links


def render_html(links):
    items = "\n".join(
        f'    <li><a href="{url}">{title}</a></li>'
        for title, url in links
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Page URLs</title>
</head>
<body>
<ul>
{items}
</ul>
</body>
</html>
"""


def main():
    links = collect_links()
    OUTPUT_FILE.write_text(render_html(links), encoding="utf-8")
    print(f"Wrote {len(links)} links to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
