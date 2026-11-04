"""Tiny HTTP API over the ask_cli pipeline (baseline vs. RAG), for the Android front end.

Usage (from the project root):
  .venv\\Scripts\\python.exe scripts\\api_server.py data\\emb                 # 0.0.0.0:8000, Ollama
  .venv\\Scripts\\python.exe scripts\\api_server.py data\\emb --max-chapter 586
  .venv\\Scripts\\python.exe scripts\\api_server.py data\\emb --provider anthropic --model claude-sonnet-5-5

Endpoints:
  GET  /health -> {"ok": bool, "db": ..., "ollama": ..., "model": ...}
  POST /ask    {"question": str, "max_chapter": int?, "k": int?, "include_context": bool?}
               -> {"baseline": {...}, "rag": {...}, "retrieved": [...], ...}
  GET  /       -> a minimal browser page for trying queries without the app

Uses only the standard library on top of the project's existing deps. Prompts, providers and
the log format come straight from ask_cli.py, so answers match the CLI for the same settings.
Every /ask is appended to data/logs/ask.jsonl (with "client": "api").
"""
import argparse
import datetime
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import requests
from pgvector.psycopg import register_vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ask_cli import (BASELINE_TEMPLATE, DEFAULTS, PROMPT_VERSION, PROVIDERS, RAG_TEMPLATE,  # noqa: E402
                     SYSTEM, build_context, log, timed)
from query_cli import DB_URL, OLLAMA_URL, embed_query, search  # noqa: E402

LLM_LOCK = threading.Lock()  # one GPU: serialize queries so concurrent demo users just queue


