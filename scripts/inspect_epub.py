"""Inspect an epub's structure before parsing.

Usage: python inspect_epub.py book.epub > inspect.txt
"""
import sys

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup


def flatten_toc(toc, depth=0):
    for entry in toc:
        if isinstance(entry, tuple):  # (Section, children)
            section, children = entry
            yield depth, section.title, getattr(section, "href", None)
            yield from flatten_toc(children, depth + 1)
        else:  # Link
            yield depth, entry.title, entry.href


def main(path):
    book = epub.read_epub(path)

    print("== METADATA ==")
    print("title:", book.get_metadata("DC", "title"))
    print("creator:", book.get_metadata("DC", "creator"))

    print("\n== SPINE (reading order) ==")
    print(f"{'idx':>4}  {'file':45} {'words':>8}  first headings")
    total = 0
    for i, (idref, linear) in enumerate(book.spine):
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "lxml")
        words = len(soup.get_text(" ").split())
        total += words
        heads = [h.get_text(" ", strip=True)[:40] for h in soup.find_all(["h1", "h2", "h3"])][:3]
        n_p = len(soup.find_all("p"))
        print(f"{i:4}  {item.get_name()[:45]:45} {words:8}  p={n_p:<5} {heads}")
    print(f"\nTOTAL WORDS (spine): {total:,}")

    print("\n== TOC (first 40 entries) ==")
    entries = list(flatten_toc(book.toc))
    for depth, title, href in entries[:40]:
        print("  " * depth + f"- {title}  ->  {href}")
    print(f"... {len(entries)} TOC entries total")
    anchored = sum(1 for _, _, h in entries if h and "#" in h)
    print(f"TOC entries with #anchors: {anchored}  (nonzero may mean multiple chapters per file)")

    # Show the tail of a mid-book chapter to spot author notes / boilerplate
    docs = [book.get_item_with_id(i) for i, _ in book.spine]
    docs = [d for d in docs if d is not None and d.get_type() == ebooklib.ITEM_DOCUMENT]
    if docs:
        sample = docs[len(docs) // 2]
        text = BeautifulSoup(sample.get_content(), "lxml").get_text("\n", strip=True)
        print(f"\n== SAMPLE HEAD/TAIL: {sample.get_name()} ==")
        print(text[:600])
        print("\n[...]\n")
        print(text[-1200:])


if __name__ == "__main__":
    main(sys.argv[1])
