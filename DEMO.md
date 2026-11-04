# Demo: Android app ↔ RAG backend

```
TWI-RAG-frontend (Android, Compose)
   │  HTTPS, Authorization: Bearer <key>
   ▼
Cloudflare quick tunnel (https://<random>.trycloudflare.com)   ← only with -Tunnel
   │
   ▼
scripts/api_server.py on 127.0.0.1:8000 (this repo, Python, one worker thread)
   ├─ Postgres + pgvector (Docker: twi-pg)
   └─ Ollama (native): nomic-embed-text + llama3.1:8b
```

There's one small Python process and no extra containers. `api_server.py` reuses `ask_cli.py`'s
prompts, provider calls and logging, so its answers match the CLI exactly for the same settings.
It appends every query to `data/logs/ask.jsonl`, tagged `"client": "api"`.

## 1. Start the backend (on the PC with the data)

1. Make sure the Ollama app is running.
2. Then pick one:

```powershell
# Remote clients anywhere (public https URL + API key):
powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1 -Tunnel

# Only this PC (emulator / USB phone via adb reverse); no key needed:
powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1

# Clients on the home LAN (API key on; needs a firewall rule, see below):
powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1 -Lan
```

Extra flags: `-MaxChapter 586` sets the default spoiler cutoff (end of Volume 8). `-NewKey` rotates the API key. `-Port 8000` changes the port.

The script:
- starts Docker Desktop if it isn't running, starts `twi-pg` and waits for Postgres, and checks Ollama;
- with `-Tunnel`, starts `cloudflared` and prints a box like this:

```
================ give these to remote clients ================
  Server URL : https://<random-words>.trycloudflare.com
  API key    : <key>
```

- **The key** is generated once and stored in `data\api_key.txt`, which is gitignored. It stays the same across restarts. To use your own instead, set `$env:TWI_API_KEY` first.
- **The tunnel URL changes every time the tunnel starts.** The current one is also written to `data\tunnel_url.txt`. Send the new URL to your partner after each restart; they paste it into the app, with no rebuild needed.
- Ctrl+C stops the server, and the tunnel stops with it.

To check it:
- Open `<URL>/health`. Without the key it only says `{"ok": true, "auth_required": true}`. With the key it shows the details.
- `<URL>/` is a bare-bones browser demo page: paste the key into its API key box.

Each query takes about 35–50 s on the local 8B model (the baseline and RAG answers run one after the other).
The first query after a cold start takes about 15 s longer. Concurrent users queue, and the app shows "Queued (#2 in line)".

## 2. Run the app (TWI-RAG-frontend)

1. Install and open the app.
2. Tap **Server settings**, paste the **Server URL** and **API key**, then tap **Test connection**.
   It should say `Connected ✓  llama3.1:8b · 43572 chunks`.
3. Tap **Done** and ask away.

The settings are saved on the device. The build only provides the defaults, using the Gradle properties
`ragApiUrl` (default `http://10.0.2.2:8000`, i.e. the emulator's address for its host PC) and `ragApiKey` (default empty).
Put them in `~/.gradle/gradle.properties` to keep a key out of git.

| Where the app runs | Server URL in the app | Notes |
|---|---|---|
| Anywhere (partner's phone) | the `https://….trycloudflare.com` URL | Backend started with `-Tunnel`. |
| Emulator on the backend PC | `http://10.0.2.2:8000` (default) | Needs the API key only if the backend runs with `-Tunnel`/`-Lan`. |
| Phone on USB to the backend PC | `http://localhost:8000` | First run `adb reverse tcp:8000 tcp:8000`. |
| Same Wi-Fi | `http://<backend LAN IP>:8000` | Backend started with `-Lan`, plus the firewall rule below. |

To work on the UI without the backend, return `TestSearchRepository()` from `repository()` in `SearchViewModel.kt`.

### API

- `POST /jobs` takes `{"question": "...", "max_chapter": 586?, "k": 8?, "include_context": false?}` and returns `202 {"id", "status": "queued", "position"}`.
- `GET /jobs/<id>` returns `{"status": "queued"|"running"|"done"|"error", "position"?, "result"?, "error"?}`. Jobs are kept for 30 minutes after they finish.
- `POST /ask` takes the same body but waits and returns the result directly. That's fine locally, but behind Cloudflare use `/jobs`: Cloudflare drops any response that takes longer than about 100 s, and a busy queue can exceed that.

When a key is set, all of these need `Authorization: Bearer <key>` (or `X-API-Key: <key>`).

The result looks like this:

```json
{"question": "...", "model": "llama3.1:8b", "k": 8, "max_chapter": 586, "retrieval_seconds": 3.6,
 "retrieved": [{"chunk_id": "0541-007", "chapter_idx": 541, "title": "8.48 H", "score": 0.7109}, ...],
 "baseline": {"answer": "...", "seconds": 16.8, "prompt_tokens": 64, "output_tokens": 16, "truncation_warning": false},
 "rag":      {"answer": "...", "seconds": 30.5, "prompt_tokens": 4465, "output_tokens": 34, "truncation_warning": false}}
```

The server also has these limits:
- request body 16 KB, question 1,000 characters, and `k` between 1 and 50;
- up to 20 queued jobs, then it answers `503 busy`.

## Security notes

- **Anyone with the tunnel URL can reach the server**, so with `-Tunnel` the API key is always on. Treat the key like a password. If it leaks, restart with `-NewKey`.
- The server listens on `127.0.0.1` unless you pass `-Lan`. The tunnel connects to it locally, so nothing else on your network can reach it.
- Quick tunnels are meant for demos. They have no uptime guarantee and get a new URL every restart. For a stable URL, use a named Cloudflare tunnel, which needs a free Cloudflare account and a domain.

## Troubleshooting

- **"Wrong or missing API key":** re-paste the key from the startup box or from `data\api_key.txt`.
- **"Can't find …trycloudflare.com":** the tunnel was restarted and its URL changed. Get the new one from `data\tunnel_url.txt`.
- **"Server error 530 from the tunnel":** the tunnel exists but the home server behind it isn't running.
- **"Can't reach / Couldn't connect to the RAG server at …":** the server isn't running, or the URL or firewall is wrong.
- **Android 17+ (API 37, which the app targets):** connections to LAN addresses, *including the emulator's `10.0.2.2`*, are silently dropped unless the app has the **Nearby devices** (`ACCESS_LOCAL_NETWORK`) permission.
  The app asks for it when the server URL is a local address (on launch, and on Test connection or Done). If someone tapped "Don't allow", re-enable it in Settings → Apps → UserFacing → Permissions.
  Tunnel URLs and `localhost` don't need it.
- **`-Lan` mode:** run this once in an *admin* PowerShell. The home network is currently classified as *Public*, so either switch it to Private in Windows settings or add `-Profile Public`:
  ```powershell
  New-NetFirewallRule -DisplayName "TWI RAG API 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
  ```
  NordVPN can also block incoming LAN connections unless its "allow LAN" option is on.
- **Gradle can't find the SDK:** the user-level `ANDROID_HOME` on this PC points at `C:\Users\Door to Everything\...`.
  Fix that variable, or rely on `local.properties` (`sdk.dir=...`), which Android Studio writes for you.
