# Visor ATC

© 2026 Gonzalo Alonso. All rights reserved. Owner, creator and developer: **Gonzalo Alonso**.
Proprietary software, not open source. See [LICENSE](LICENSE).

A 3D air traffic control working position for European airspace. It is part game, part proof of concept that a decision-making AI can control airspace.

Real traffic recorded from the [OpenSky Network](https://openskynetwork.github.io/opensky-api/rest.html) is replayed as a living scenario. You, or an AI agent, take a sector and issue clearances. Each aircraft follows its recorded trajectory until it is cleared otherwise, then flies the clearance with a simple performance model.

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/fetch_navdata.py      # airports + navaids (OurAirports, public domain)
VISOR_ADMIN_PASSWORD='choose-a-password' .venv/bin/python server.py   # http://localhost:8000 (API docs: /docs)
```

The server records OpenSky snapshots of Europe (34–72°N, 25°W–45°E) into `data/traffic.db` and keeps a rolling 24 h window. Leave it running to build up history.

| OpenSky access | Snapshot interval | Notes |
|---|---|---|
| anonymous | 15 min (400 credits/day, 4 per call) | coarse tracks; the replay interpolates between snapshots |
| `OPENSKY_CLIENT_ID` + `OPENSKY_CLIENT_SECRET` | 90 s | backfills the last hour on first start |

## Deploy with Docker

```sh
cp .env.example .env            # set VISOR_ADMIN_PASSWORD, optionally OpenSky / Jev credentials
docker compose up -d --build
docker compose logs -f
```

- The image contains the app and the airport and navaid data, which is downloaded at build time.
- Recordings **and user accounts** live in the `visor-data` volume (`/data`), so they survive rebuilds and upgrades. Keep the container running to build the 24 h history.
- Run **one** container. The simulation state is held in memory, so do not scale the service.
- The container listens on `127.0.0.1:8000` of the host. Publish it through a reverse proxy with TLS on its own (sub)domain; the UI uses absolute `/api` and `/ws` paths, so a sub-path such as `/visor/` won't work.

To run a published release instead of building on the server, set `VISOR_IMAGE=ghcr.io/<owner>/<repo>:<version>` in `.env`, then run `docker compose pull && docker compose up -d`.

### Accounts

Every page and API call requires a sign-in. The exceptions are the login page and `GET /api/health`, which is used by the Docker healthcheck.

| Variable | Default | |
|---|---|---|
| `VISOR_ADMIN_USER` | `admin` | first admin account, created **only when there are no users yet** |
| `VISOR_ADMIN_PASSWORD` | *(empty)* | empty → a random password is printed in `docker compose logs` and must be changed at first sign-in |
| `VISOR_SESSION_HOURS` | `168` | session lifetime |
| `VISOR_COOKIE_SECURE` | `auto` | `auto` marks the cookie Secure when the proxy sends `X-Forwarded-Proto: https`; or set `true` / `false` |

After the first start the database is the source of truth: changing the variables does not modify existing accounts.

- **Managing users:** admins use **Manage users** in the account menu (`/admin`) to add, edit and remove users. Available changes are role (*admin* or *controller*), password reset (optionally forcing a change at next sign-in), and disable/enable. Disabling a user, deleting them or resetting their password signs them out everywhere.
- **Safeguards:** you can't delete, demote or disable yourself, and the last active admin can't be removed.
- **Password hashing:** PBKDF2-SHA256 with 600,000 iterations.
- **Throttling:** sign-in is throttled after 5 failures per user (20 per client address) within 5 minutes.
- **Attribution:** clearances are recorded per user; the radio log shows `ATC·alice`.

Recovery, e.g. a forgotten admin password:

```sh
docker compose exec visor python -m atc.auth list
docker compose exec visor python -m atc.auth set-password admin              # prompts
docker compose exec visor python -m atc.auth set-password ops --role admin --create
```

**AI agents** sign in with a regular account. `POST /api/auth/login` returns a `token` to send as `Authorization: Bearer <token>`. See [examples/agent_client.py](examples/agent_client.py). Agents may label their clearances `issuer: "ai:<name>"`; human clearances are always attributed to the signed-in user.

### Reverse proxy

Running behind an existing Caddy container managed by Portainer? Add `ghcr.io` under Portainer *Registries* (GitHub username + token with `read:packages`), then use [deploy/portainer-stack.yml](deploy/portainer-stack.yml) and [deploy/Caddyfile.example](deploy/Caddyfile.example) are ready-made for that. Set `VISOR_TRUST_PROXY=1` whenever the app sits behind exactly one reverse proxy, so login throttling uses the real client address.

Example nginx site (TLS via certbot or similar):

```nginx
server {
    server_name atc.example.com;
    listen 443 ssl;
    # ssl_certificate ...; ssl_certificate_key ...;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;         # WebSocket (/ws)
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;     # Secure session cookie
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 1h;
    }
}
```

With Caddy, `atc.example.com { reverse_proxy 127.0.0.1:8000 }` is enough: it handles TLS and WebSockets and sends the forwarding headers.

Always serve it over HTTPS on a public server; otherwise passwords and session cookies travel in clear text.

## Versioning, CI and releases

`VERSION` holds `MAJOR.MINOR`. [.github/workflows/release.yml](.github/workflows/release.yml) runs on every push and pull request:

1. **Version**: the next patch number after the highest existing `vMAJOR.MINOR.*` tag. For example, with `VERSION` = `1.0` and tags up to `v1.0.4`, the next version is `1.0.5`. Pull requests get `1.0.5-dev.<sha>`.
2. **Test**: pytest (flight model, parser, conflict detection, engine plus rule agent, API), byte-compilation, a syntax check of the frontend modules, and shellcheck.
3. **Build and verify**: builds the image with the version baked in, starts it, and runs [scripts/smoke_test.sh](scripts/smoke_test.sh). The smoke test checks health, the reported version and label, UI, docs, navdata, the API contract, and that the container runs as non-root.
4. **Publish** (default branch only, after all checks pass):
   - pushes `ghcr.io/<owner>/<repo>:X.Y.Z`, `:X.Y` and `:latest` for amd64 and arm64
   - creates the annotated tag `vX.Y.Z` on the commit
   - creates a GitHub Release `vX.Y.Z` with generated notes

The running app reports its version at `/api/status`, in `/docs`, and in the UI status bar. To start a new minor or major line, edit `VERSION` (e.g. `1.1`); the patch number restarts at 0.

Local checks:

```sh
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q tests
docker build --build-arg VISOR_VERSION=1.0.0-local -t visor-atc:test . && scripts/smoke_test.sh visor-atc:test 1.0.0-local
```

## Playing

- **Scenario:** *Live* runs just behind the newest snapshot. *Replay* starts anywhere in the recorded window and can run at 1–16×.
- **Sector:** take responsibility for a sector volume. Its traffic is labelled, checks in on the radio and counts for your score.
- **Clearances:** use the flight-strip panel or the command line (<kbd>/</kbd>):

  | | |
  |---|---|
  | `DLH4AB C 370` / `D 240` / `FL 350` | climb / descend / either |
  | `TL 270` / `TR 090` / `H 180` | turn left or right to a heading / fly a heading |
  | `TL 30D` / `TR 20D` | turn by N degrees |
  | `DCT KPT` | proceed direct to a navaid or airport, then rejoin the route |
  | `S 280` | speed (IAS) |
  | `RON` | resume own navigation |

  Several clearances can go in one line: `EZY12 D 300 TR 20D S 280`. If an aircraft is selected, you can omit the callsign.
- **Aircraft behaviour:**
  - Pilots respond after 3–8 s.
  - Turn rate is limited by bank angle.
  - Climb and descent rates depend on class and altitude.
  - Impossible clearances get "unable".
  - An aircraft held at a level away from its planned profile, or kept on a heading too long, will ask for a change.
- **Monitoring:** STCA predicts 2 minutes ahead. Separation minima are 5 NM / 1000 ft, or 3 NM when both aircraft are below FL100. Traffic below 4000 ft is ignored.
- **Score:**
  - +2 per aircraft handled
  - +10 per pilot request granted
  - −2 per STCA alert
  - −10 per ignored request
  - −50 per loss of separation, but only if you were warned (at least 45 s of STCA) or you controlled one of the aircraft

Display settings (⚙) cover vertical exaggeration, speed vectors, trails, drop lines and labels. The map style can be Radar (dark) or Satellite.

## AI integration

The simulator is the environment, and humans and agents use the same API.

**Decision points.** When a situation needs a decision (a conflict or a pilot request), the engine publishes a decision point shaped for decision models such as Jev, which take state plus typed questions and return typed answers:

```json
{
  "id": "D12", "kind": "conflict",
  "state": { "conflict": {...}, "aircraft": [...], "neighbours": [...], "separation_minima": {...} },
  "questions": [
    { "id": "action", "type": "choice", "prompt": "...",
      "options": [ { "id": "o3", "label": "DLH4AB climb FL360",
                     "clearances": [ { "aircraft": "DLH4AB", "kind": "CLIMB", "value": 360 } ],
                     "predicted": { "los_duration_s": 0, "min_h_nm": 6.1, "first_los_s": null, ... },
                     "cost": 1.0 } ] },
    { "id": "urgency", "type": "score", "scale": ["low", "medium", "high", "critical"] },
    { "id": "will_lose_separation", "type": "probability", "statement": "..." }
  ]
}
```

Every option is scored by fast-time prediction: the aircraft and their neighbours are cloned and flown 4 minutes ahead with that clearance applied. The agent chooses from explicit consequences. Answering `{"action": "o3"}` executes the option.

**AI modes** (top bar):
- **Advisory:** the agent's choice is highlighted and you click *Accept*.
- **Autonomous:** the agent's choice is executed directly.

The built-in agents are `rules` (the reference baseline) and `jev`.

**Jev.** Set `JEV_ENDPOINT` (plus `JEV_API_KEY` and `JEV_MODEL` if needed) and select the `jev` agent. The request and response mapping lives in two functions in [atc/agents/jev.py](atc/agents/jev.py): `build_request` and `parse_response`. Adapt them to the provider's actual wire format.

**External agents** can use the HTTP API directly. See [examples/agent_client.py](examples/agent_client.py).

| Endpoint | Purpose |
|---|---|
| `GET /api/observation` | full typed state: aircraft, conflicts, open decisions, score |
| `GET /api/decisions`, `POST /api/decisions/{id}` | list and answer decision points |
| `POST /api/clearance`, `POST /api/command` | structured or shorthand clearances (`issuer: "ai:<name>"`) |
| `POST /api/sim` | pause/resume/speed/reset; `lockstep` + `step` so the agent owns the clock |
| `GET /api/events?since=` | radio and alert log |
| `GET /api/schema` | action space description |
| `WS /ws` | 2 Hz state frames (used by the UI) |

## Code map

- `atc/recorder.py`, `atc/store.py`, `atc/opensky.py`: polling, SQLite rolling history, OAuth2 client
- `atc/scenario.py`: streams recorded snapshots into flight plans
- `atc/aircraft.py`, `atc/performance.py`: flight model, autopilot modes, pilot delay, performance classes
- `atc/clearances.py`: clearance model, shorthand parser, readback phrasing
- `atc/conflicts.py`: STCA and loss-of-separation detection
- `atc/decisions.py`: decision points and fast-time prediction
- `atc/engine.py`: simulation loop, traffic life-cycle, requests, scoring, events
- `atc/api.py`: FastAPI REST and WebSocket
- `public/js/`: three.js client
  - `tiles.js`: level-of-detail terrain from AWS Terrain Tiles plus Esri imagery
  - `traffic.js`: instanced aircraft, drop lines, vectors, trails
  - `overlay.js`: ATC symbology and data blocks
  - `ui.js`: panels, strip, radio, command line

## About, build information and copyright

The account menu has an **About Visor ATC** item; clicking the version in the status bar opens it too. It shows:

- **Build:** version, release or development build, git commit (linked to the source), and build date.
- **Runtime:** Python, platform, server libraries, the client's three.js revision, and server start time.
- **Ownership:** the copyright notice and the credits for data and third-party software.

The same information is available as JSON at `GET /api/about` (signed in).

- **Release images:** the workflow bakes in the version, commit, build date and repository URL. They also appear as OCI labels: `org.opencontainers.image.{version,revision,created,source,authors,vendor}`.
- **Local runs:** the version reads `dev`, and the commit is taken from git; uncommitted changes are flagged.

Visor ATC is © 2026 Gonzalo Alonso, who is its owner, creator and developer. All rights reserved. It is proprietary software, not open source: no use, copying, modification or distribution without written permission (see [LICENSE](LICENSE)). The third-party data and libraries listed in the About dialog remain under their own licences and terms.

The container images are published to a **private** GHCR package. Servers pull them with registry credentials: a GitHub token with `read:packages`, set in Portainer under *Registries*, or `docker login ghcr.io`.

## Limitations

- No wind model (ground speed equals true airspeed), no wake-turbulence spacing, and no approach or departure procedures. Aircraft appear when first seen airborne and disappear on landing or when leaving the area.
- Sector boundaries are simplified approximations, not official airspace data.
- With anonymous OpenSky access, recorded tracks are 15 minutes apart, so routes between snapshots are straight lines.
