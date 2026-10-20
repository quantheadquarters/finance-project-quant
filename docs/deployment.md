# Running Alpha Engine

The supported public install is a **single operator** running the research app locally.
The project does not isolate one visitor's strategies, files, or data from another's.
Keep the dashboard on loopback unless you operate an authenticated private network.

## Local setup

```bash
./start.sh doctor
./start.sh scan AAPL --days 365 --no-record
./start.sh ingest AAPL
./start.sh dashboard
```

Open <http://127.0.0.1:8000/research>. The first two research commands fill the
price and context caches. `--no-record` keeps setup checks out of the signal
track record. Optional Finnhub, FRED and SEC details go in an untracked `.env`
file (see `.env.example`); the default path works without keys. Never publish
`.env` or the signal log as a secret store.

## Docker

Start Docker Desktop first, then check `docker info`. Compose binds the web port
to `127.0.0.1` on your machine and mounts `data/` so caches persist:

```bash
docker compose config --quiet
docker compose run --rm engine scan AAPL --days 365 --no-record
docker compose run --rm engine ingest AAPL
docker compose --profile dashboard up --build dashboard
```

If another app uses host port 8000, change only the left side of the
`127.0.0.1:8000:8000` mapping in `docker-compose.yml`, then open that new port.

The server binds `0.0.0.0` **inside the container** so Docker can reach it;
the host mapping remains loopback only. The compose service uses
`--allow-public` for that container binding and leaves write operations off.
Keep that host mapping on loopback. `ALPHA_API_KEY` protects API calls, but the
browser pages have no login flow, so switching on the key alone breaks their
API requests. A publicly hosted, multiuser app needs a separate authenticated
frontend and isolation for each person's data and strategies.

## Daily scans and checks

The repository's scheduled job is `.github/workflows/daily-signals.yml`. It
runs ingestion, batch scans, source checks and signal-log integrity checks. Do
not add a second scheduler to write the same append-only log. For a private
installation, `scripts/daily.sh` provides locking, timeout, rotation and a
health gate; schedule that script if you are not using the GitHub workflow.

Before sharing a build, run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
./start.sh health --strict
```

`health --strict` reports missing or degraded optional sources as a failure.
That is useful evidence of what the app can show today, not a claim of alpha.
The CI workflow also checks that the built Python wheel includes the frontend.

Research and education only. Not financial advice. Example signals have no
proven edge, and backtests cannot predict future returns.
