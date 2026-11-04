"""Tiny HTTP API over the ask_cli pipeline (baseline vs. RAG), for the Android front end.

Usage (from the project root; normally via scripts\\start_api.ps1):
  .venv\\Scripts\\python.exe scripts\\api_server.py data\\emb                     # 127.0.0.1:8000
  .venv\\Scripts\\python.exe scripts\\api_server.py data\\emb --host 0.0.0.0      # also reachable on the LAN
  set TWI_API_KEY=...  (or --api-key ...)  -> require "Authorization: Bearer <key>" on queries

Endpoints:
  GET  /health      -> {"ok": bool, ...}  (details only when authorized or no key is set)
  POST /jobs        {"question": str, "max_chapter": int?, "k": int?, "include_context": bool?}
                    -> 202 {"id", "status": "queued", "position"}
  GET  /jobs/<id>   -> {"id", "status": queued|running|done|error, "position"?, "result"?, "error"?}
  POST /ask         same body as /jobs, but waits and returns the result directly (fine locally;
                    behind Cloudflare use /jobs, since its proxy gives up on responses after ~100 s)
  GET  /            -> a minimal browser page for trying queries without the app

All LLM work runs on one worker thread, so concurrent users queue instead of fighting over the GPU.
Prompts, providers and the log format come straight from ask_cli.py, so answers match the CLI.
Every query is appended to data/logs/ask.jsonl (with "client": "api").
"""
import argparse
import collections
import datetime
import hmac
import json
import os
import queue
import re
import secrets
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

MAX_BODY = 16 * 1024        # bytes
MAX_QUESTION = 1000         # characters
MAX_QUEUE = 20              # pending jobs before we start returning 503
JOB_TTL = 30 * 60           # seconds a finished job is kept for polling
JOB_PATH = re.compile(r"^/jobs/([A-Za-z0-9_-]{8,64})$")


# ---------- the actual work (unchanged pipeline) ----------

def answer(cfg, question, k, max_ch, include_context):
    model, call, gen = cfg["model"], PROVIDERS[cfg["provider"]], cfg["gen"]
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


# ---------- job queue: one worker thread = one query on the GPU at a time ----------

