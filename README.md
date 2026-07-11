# Vaio

This repository contains the infrastructure and web assets for saloraxz.com.

## What’s here

- [Caddyfile](Caddyfile) for routing and reverse proxy rules
- [www/](www) for the deployed site content
- [www/csrs/](www/csrs) for the CSRS application and its backend/frontend code

## CSRS

CSRS has its own troubleshooting notes here:

- [www/csrs/README.md](www/csrs/README.md)

That guide covers browser debugging, backend/API checks, container issues, and the fastest local validation commands.

## Local checks

These are the quickest sanity checks when something changes:

```bash
python -m py_compile www/csrs/csrs_api/main.py
curl -sS http://localhost:8000/health
curl -sS http://localhost:8000/api/meta
```

## Deployment notes

- `www/csrs/csrs_api/main.py` is the FastAPI backend for CSRS.
- `www/csrs/csrs_api/frontend/index.html` is the CSRS browser UI.
- `Caddyfile` controls the public routes for the domain.

If the site is broken in production but works locally, check routing first, then API responses, then the mounted data file.