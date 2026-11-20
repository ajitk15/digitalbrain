# Setup

## 1. First-time install

| Step | Do this |
|---|---|
| 1 | *(Optional)* Install and start **Docker Desktop** - see section 3 |
| 2 | Open the project folder and **double-click `start-all.cmd`** (or run it in a terminal) |
| 3 | Sign-in ID prompt: press **Enter** for `siteadmin@db.com`, or type your own |
| 4 | Password prompt: type a strong password twice (hidden, never saved to a file) |
| 5 | AI provider prompt: pick **Claude** or **OpenAI** and paste the key, or press Enter to skip |
| 6 | Open **http://127.0.0.1:8000** and sign in |

Use `start-all.cmd`, not `start-all.ps1` - Windows blocks `.ps1` files by default.
Nothing needs to be installed first: setup installs `uv` and builds `.venv` itself.

## 2. All options

| Command | Use it when |
|---|---|
| `start-all.cmd` | First run, and every normal start |
| `start-all.cmd -Install` | Setup must run again (e.g. no administrator was created) |
| `start-all.cmd -Install -Demo` | First run **with** the CarePath demo data |
| `start-all.cmd -Demo` | Add or refresh the demo on an existing setup (safe to repeat) |
| `start-all.cmd -ConfigureAI` | Change or add the AI provider key only |
| `start-all.cmd -NoDocker` | Run on SQLite even though Docker is running |
| `start-all.cmd -Port 8123` | Port 8000 is already in use |
| `stop-all.cmd` | Stop the server (data is kept) |

Options can be combined: `start-all.cmd -Install -Demo -Port 8123`.

The demo creates `siteadmin@db.com` and `acmeadmin`, both with the development password
`demo123456789`, and asks for the Jira and ServiceNow credentials (Enter skips them).

## 3. Local Docker installation

Docker is optional. With it you get PostgreSQL, Redis and a virus scanner, the same as
production. Without it the platform uses SQLite.

| Step | Do this |
|---|---|
| 1 | Install **Docker Desktop** for Windows from docker.com and start it |
| 2 | Wait until Docker Desktop says *Engine running* |
| 3 | Run `start-all.cmd` - it starts the containers itself |
| 4 | Check them: `docker compose -f docker-compose.dev.yaml -p digitalbrain-dev ps` |

| Container | Port | Purpose |
|---|---|---|
| `postgres` | 127.0.0.1:5442 | Database |
| `redis` | 127.0.0.1:6390 | Streaming answers (TLS) |
| `clamd` | 127.0.0.1:3310 | Virus scanning (the first start downloads signatures for several minutes) |

| Task | Command |
|---|---|
| Stop the containers | `docker compose -f docker-compose.dev.yaml -p digitalbrain-dev stop` |
| Start them without the server | `docker compose -f docker-compose.dev.yaml -p digitalbrain-dev up -d postgres redis clamd` |
| Uploads refused as "stale" on the first start | `docker compose -f docker-compose.dev.yaml -p digitalbrain-dev restart clamd` |

If you already used the project without Docker, the first Docker start copies your
SQLite data into PostgreSQL once and keeps the SQLite file as a backup.

## 4. Forgot the siteadmin password

Run these in PowerShell from the project folder. Use your own sign-in ID if it is not
`siteadmin@db.com`.

| Your setup | Command |
|---|---|
| With Docker (containers running - start them with `start-all.cmd` or section 3) | `.venv\Scripts\python.exe manage.py changepassword siteadmin@db.com` |
| Without Docker | `$env:DIGITAL_BRAIN_CONFIG="config/local.nodocker.toml"; .venv\Scripts\python.exe manage.py changepassword siteadmin@db.com` |
| Never used Docker (no `config/local.nodocker.toml`) | `.venv\Scripts\python.exe manage.py changepassword siteadmin@db.com` |
| Hosted VPS | `docker compose exec app python manage.py changepassword siteadmin@db.com` |

You are asked for the new password twice (hidden). Then, if needed:

| Problem | Command (add the same prefix as above) |
|---|---|
| Locked out after too many wrong passwords | `.venv\Scripts\python.exe manage.py axes_reset_username siteadmin@db.com` |
| Lost the authenticator app | `.venv\Scripts\python.exe manage.py remove_authenticator siteadmin@db.com` |

Other users' passwords: sign in as siteadmin, **Administration -> People**, issue a reset link.

## 5. If something goes wrong

| Message | Fix |
|---|---|
| Port 8000 is already occupied | `start-all.cmd -Port 8123` |
| Digital Brain is already running | Open the printed URL, or `stop-all.cmd` first |
| No administrator account exists yet | `stop-all.cmd`, then `start-all.cmd -Install` |
| AI provider setup did not complete | `start-all.cmd -ConfigureAI` |
| Docker is not running, so starting without it | Start Docker Desktop, then `stop-all.cmd` and `start-all.cmd` |
| Startup failed | Read `.runtime\server-error.log` |

More detail: [`README.md`](README.md) (local) and [`deploy/README.md`](deploy/README.md) (VPS).
