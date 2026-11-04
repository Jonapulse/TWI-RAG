# Demo: Android app ↔ RAG backend

```
UserSide (Android, Compose) ──HTTP POST /ask──▶ scripts/api_server.py (this repo, Python)
                                                  ├─ Postgres + pgvector (Docker: twi-pg)
                                                  └─ Ollama (native): nomic-embed-text + llama3.1:8b
```

There's one small Python process and no extra containers. `api_server.py` reuses `ask_cli.py`'s
prompts, provider calls and logging, so its answers match the CLI exactly for the same settings.
It appends every query to `data/logs/ask.jsonl`, tagged `"client": "api"`.

## 1. Start the backend (on the PC with the data)

1. Make sure the Ollama app is running.
2. Then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1             # no spoiler cutoff
powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1 -MaxChapter 586   # default cutoff = end of Vol 8
```

The script starts Docker Desktop if it isn't running, starts `twi-pg` and waits for Postgres,
checks Ollama, then serves on `0.0.0.0:8000`.

To check it:
- Open http://localhost:8000/health in a browser. It should say `"ok": true`.
- http://localhost:8000/ is a bare-bones browser page for trying queries without the app.

Each query takes about 35–50 s on the local 8B model (the baseline and RAG answers run one
after the other). The first query after a cold start takes about 15 s longer while the model loads.
Concurrent requests queue, so they don't fight over the GPU.

### API

`POST /ask` takes `{"question": "...", "max_chapter": 586?, "k": 8?, "include_context": false?}`.
Leaving out `max_chapter` uses the server default. `0` turns the cutoff off.

It returns:

```json
{"question": "...", "model": "llama3.1:8b", "k": 8, "max_chapter": 586, "retrieval_seconds": 3.6,
 "retrieved": [{"chunk_id": "0541-007", "chapter_idx": 541, "title": "8.48 H", "score": 0.7109}, ...],
 "baseline": {"answer": "...", "seconds": 16.8, "prompt_tokens": 64, "output_tokens": 16, "truncation_warning": false},
 "rag":      {"answer": "...", "seconds": 30.5, "prompt_tokens": 4465, "output_tokens": 34, "truncation_warning": false}}
```

## 2. Run the app (UserSide)

The app reads the server URL from the Gradle property `ragApiUrl`, which is baked into
`BuildConfig.RAG_API_URL`. The default is `http://10.0.2.2:8000`, the emulator's alias for the PC it runs on.

| Where the app runs | `ragApiUrl` | Notes |
|---|---|---|
| Emulator on the backend PC | (default) | Nothing else to set up. |
| Phone on USB to the backend PC | `http://localhost:8000` | First run `adb reverse tcp:8000 tcp:8000`. No firewall changes needed. |
| Phone or emulator on another machine, same Wi-Fi | `http://<backend PC LAN IP>:8000` | Windows Firewall must allow inbound TCP 8000 (see below). |
| Partner somewhere else | a tunnel URL (see below) | |

To set the URL:
- In Android Studio, add `ragApiUrl=http://...` to `UserSide/gradle.properties` (or `~/.gradle/gradle.properties` to keep it out of the repo).
- On the command line, use `gradlew assembleDebug -PragApiUrl=http://...`.

To work on the UI without the backend, swap `HttpSearchRepository(...)` for `TestSearchRepository()` in `SearchViewModel.kt`.

### Letting a partner demo it

- **Same network:** run this once in an *admin* PowerShell on the backend PC, then use the PC's LAN IP from `ipconfig`:
  ```powershell
  New-NetFirewallRule -DisplayName "TWI RAG API 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
  ```
- **Different network:** put a tunnel in front of port 8000 and build the app with that URL. Options:
  - Tailscale on both machines (private), then `http://<tailscale-ip>:8000`.
  - `cloudflared tunnel --url http://localhost:8000` (public https URL; anyone with the link can query it).
- **Without the app:** the same URL in a browser (`/`) gives the minimal demo page.

## Troubleshooting

- **App says "Can't reach / Couldn't connect to the RAG server at …":** the server isn't running, or the URL or firewall is wrong. Check `/health` from the device's browser.
- **Android 17+ (API 37, which the app targets):** connections to LAN addresses, *including the emulator's `10.0.2.2`*, are silently dropped unless the app has the **Nearby devices** (`ACCESS_LOCAL_NETWORK`) permission.
  The app asks for it on launch when the URL isn't loopback. If someone tapped "Don't allow", re-enable it in Settings → Apps → UserFacing → Permissions.
  `localhost` via `adb reverse` and public tunnel URLs don't need it.
- **`/health` shows a `db` error:** run `docker start twi-pg` and wait a few seconds.
- **`/health` shows an `ollama` error:** start the Ollama app, or `ollama pull llama3.1:8b`.
- **Gradle can't find the SDK:** on this PC the user-level `ANDROID_HOME` points at `C:\Users\Door to Everything\...`.
  Fix that variable, or add `sdk.dir=C\:\\Users\\jonaDev\\AppData\\Local\\Android\\Sdk` to `UserSide/local.properties`.
  Android Studio writes `local.properties` for you.
