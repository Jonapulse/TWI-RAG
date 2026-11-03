"""Interactive retrieval-only test: question in, top-k chunks out. Logs every query.

Usage: python query_cli.py data/emb [--k 10] [--max-chapter 500]
Requires: pip install requests numpy "psycopg[binary]" pgvector

Type a question at the prompt. Commands: :k N  (change k), :max N  (spoiler cutoff,
0 = off), :full N  (print full text of result N from the last query), :q  (quit).
"""
import argparse
import datetime
import json
import os
import sys

import numpy as np
import psycopg
import requests
from pgvector.psycopg import register_vector

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:twi@localhost:5432/twi")
LOG_PATH = os.path.join("data", "logs", "retrieval.jsonl")


def embed_query(meta, q):
    r = requests.post(f"{OLLAMA_URL}/api/embed",
                      json={"model": meta["model"], "input": [meta["query_prefix"] + q]}, timeout=60)
    r.raise_for_status()
    return np.asarray(r.json()["embeddings"][0], dtype=np.float32)


def search(conn, vec, k, max_chapter):
    where = "WHERE chapter_idx <= %(maxc)s" if max_chapter else ""
    sql = f"""SELECT chunk_id, chapter_idx, title, 1 - (embedding <=> %(v)s) AS score, text
              FROM chunks {where}
              ORDER BY embedding <=> %(v)s LIMIT %(k)s"""
    return conn.execute(sql, {"v": vec, "k": k, "maxc": max_chapter}).fetchall()


def log(entry):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("emb_dir")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-chapter", type=int, default=0)
    args = ap.parse_args()
    with open(os.path.join(args.emb_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console: avoid mojibake

    k, max_ch, last = args.k, args.max_chapter, []
    with psycopg.connect(DB_URL) as conn:
        register_vector(conn)
        while True:
            try:
                q = input(f"\n[k={k} max_chapter={max_ch or 'off'}] question> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                continue
            if q == ":q":
                break
            if q.startswith(":k "):
                k = int(q.split()[1]); continue
            if q.startswith(":max "):
                max_ch = int(q.split()[1]); continue
            if q.startswith(":full "):
                n = int(q.split()[1]) - 1
                if 0 <= n < len(last):
                    print(last[n][4])
                continue

            last = search(conn, embed_query(meta, q), k, max_ch)
            for i, (cid, ch, title, score, text) in enumerate(last, 1):
                snippet = " ".join(text.split())[:220]
                print(f"\n{i:2}. {score:.3f}  ch {ch}  {title}  ({cid})\n    {snippet}...")
            log({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                 "query": q, "k": k, "max_chapter": max_ch or None,
                 "model": meta["model"], "use_header": meta["use_header"],
                 "results": [{"chunk_id": r[0], "chapter_idx": r[1], "score": round(float(r[3]), 4)}
                             for r in last],
                 "verdict": None})  # fill in by hand later: hit / partial / miss


if __name__ == "__main__":
    main()
