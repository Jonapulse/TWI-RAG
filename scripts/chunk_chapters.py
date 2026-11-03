"""Split chapters.jsonl into paragraph-aligned, overlapping chunks.

Usage: python chunk_chapters.py data/chapters.jsonl data/chunks.jsonl
Requires: pip install tiktoken
"""
import json
import re
import statistics
import sys

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")  # approximate; fine for sizing

TARGET = 500    # close a chunk once it reaches this many tokens
MAX = 800       # never exceed this
OVERLAP = 80    # carry up to this many tokens of trailing paragraphs forward
MIN_TAIL = 120  # a chapter's last chunk smaller than this merges into the previous one

SENT_RE = re.compile(r"(?<=[.!?\u2026\"\u201d])\s+")


def ntok(s):
    return len(ENC.encode(s, disallowed_special=()))


def sentence_spans(para):
    last = 0
    for m in SENT_RE.finditer(para):
        yield last, m.start()
        last = m.end()
    yield last, len(para)


def word_spans(para, s, e, max_words=300):
    words = [(m.start() + s, m.end() + s) for m in re.finditer(r"\S+", para[s:e])]
    for i in range(0, len(words), max_words):
        grp = words[i:i + max_words]
        yield grp[0][0], grp[-1][1]


def units_for_chapter(text):
    """Yield (start, end, ntokens) spans: paragraphs, or pieces of oversized ones."""
    pos = 0
    for para in text.split("\n\n"):
        start, end = pos, pos + len(para)
        pos = end + 2
        if not para.strip():
            continue
        n = ntok(para)
        if n <= MAX:
            yield start, end, n
            continue
        for s, e in sentence_spans(para):
            if s == e:
                continue
            n = ntok(para[s:e])
            if n <= MAX:
                yield start + s, start + e, n
            else:
                for ws, we in word_spans(para, s, e):
                    yield start + ws, start + we, ntok(para[ws:we])


def overlap_tail(cur):
    """Trailing units (never the first one) totalling at most OVERLAP tokens."""
    tail, tok = [], 0
    for u in reversed(cur[1:]):
        if tok + u[2] > OVERLAP:
            break
        tail.insert(0, u)
        tok += u[2]
    return tail


def pack(units):
    chunks, cur, cur_tok, fresh = [], [], 0, 0

    def close():
        nonlocal cur, cur_tok, fresh
        chunks.append(cur)
        cur = overlap_tail(cur)
        cur_tok = sum(x[2] for x in cur)
        fresh = 0

    for u in units:
        if cur_tok + u[2] > MAX:
            if fresh:
                close()
            if cur_tok + u[2] > MAX:  # overlap would push us over; drop it
                cur, cur_tok = [], 0
        cur.append(u)
        cur_tok += u[2]
        fresh += 1
        if cur_tok >= TARGET:
            close()

    if fresh:
        new_units = cur[-fresh:]
        new_tok = sum(x[2] for x in new_units)
        if chunks and new_tok < MIN_TAIL and sum(x[2] for x in chunks[-1]) + new_tok <= MAX:
            chunks[-1] = chunks[-1] + new_units
        else:
            chunks.append(cur)
    return chunks


def main(in_path, out_path):
    sizes = []
    with open(in_path, encoding="utf-8") as fin, \
         open(out_path, "w", encoding="utf-8", newline="\n") as fout:
        for line in fin:
            ch = json.loads(line)
            text = ch["text"]
            vol = ch["volume"] if ch["volume"] is not None else "?"
            header = f"[Volume {vol} | {ch['title']}]"
            for i, grp in enumerate(pack(units_for_chapter(text))):
                s, e = grp[0][0], grp[-1][1]
                body = text[s:e]
                tokens = ntok(body)
                sizes.append(tokens)
                rec = {
                    "chunk_id": f"{ch['chapter_idx']:04d}-{i:03d}",
                    "chapter_idx": ch["chapter_idx"],
                    "chunk_idx": i,
                    "volume": ch["volume"],
                    "title": ch["title"],
                    "char_start": s,
                    "char_end": e,
                    "token_count": tokens,
                    "header": header,  # prepend to text at embed time
                    "text": body,
                }
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"chunks: {len(sizes):,}")
    print(f"total tokens: {sum(sizes):,}")
    print(f"tokens per chunk: mean {statistics.mean(sizes):.0f}, "
          f"median {statistics.median(sizes):.0f}, min {min(sizes)}, max {max(sizes)}")
    print(f"chunks under {MIN_TAIL} tokens: {sum(1 for n in sizes if n < MIN_TAIL)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
