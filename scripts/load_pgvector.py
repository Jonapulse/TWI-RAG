"""Load chunks + embedding shards into Postgres/pgvector and build indexes.

Usage: python load_pgvector.py data/chunks.jsonl data/emb
Requires: pip install "psycopg[binary]" pgvector numpy
Env: DATABASE_URL (default postgresql://postgres:twi@localhost:5432/twi)

Drops and recreates the `chunks` table each run, so it is safe to rerun.
"""
import glob
import json
import os
import sys

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:twi@localhost:5432/twi")


def main(chunks_path, emb_dir):
    with open(os.path.join(emb_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    dims = meta["dims"]

    vec_by_id = {}
    for path in sorted(glob.glob(os.path.join(emb_dir, "shard_*.npz"))):
        z = np.load(path)
        for cid, v in zip(z["chunk_ids"], z["vectors"]):
            vec_by_id[str(cid)] = v
    with open(chunks_path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f]
    missing = [r["chunk_id"] for r in recs if r["chunk_id"] not in vec_by_id]
    if missing:
        sys.exit(f"{len(missing)} chunks have no embedding (e.g. {missing[:3]}); rerun embed_chunks.py")

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute(f"""
            CREATE TABLE chunks (
                chunk_id    text PRIMARY KEY,
                chapter_idx int  NOT NULL,
                chunk_idx   int  NOT NULL,
                volume      int,
                title       text NOT NULL,
                char_start  int  NOT NULL,
                char_end    int  NOT NULL,
                token_count int  NOT NULL,
                text        text NOT NULL,
                embedding   vector({dims}) NOT NULL,
                tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
            )""")

        rows = [(r["chunk_id"], r["chapter_idx"], r["chunk_idx"], r["volume"], r["title"],
                 r["char_start"], r["char_end"], r["token_count"], r["text"], vec_by_id[r["chunk_id"]])
                for r in recs]
        with conn.cursor() as cur:
            for i in range(0, len(rows), 1000):
                cur.executemany(
                    "INSERT INTO chunks (chunk_id, chapter_idx, chunk_idx, volume, title, "
                    "char_start, char_end, token_count, text, embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows[i:i + 1000])
                print(f"inserted {min(i + 1000, len(rows)):,}/{len(rows):,}")

        print("building indexes (HNSW takes a few minutes)...")
        conn.execute("CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
        conn.execute("CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv)")
        conn.execute("CREATE INDEX chunks_chapter ON chunks (chapter_idx)")
        conn.execute("ANALYZE chunks")
        n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        print(f"done: {n:,} rows, vector({dims}), model {meta['model']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
