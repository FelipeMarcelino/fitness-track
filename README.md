# FitTrack

A Telegram and WhatsApp bot that turns natural language — typed or spoken — into structured
training data, and uses that history for progress analysis and workout recommendations.

```
"Supino reto com 10 kg, 8 repetições e foi fácil"
   → exercise=supino_reto_barra  load=10.0kg  reps=8  rpe=4  session=#182  set_index=1
```

**[`doc/spec.md`](doc/spec.md) is the source of truth** for architecture and product. This file
covers only how to run the thing.

## What exists today

Phase 1.0's foundation, plus sprint 2's Telegram pipeline end to end: a verified webhook (or, in
dev, `getUpdates` polling) lands an update, deduplicates it, persists it encrypted, buffers it
through the debounce window, and drains it into a `processing_batch`. `make bootstrap` reconciles
`TELEGRAM_MODE` against Telegram's own webhook registration, so the two never fight over the same
update stream. There is no LLM gateway and no graph yet — those are the sprints that follow, and a
batch marked `done` today has no reply, only a persisted record.

## Setting up

You need [Nix](https://nixos.org/download) with flakes, [direnv](https://direnv.net), and Docker.

```bash
direnv allow .    # enters the devshell from flake.nix; creates ./.venv on first use
uv sync           # installs dependencies into that venv
make up           # generates certificates and .env, then starts the stack and waits for health
make bootstrap    # migrates the database and sets up the LangGraph tables
```

`make up` does two things that are easy to get wrong by hand, so it does them for you:

- **`make certs`** generates a development CA and one certificate per data store. All three speak
  TLS, and the application verifies with `sslmode=verify-full` — a wrong certificate or a plaintext
  connection *fails*, which `tests/integration/test_transit_encryption.py` proves in both
  directions.
- **`make env`** writes `.env` from `.env.example`, generating the local credentials and rebuilding
  the connection URLs that depend on them. Copying the template by hand leaves `change-me` inside
  `LANGFUSE_ENCRYPTION_KEY`, which Langfuse rejects for not being 64 hex characters.

Without direnv, `nix develop` is the manual equivalent.

## Everyday commands

`make` is the single entry point, and CI calls the same targets — so "passed locally" and "passed in
CI" mean the same thing.

| Command | What it does |
| --- | --- |
| `make fmt` | Format and apply the safe lint fixes |
| `make lint` | Ruff: format check plus lint rules |
| `make typecheck` | mypy, strict |
| `make test` | Unit and architecture tests. No containers needed |
| `make test-architecture` | The guardrails alone — the first thing CI runs |
| `make test-in-worker` | The whole suite inside the worker, against the real services |
| `make check` | `lint` + `typecheck` + `test`, in CI order |
| `make eval-judge` | The LLM-as-judge round of spec §21.2 |

## The stack

| | |
| --- | --- |
| `make up` | Start everything and wait for health |
| `make ps` / `make logs` | State and logs |
| `make down` | Stop, keeping the volumes |
| `make reset` | Destroy the volumes and rebuild from scratch |

**`docker-compose.yml` alone is the production topology** — Postgres, Redis, Qdrant and Langfuse
publish no ports; only Caddy does (spec §3.1). `docker-compose.dev.yml` is the only file that opens
anything to the host, which is why every command above passes both.

**Telegram runs one of two ways, and the base file only allows one of them.** `TELEGRAM_MODE`
defaults to `webhook`, and `docker-compose.yml` hardcodes it regardless of `.env` — `setWebhook`,
behind Caddy, across as many `ingress` replicas as you like. `polling` is explicitly a development
choice: `docker-compose.dev.yml` is the only file that lets the operator's `.env` choose it, the dev
override runs a single `ingress` replica on purpose (two pollers on one bot collide, spec §18.2),
and `make bootstrap` calls `deleteWebhook` before the poller starts so the two mechanisms are never
both registered at once.

## Database

```bash
make migrate                       # to head, as the schema owner
make revision M="what it does"     # a new revision
```

Two principals, and the separation is load-bearing (spec §19.1). The application connects as
`fittrack_runtime` — `NOSUPERUSER NOBYPASSRLS`, owning nothing — because a superuser, or any role
with `BYPASSRLS`, ignores row level security *even with `FORCE`*. Pointing `DATABASE_URL` at the
owner would leave every policy in place and never evaluated, which is a silent failure rather than
an error, so settings refuse it at boot.

`make migrate-down` needs an explicit `DSN=`: the only revision's `downgrade` drops every table of
§5.2, and it must never be pointed at a database anyone cares about.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `certs/ exists and is not writable` | `docker compose up` ran before `make certs` and Docker created the bind-mount source as root. The message gives the removal command |
| Services healthy, application cannot connect | `.env` was regenerated against a live volume. Postgres reads `POSTGRES_PASSWORD` only at initdb, so the server keeps the old one. `make reset` |
| `make env` refuses | The existing `.env` still holds `change-me`, is missing a newer variable, or its URLs disagree with the passwords beside it. It names which |
| `role fittrack_runtime does not exist` | The volume predates the roles. `make reset`, or create the two roles by hand — the migration's error message gives both statements |
| `permission denied for database` | The Langfuse role is confined to its own database on purpose (spec §22.2) |
| A query returns nothing that should return something | The transaction has no `app.tenant_id`. RLS fails closed by design; open it with `tenant_transaction()` |

## Contributing

Read [`CLAUDE.md`](CLAUDE.md) first — it carries the invariants that are expensive to violate and
easy to violate by accident. In short: TDD, a branch per change, a PR reviewed before merge, and
never a silent divergence from the spec.
