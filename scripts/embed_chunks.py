"""Embed chunks.jsonl with a local Ollama embedding model, saved as resumable shards.

Usage: python embed_chunks.py data/chunks.jsonl data/emb
Requires: pip install requests numpy   (and Ollama running with the model pulled)

Writes data/emb/shard_00000.npz ... (vectors + chunk_ids) and data/emb/meta.json.
Re-running skips shards that already exist, so an interrupted run resumes.
"""
import json
import os
import sys
import time

import numpy as np
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = "nomic-embed-text"
DOC_PREFIX = "search_document: "    # nomic requires task prefixes
QUERY_PREFIX = "search_query: "
USE_HEADER = True                   # prepend "[Volume N | title]" to each chunk
SHARD_SIZE = 1024                   # chunks per saved file
BATCH = 32                          # chunks per HTTP request


def embed(texts):
    r = requests.post(f"{OLLAMA_URL}/api/embed",
                      json={"model": MODEL, "input": texts}, timeout=600)
    r.raise_for_status()
    return r.json()["embeddings"]


def doc_text(rec):
    body = f"{rec['header']}\n\n{rec['text']}" if USE_HEADER else rec["text"]
    return DOC_PREFIX + body


def main(chunks_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(chunks_path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f]
    n_shards = (len(recs) + SHARD_SIZE - 1) // SHARD_SIZE
    print(f"{len(recs):,} chunks -> {n_shards} shards")

    dims = None
    t0, done = time.time(), 0
    for s in range(n_shards):
        path = os.path.join(out_dir, f"shard_{s:05d}.npz")
        part = recs[s * SHARD_SIZE:(s + 1) * SHARD_SIZE]
        if os.path.exists(path):
            continue
        vecs = []
        for i in range(0, len(part), BATCH):
            vecs.extend(embed([doc_text(r) for r in part[i:i + BATCH]]))
        arr = np.asarray(vecs, dtype=np.float32)
        dims = arr.shape[1]
        np.savez(path, vectors=arr, chunk_ids=np.array([r["chunk_id"] for r in part]))
        done += len(part)
        rate = done / (time.time() - t0)
        left = sum(len(recs[k * SHARD_SIZE:(k + 1) * SHARD_SIZE]) for k in range(s + 1, n_shards))
        print(f"shard {s + 1}/{n_shards}  {rate:.1f} chunks/s  ~{left / rate / 60:.1f} min left")

    if dims is None:  # everything already existed; read dims from a shard
        dims = int(np.load(os.path.join(out_dir, "shard_00000.npz"))["vectors"].shape[1])
    meta = {"model": MODEL, "dims": dims, "doc_prefix": DOC_PREFIX,
            "query_prefix": QUERY_PREFIX, "use_header": USE_HEADER,
            "n_chunks": len(recs), "shard_size": SHARD_SIZE}
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=2)
    print("done:", meta)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
