"""Parse The Wandering Inn epub into one JSON record per chapter.

Assumes one chapter per spine file (check with inspect_epub.py first).
Usage: python parse_epub.py book.epub chapters.jsonl
"""
import json
import re
import sys

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

MIN_WORDS = 200  # skip cover, title page, copyright, etc.
VOLUME_RE = re.compile(r"^\s*(\d+)\.\d+")  # "7.12 E" -> volume 7


def flatten_toc(toc):
    for entry in toc:
        if isinstance(entry, tuple):
            section, children = entry
            if getattr(section, "href", None):
                yield section.title, section.href
            yield from flatten_toc(children)
        else:
            yield entry.title, entry.href


def extract_paragraphs(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = [re.sub(r"\s+", " ", p) for p in paras if p]
    if not paras:  # some epubs put text in divs
        paras = [line for line in soup.get_text("\n", strip=True).split("\n") if line]
    heading = soup.find(["h1", "h2", "h3"])
    return paras, (heading.get_text(" ", strip=True) if heading else None)


def main(epub_path, out_path):
    book = epub.read_epub(epub_path)
    toc_titles = {}
    for title, href in flatten_toc(book.toc):
        toc_titles.setdefault(href.split("#")[0], title)

    chapter_idx = 0
    volume = None
    total_words = 0
    skipped = []

    with open(out_path, "w", encoding="utf-8") as out:
        for idref, _linear in book.spine:
            item = book.get_item_with_id(idref)
            if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            name = item.get_name()
            paras, heading = extract_paragraphs(item.get_content())
            text = "\n\n".join(paras)  # keep paragraph breaks for chunking
            words = len(text.split())
            if words < MIN_WORDS:
                skipped.append((name, words))
                continue

            title = toc_titles.get(name) or heading or name
            m = VOLUME_RE.match(title)
            if m:
                volume = int(m.group(1))
            # interludes and side stories inherit the current volume

            record = {
                "chapter_idx": chapter_idx,
                "title": title,
                "volume": volume,
                "source_file": name,
                "word_count": words,
                "text": text,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            chapter_idx += 1
            total_words += words

    print(f"chapters written: {chapter_idx}")
    print(f"total words: {total_words:,}")
    print(f"skipped {len(skipped)} short items: {skipped[:10]}{' ...' if len(skipped) > 10 else ''}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