def answer(cfg, question, k, max_ch, include_context):
    model, call = cfg["model"], PROVIDERS[cfg["provider"]]
    gen = cfg["gen"]
    with LLM_LOCK:
        t0 = time.time()
        with psycopg.connect(DB_URL) as conn:
            register_vector(conn)
            rows = search(conn, embed_query(cfg["meta"], question), k, max_ch)
        retrieval_s = round(time.time() - t0, 2)
        ctx = build_context(rows)
        base_user = BASELINE_TEMPLATE.format(question=question)
        rag_user = RAG_TEMPLATE.format(context=ctx, question=question)
        b_text, b_usage, b_s = timed(call, model, SYSTEM, base_user, gen)
        r_text, r_usage, r_s = timed(call, model, SYSTEM, rag_user, gen)

    results = [{"chunk_id": r[0], "chapter_idx": r[1], "title": r[2],
                "score": round(float(r[3]), 4)} for r in rows]
    log({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
         "query": question, "prompt_version": PROMPT_VERSION, "client": "api",
         "provider": cfg["provider"], "model": model, "params": gen,
         "retrieval": {"k": k, "max_chapter": max_ch or None, "embed_model": cfg["meta"]["model"],
                       "use_header": cfg["meta"]["use_header"], "seconds": retrieval_s,
                       "results": results},
         "system": SYSTEM,
         "baseline": {"user": base_user, "answer": b_text, "usage": b_usage,
                      "seconds": b_s, "verdict": None},
         "rag": {"user": rag_user, "answer": r_text, "usage": r_usage,
                 "seconds": r_s, "verdict": None},
         "notes": None})

    def side(text, usage, secs):
        return {"answer": text, "seconds": secs, "prompt_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "truncation_warning": bool(usage.get("truncation_warning"))}

    out = {"question": question, "provider": cfg["provider"], "model": model,
           "prompt_version": PROMPT_VERSION, "k": k, "max_chapter": max_ch or None,
           "retrieval_seconds": retrieval_s, "retrieved": results,
           "baseline": side(b_text, b_usage, b_s), "rag": side(r_text, r_usage, r_s)}
    if include_context:
        out["context"] = ctx
    return out


def health(cfg):
    st = {"model": cfg["model"], "provider": cfg["provider"],
          "default_max_chapter": cfg["max_chapter"] or None}
    try:
        with psycopg.connect(DB_URL, connect_timeout=3) as conn:
            st["db"] = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        st["db"] = f"error: {e}"
    if cfg["provider"] == "ollama":
        try:
            tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
            names = [m["name"] for m in tags.get("models", [])]
            st["ollama"] = "ok" if cfg["model"] in names else f"model {cfg['model']} not pulled"
        except Exception as e:  # noqa: BLE001
            st["ollama"] = f"error: {e}"
    st["ok"] = isinstance(st.get("db"), int) and st.get("ollama", "ok") == "ok"
    return st


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>TWI RAG demo</title>
<style>body{font:16px system-ui;max-width:900px;margin:2em auto;padding:0 16px}
textarea{width:100%;font:inherit}pre{white-space:pre-wrap;background:#f4f4f4;padding:1em}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1em}@media(max-width:700px){.cols{display:block}}</style>
<h1>Baseline vs. RAG</h1>
<textarea id=q rows=3 placeholder="Ask about The Wandering Inn"></textarea>
<p>Spoiler cutoff (max chapter_idx, blank = server default): <input id=m size=5>
<button id=go>Ask</button> <span id=st></span></p>
<div class=cols><div><h3>Baseline</h3><pre id=b></pre></div><div><h3>RAG</h3><pre id=r></pre></div></div>
<h3>Retrieved</h3><pre id=s></pre>
<script>
go.onclick=async()=>{st.textContent='thinking (30-60 s)...';b.textContent=r.textContent=s.textContent='';
const body={question:q.value};if(m.value!=='')body.max_chapter=+m.value;
try{const res=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const j=await res.json();if(!res.ok)throw new Error(j.error||res.status);
b.textContent=j.baseline.answer+'\\n\\n('+j.baseline.seconds+' s)';
r.textContent=j.rag.answer+'\\n\\n('+j.rag.seconds+' s, '+j.rag.prompt_tokens+' prompt tokens)'+(j.rag.truncation_warning?'\\n!! prompt hit num_ctx':'');
s.textContent=j.retrieved.map(x=>x.title+'  ('+x.score.toFixed(2)+')').join('\\n');st.textContent='';}
catch(e){st.textContent='Error: '+e.message}}
</script>"""


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(
                body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):  # CORS preflight, in case anyone points a web page at this
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                st = health(cfg)
                self._send(200 if st["ok"] else 503, st)
            elif self.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/ask":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                q = str(req.get("question", "")).strip()
                k = int(req.get("k") or cfg["k"])
                mc = req.get("max_chapter")
                max_ch = cfg["max_chapter"] if mc is None else int(mc)
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": f"bad request: {e}"})
            if not q:
                return self._send(400, {"error": "question is required"})
            if not 1 <= k <= 50:
                return self._send(400, {"error": "k must be 1..50"})
            try:
                self._send(200, answer(cfg, q, k, max_ch, bool(req.get("include_context"))))
            except requests.RequestException as e:
                self._send(502, {"error": f"LLM/embedding call failed: {e}"})
            except psycopg.Error as e:
                self._send(502, {"error": f"database error: {e}"})

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}\n")

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("emb_dir")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--provider", choices=PROVIDERS, default="ollama")
    ap.add_argument("--model")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--max-chapter", type=int, default=0, help="default spoiler cutoff, 0 = off")
    ap.add_argument("--num-ctx", type=int, default=8192, help="Ollama context window")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()
    with open(os.path.join(args.emb_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    cfg = {"provider": args.provider, "model": args.model or DEFAULTS[args.provider],
           "k": args.k, "max_chapter": args.max_chapter, "meta": meta,
           "gen": {"temperature": args.temperature, "num_ctx": args.num_ctx,
                   "max_tokens": args.max_tokens}}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    print(f"health: {json.dumps(health(cfg))}")
    print(f"listening on http://{args.host}:{args.port}  (provider={cfg['provider']} "
          f"model={cfg['model']} prompt={PROMPT_VERSION})", flush=True)
    ThreadingHTTPServer((args.host, args.port), make_handler(cfg)).serve_forever()


if __name__ == "__main__":
    main()
