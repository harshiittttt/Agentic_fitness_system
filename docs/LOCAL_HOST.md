# Host 4-5 friends from your own PC (Docker + gateway + Tailscale Funnel)

```
phone --https--> https://<pc>.<tailnet>.ts.net  (Tailscale Funnel, :443)
                    --> gateway  127.0.0.1:8000   (deploy/local/gateway.py; the Bearer key picks the person)
                          --> container fitness-<name>  127.0.0.1:8001, 8002, ...   (own volume, own API_TOKEN)
```
Each person is an isolated container from `deploy/new_user.py` (see `docs/DEPLOY.md`). The gateway only routes: every
container still enforces its own `API_TOKEN`, so a gateway bug cannot cross users.

## 1. Containers
1. Install Docker Desktop (WSL2 backend), moving its disk image to D: (Settings > Resources > Advanced > Disk image location). C: has little free space.
2. `copy deploy\shared.env.example deploy\shared.env` and fill `GEMINI_API_KEY` / `ELEVENLABS_API_KEY`.
3. Per person: `python deploy/new_user.py alice`, then `docker compose up -d --build`. Their address+key are in `deploy/users/alice.txt` (replace the URL line by the public address, see 3). Keys are the `token` in `deploy/users/<name>.json`.

## 2. Gateway
```
python deploy/local/gateway.py            # 127.0.0.1:8000; --port N, --users-dir DIR (repeatable)
```
- Reads every `deploy/users/*.json` (new_user.py format `{port, token}`) and `deploy/local/users/*.json`; override with `GATEWAY_USERS_DIRS` (os.pathsep-separated). Files are re-read when they change: adding or rotating a person needs no gateway restart (but `docker compose up -d` for the container).
- `/health` needs no key and answers by itself. Unknown or missing key: 401 + `WWW-Authenticate: Bearer`. CORS preflight is answered by the gateway. Responses (incl. TTS audio) stream; upstream timeout 120 s; backend down gives 502.
- Needs fastapi/uvicorn/httpx only (`pip install -r requirements.txt`). Keys are never logged.
- Autostart: Task Scheduler "at logon" running `python <repo>\deploy\local\gateway.py`; keep the PC awake while plugged in: `powercfg /change standby-timeout-ac 0` (run yourself; not applied by any script). Docker Desktop must also start at logon; containers use `restart: unless-stopped`.

## 3. Public address
**Tailscale Funnel (stable URL, free personal plan).** Install Tailscale, log in, enable MagicDNS + HTTPS certificates and Funnel in the admin console, then:
```
tailscale funnel --bg 8000      # publish 127.0.0.1:8000 at https://<machine>.<tailnet>.ts.net
tailscale funnel status
tailscale funnel --https=443 off   # stop
```
Funnel allows ports 443, 8443 and 10000 only; bandwidth limits are unpublished (voice replies are small, fine for 5 people). Friends need no Tailscale.

**Fallback, no account: Cloudflare quick tunnel.** `cloudflared tunnel --url http://127.0.0.1:8000` prints a random `https://xxxx.trycloudflare.com`. It changes on every start, so every phone must re-enter the address each time. Not for daily use.

## 4. What each person does
App > Connect to your coach: paste the server address (`https://<machine>.<tailnet>.ts.net`, no trailing path) and their access key. Send keys privately.

## 5. Update, backup
`git pull && docker compose up -d --build`. Backups: `docs/DEPLOY.md` section 5 (volumes). `deploy/local/backup.py` copies bare-process `fitness.db` files only (not used with Docker volumes).

## 6. Disk and RAM
Budget roughly 300-500 MB RAM per container plus Docker/WSL2 overhead (cap it with `%UserProfile%\.wslconfig`, `memory=4GB`), against 7.4 GB total. Put Docker's disk image on D:. Test and measure with `docker stats`.

## Not verified
- Real Docker + gateway + Funnel on this PC (Docker not installed when this was written).
- Exact current `tailscale funnel` syntax (not checked against Tailscale docs; confirm with `tailscale funnel --help`) and Funnel bandwidth limits.
- Gateway verified only with fake upstream apps in `deploy/local/tests/` (pytest), not against the real backend containers.
- `deploy/local/run.py`, `autostart.ps1`, `backup.py`, `users.py` (bare-process mode, no Docker) are written but untested end to end and no longer the recommended path.
