# Deploying for a few people (one isolated backend per person)

The app is single-user (SQLite, in-process scheduler). So each person gets their **own container, own volume, own port, own API token**. No code is shared-state between people. Budget: a 2 vCPU / 2-4 GB VPS handles 4-5 instances.

All writable state (`fitness.db`, `tts_cache/`) lives under `FITNESS_DATA_DIR` (`/data` in the image, one named Docker volume per person). Unset locally = unchanged dev behaviour.

## 1. Set up the server (once)
1. Any Linux VPS with Docker + the compose plugin, ports 80/443 open. Clone this repo onto it.
2. `cp deploy/shared.env.example deploy/shared.env` and fill in `GEMINI_API_KEY` / `ELEVENLABS_API_KEY`. This file is git-ignored; never commit it.
3. Install Caddy; copy `deploy/Caddyfile.example` to `/etc/caddy/Caddyfile`. Point DNS (`*.yourdomain` A record) at the server.

## 2. Add a person
```
python deploy/new_user.py alice --domain yourdomain.com
docker compose up -d --build
```
This allocates a port (8001+), generates a random token, regenerates `docker-compose.yml` (git-ignored, do not hand-edit), and writes `deploy/users/alice.txt` with the URL and token. Add an `alice.yourdomain.com { reverse_proxy 127.0.0.1:8001 }` block to the Caddyfile and `sudo systemctl reload caddy`. Send alice her `.txt` over a private channel; she pastes URL and token into the app. Re-running for an existing name keeps port and token (`--rotate-token` to change it).

ElevenLabs credits (`--eleven-total`, default 9000/month) are split evenly across everyone and re-split whenever you add someone, so run `docker compose up -d` after each add.

## 3. HTTPS is required
Phones (iOS/Android release builds) block plain `http://` and push needs a secure origin. Caddy gives automatic Let's Encrypt certificates. Alternatives that avoid opening ports: Tailscale (everyone installs it; use the tailnet address) or a Cloudflare Tunnel pointing each hostname at `127.0.0.1:<port>`. Containers publish on `127.0.0.1` only, so nothing is reachable except through the proxy.

## 4. Update
```
git pull && docker compose up -d --build
```
Volumes persist. Check `docker compose ps` (health) and `docker compose logs --tail 50 fitness-alice`.

## 5. Backups and restore
`deploy/backup.sh /var/backups/fitness` writes a consistent copy of every person's `fitness.db` (SQLite online-backup API, safe on a live DB) and prunes after 14 days. Cron: `15 3 * * * /path/to/repo/deploy/backup.sh /var/backups/fitness`. Copy backups off the server too.

Restore: `docker compose stop fitness-alice`, then
```
docker run --rm -v data-alice-volume:/data -v /var/backups/fitness:/in python:3.11-slim cp /in/<file>.db /data/fitness.db
docker compose start fitness-alice
```
(the real volume name is `<project>_data-alice`; see `docker volume ls`). Delete stale `fitness.db-wal`/`-shm` in the volume if present.

## 6. Quota warning
Every instance uses the same Gemini and ElevenLabs keys, so 5 people share one rate limit and one credit pool. The ElevenLabs split is enforced per instance; Gemini free-tier limits are not, so heavy use by one person can slow the others. Give people their own keys later by adding `GEMINI_API_KEY` to their service environment.

## 7. Security notes
- `API_TOKEN` is enforced on every endpoint except `/health`. Treat the token as a password; `deploy/users/` and `docker-compose.yml` hold them and are git-ignored.
- `deploy/shared.env` holds provider keys: never commit, never paste in chats.
- Do not expose container ports publicly (the generated compose binds 127.0.0.1).
- Always exactly ONE uvicorn worker per container (scheduler and SQLite assume it).
- CORS is `*`; the token, not CORS, is the protection.

## 8. Not verified
- Docker was not available on the machine this was written on (see end of file for the exact status), so the image build, container start, healthcheck and the compose file were **not run**; they were only written and syntax-checked (`new_user.py` executed, `backup.sh` passed `bash -n`, the SQLite backup call tested locally).
- Not tested on a real host: Caddy certificates, DNS, the backup/restore commands inside Docker, `docker compose up` with several services.
- **The app sends the access key.** On first launch it shows "Connect to your coach" (server address + access key, stored in the phone's secure storage; see Setup > Server later). Install the build made after the `expo-secure-store` change (see docs/BUILD.md). The key and address come from `deploy/users/<name>.txt`.
- The `ELEVEN_MONTHLY_CREDITS` split is a convention, not measured against real ElevenLabs billing.