class Jobs:
    def __init__(self, cfg):
        self.cfg = cfg
        self.q = queue.Queue(maxsize=MAX_QUEUE)
        self.jobs = {}
        self.order = collections.deque()  # ids waiting, for queue position
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, params):
        jid = secrets.token_urlsafe(12)
        job = {"id": jid, "status": "queued", "created": time.time(), "params": params,
               "done": threading.Event()}
        with self.lock:
            self._expire()
            try:
                self.q.put_nowait(jid)
            except queue.Full:
                return None
            self.jobs[jid] = job
            self.order.append(jid)
        return job

    def view(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                return None
            out = {"id": jid, "status": job["status"]}
            if job["status"] == "queued":
                out["position"] = self.order.index(jid) + 1 if jid in self.order else 1
            if "result" in job:
                out["result"] = job["result"]
            if "error" in job:
                out["error"] = job["error"]
            return out

    def _expire(self):
        now = time.time()
        for jid in [j for j, v in self.jobs.items()
                    if v["status"] in ("done", "error") and now - v["finished"] > JOB_TTL]:
            del self.jobs[jid]

    def _worker(self):
        while True:
            jid = self.q.get()
            with self.lock:
                job = self.jobs[jid]
                job["status"] = "running"
                if jid in self.order:
                    self.order.remove(jid)
            p = job["params"]
            try:
                res = answer(self.cfg, p["question"], p["k"], p["max_chapter"], p["include_context"])
                upd = {"status": "done", "result": res}
            except requests.RequestException as e:
                upd = {"status": "error", "error": f"LLM/embedding call failed: {e}"}
            except psycopg.Error as e:
                upd = {"status": "error", "error": f"database error: {e}"}
            except Exception as e:  # noqa: BLE001 -- never let the worker die
                upd = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            with self.lock:
                job.update(upd, finished=time.time())
                job.pop("params", None)
            job["done"].set()


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
textarea,input{font:inherit}textarea{width:100%;box-sizing:border-box}
pre{white-space:pre-wrap;background:#f4f4f4;padding:1em}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1em}@media(max-width:700px){.cols{display:block}}</style>
<h1>Baseline vs. RAG</h1>
<p>API key: <input id=key type=password size=30 autocomplete=off></p>
<textarea id=q rows=3 placeholder="Ask about The Wandering Inn"></textarea>
<p>Spoiler cutoff (max chapter_idx, blank = server default): <input id=m size=5>
<button id=go>Ask</button> <span id=st></span></p>
<div class=cols><div><h3>Baseline</h3><pre id=b></pre></div><div><h3>RAG</h3><pre id=r></pre></div></div>
<h3>Retrieved</h3><pre id=s></pre>
<script>
try{key.value=localStorage.getItem('twiKey')||''}catch(e){}
const hdr=()=>{const h={'Content-Type':'application/json'};if(key.value)h.Authorization='Bearer '+key.value;return h};
async function call(method,path,body){const res=await fetch(path,{method,headers:hdr(),body:body&&JSON.stringify(body)});
const j=await res.json().catch(()=>({}));if(!res.ok)throw new Error(j.error||('HTTP '+res.status));return j}
go.onclick=async()=>{try{localStorage.setItem('twiKey',key.value)}catch(e){}
b.textContent=r.textContent=s.textContent='';go.disabled=true;
const body={question:q.value};if(m.value!=='')body.max_chapter=+m.value;
try{let j=await call('POST','/jobs',body);const t0=Date.now();
while(j.status==='queued'||j.status==='running'){
st.textContent=(j.status==='queued'?'queued (#'+j.position+')':'thinking')+' '+Math.round((Date.now()-t0)/1000)+' s';
await new Promise(ok=>setTimeout(ok,2000));j=await call('GET','/jobs/'+j.id)}
if(j.status==='error')throw new Error(j.error);const x=j.result;
b.textContent=x.baseline.answer+'\\n\\n('+x.baseline.seconds+' s)';
r.textContent=x.rag.answer+'\\n\\n('+x.rag.seconds+' s, '+x.rag.prompt_tokens+' prompt tokens)'+(x.rag.truncation_warning?'\\n!! prompt hit num_ctx':'');
s.textContent=x.retrieved.map(y=>y.title+'  ('+y.score.toFixed(2)+')').join('\\n');st.textContent='';}
catch(e){st.textContent='Error: '+e.message}finally{go.disabled=false}}
</script>"""


def make_handler(cfg, jobs):
    api_key = cfg["api_key"]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(
                body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if not api_key:
                return True
            got = self.headers.get("Authorization", "")
            got = got[7:] if got.startswith("Bearer ") else self.headers.get("X-API-Key", "")
            return hmac.compare_digest(got.strip().encode(), api_key.encode())

        def _deny(self):
            self._send(401, {"error": "missing or wrong API key"})

        def _params(self):
            """Parse and validate a query body. Returns (params, None) or (None, (code, error))."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None, (400, "bad Content-Length")
            if n > MAX_BODY:
                return None, (413, f"body too large (max {MAX_BODY} bytes)")
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                q = str(req.get("question", "")).strip()
                k = int(req.get("k") or cfg["k"])
                mc = req.get("max_chapter")
                max_ch = cfg["max_chapter"] if mc is None else int(mc)
            except (ValueError, TypeError, AttributeError) as e:
                return None, (400, f"bad request: {e}")
            if not q:
                return None, (400, "question is required")
            if len(q) > MAX_QUESTION:
                return None, (400, f"question too long (max {MAX_QUESTION} characters)")
            if not 1 <= k <= 50:
                return None, (400, "k must be 1..50")
            if max_ch < 0:
                return None, (400, "max_chapter must be >= 0")
            return {"question": q, "k": k, "max_chapter": max_ch,
                    "include_context": bool(req.get("include_context"))}, None

        def do_OPTIONS(self):  # CORS preflight
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                st = health(cfg)
                if not self._authorized():  # don't hand internals to anonymous visitors
                    st = {"ok": st["ok"], "auth_required": True}
                self._send(200 if st["ok"] else 503, st)
            elif path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif m := JOB_PATH.match(path):
                if not self._authorized():
                    return self._deny()
                v = jobs.view(m.group(1))
                self._send(200, v) if v else self._send(404, {"error": "unknown or expired job"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path not in ("/jobs", "/ask"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._deny()
            params, err = self._params()
            if err:
                return self._send(err[0], {"error": err[1]})
            job = jobs.submit(params)
            if job is None:
                return self._send(503, {"error": "server busy, try again in a minute"})
            if path == "/jobs":
                return self._send(202, jobs.view(job["id"]))
            job["done"].wait()  # /ask: synchronous convenience
            v = jobs.view(job["id"])
            if v["status"] == "done":
                self._send(200, v["result"])
            else:
                self._send(502, {"error": v.get("error", "failed")})

        def log_message(self, fmt, *args):
            who = self.headers.get("CF-Connecting-IP") if self.headers else None
            sys.stderr.write(f"{self.log_date_time_string()} {who or self.client_address[0]} "
                             f"{fmt % args}\n")

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("emb_dir")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 covers the emulator, adb reverse and cloudflared; "
                         "0.0.0.0 to accept LAN clients too")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--api-key", default=os.environ.get("TWI_API_KEY", ""),
                    help="require this bearer token on queries (default: $TWI_API_KEY; empty = open)")
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
           "api_key": args.api_key.strip(),
           "gen": {"temperature": args.temperature, "num_ctx": args.num_ctx,
                   "max_tokens": args.max_tokens}}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    print(f"health: {json.dumps(health(cfg))}")
    print(f"listening on http://{args.host}:{args.port}  (provider={cfg['provider']} "
          f"model={cfg['model']} prompt={PROMPT_VERSION} "
          f"auth={'API key required' if cfg['api_key'] else 'OFF'})", flush=True)
    if not cfg["api_key"] and args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: listening beyond localhost with no API key", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cfg, Jobs(cfg)))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
