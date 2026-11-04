"""Side-by-side answers: closed-book baseline vs. RAG, same model and settings.

Usage:
  python ask_cli.py data/emb                                  # local Ollama (default)
  python ask_cli.py data/emb --provider openai --model gpt-4.1-mini
  python ask_cli.py data/emb --provider anthropic --model claude-sonnet-5-5
Requires: pip install requests numpy "psycopg[binary]" pgvector
Env: OPENAI_API_KEY / ANTHROPIC_API_KEY for API providers.
     OPENAI_BASE_URL to point the openai provider at any OpenAI-compatible API.

Commands at the prompt: :k N, :max N (spoiler cutoff, 0 = off), :ctx (show the last
RAG context), :q (quit). Every query is logged to data/logs/ask.jsonl.
"""
import argparse
import datetime
import json
import os
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests
from pgvector.psycopg import register_vector

from query_cli import DB_URL, OLLAMA_URL, embed_query, search  # same folder

LOG_PATH = os.path.join("data", "logs", "ask.jsonl")
PROMPT_VERSION = "v1"  # bump whenever you edit the prompts below

SYSTEM = ("You answer questions about the web serial The Wandering Inn by pirateaba. "
          "Be concise and specific. If you are not sure of something, say so rather than guessing.")

BASELINE_TEMPLATE = "Question: {question}"

RAG_TEMPLATE = """Below are excerpts from The Wandering Inn, labeled with their chapter.

{context}

Answer the question using only these excerpts. Cite the chapter(s) you used in square
brackets, e.g. [7.12 E]. If the excerpts do not contain the answer, say "Not found in the
provided text." and do not guess.

Question: {question}"""

DEFAULTS = {"ollama": "llama3.1:8b", "openai": "gpt-4.1-mini", "anthropic": "claude-sonnet-5-5"}


# ---------- providers: each takes (model, system, user, cfg) and returns (text, usage) ----------

def call_ollama(model, system, user, cfg):
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=600, json={
        "model": model, "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": cfg["temperature"], "seed": 0, "num_ctx": cfg["num_ctx"],
                    "num_predict": cfg["max_tokens"]},
    })
    r.raise_for_status()
    j = r.json()
    usage = {"prompt_tokens": j.get("prompt_eval_count"), "output_tokens": j.get("eval_count")}
    # Ollama silently drops the start of prompts longer than num_ctx; flag it loudly.
    if usage["prompt_tokens"] and usage["prompt_tokens"] >= cfg["num_ctx"] - 16:
        usage["truncation_warning"] = True
    return j["message"]["content"], usage


def call_openai(model, system, user, cfg):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = {"model": model, "max_tokens": cfg["max_tokens"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if cfg["temperature"] is not None:  # some reasoning models reject temperature
        body["temperature"] = cfg["temperature"]
    r = requests.post(f"{base}/chat/completions", json=body, timeout=600,
                      headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    r.raise_for_status()
    j = r.json()
    u = j.get("usage", {})
    return j["choices"][0]["message"]["content"], {"prompt_tokens": u.get("prompt_tokens"),
                                                    "output_tokens": u.get("completion_tokens")}


def call_anthropic(model, system, user, cfg):
    body = {"model": model, "max_tokens": cfg["max_tokens"], "system": system,
            "messages": [{"role": "user", "content": user}]}
    if cfg["temperature"] is not None:
        body["temperature"] = cfg["temperature"]
    r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=600, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    j = r.json()
    text = "".join(b.get("text", "") for b in j["content"] if b.get("type") == "text")
    u = j.get("usage", {})
    return text, {"prompt_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")}


PROVIDERS = {"ollama": call_ollama, "openai": call_openai, "anthropic": call_anthropic}


# ---------- prompt building ----------

def build_context(rows):
    parts = []
    for i, (cid, ch, title, score, text) in enumerate(rows, 1):
        parts.append(f"[{i}] Chapter: {title}  (chunk {cid})\n{text}")
    return "\n\n---\n\n".join(parts)


def timed(fn, *a):
    t = time.time()
    text, usage = fn(*a)
    return text, usage, round(time.time() - t, 2)


def show(label, text, usage, secs):
    print(f"\n===== {label}  ({secs}s, {usage.get('prompt_tokens')} prompt tokens) =====")
    if usage.get("truncation_warning"):
        print("!! prompt hit num_ctx; Ollama truncated it. Raise --num-ctx or lower k. !!")
    for para in text.strip().split("\n"):
        print(textwrap.fill(para, 100) if para else "")


def log(entry):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("emb_dir")
    ap.add_argument("--provider", choices=PROVIDERS, default="ollama")
    ap.add_argument("--model")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--max-chapter", type=int, default=0)
    ap.add_argument("--num-ctx", type=int, default=8192, help="Ollama context window")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()
    model = args.model or DEFAULTS[args.provider]
    call = PROVIDERS[args.provider]
    cfg = {"temperature": args.temperature, "num_ctx": args.num_ctx, "max_tokens": args.max_tokens}
    with open(os.path.join(args.emb_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    k, max_ch, last_ctx = args.k, args.max_chapter, ""
    print(f"provider={args.provider} model={model} prompt={PROMPT_VERSION}")
    with psycopg.connect(DB_URL) as conn, ThreadPoolExecutor(max_workers=2) as pool:
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
            if q == ":ctx":
                print(last_ctx or "(no query yet)"); continue

            t0 = time.time()
            rows = search(conn, embed_query(meta, q), k, max_ch)
            retrieval_s = round(time.time() - t0, 2)
            last_ctx = build_context(rows)
            base_user = BASELINE_TEMPLATE.format(question=q)
            rag_user = RAG_TEMPLATE.format(context=last_ctx, question=q)

            # Ollama on one GPU runs these one after the other anyway; APIs run in parallel.
            fb = pool.submit(timed, call, model, SYSTEM, base_user, cfg)
            fr = pool.submit(timed, call, model, SYSTEM, rag_user, cfg)
            try:
                b_text, b_usage, b_s = fb.result()
                r_text, r_usage, r_s = fr.result()
            except requests.HTTPError as e:
                print(f"LLM call failed: {e}\n{e.response.text[:500]}")
                continue

            show("BASELINE (no retrieval)", b_text, b_usage, b_s)
            show("RAG", r_text, r_usage, r_s)
            print("\nretrieved:", ", ".join(f"{t} ({s:.2f})" for _, _, t, s, _ in rows))

            log({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                 "query": q, "prompt_version": PROMPT_VERSION,
                 "provider": args.provider, "model": model, "params": cfg,
                 "retrieval": {"k": k, "max_chapter": max_ch or None, "embed_model": meta["model"],
                               "use_header": meta["use_header"], "seconds": retrieval_s,
                               "results": [{"chunk_id": r[0], "chapter_idx": r[1], "title": r[2],
                                            "score": round(float(r[3]), 4)} for r in rows]},
                 "system": SYSTEM,
                 "baseline": {"user": base_user, "answer": b_text, "usage": b_usage,
                              "seconds": b_s, "verdict": None},
                 "rag": {"user": rag_user, "answer": r_text, "usage": r_usage,
                         "seconds": r_s, "verdict": None},
                 "notes": None})


if __name__ == "__main__":
    main()
