# CSRS Troubleshooting Guide

This guide is for debugging and fixing CSRS issues quickly. The app has three layers:

- Routing and reverse proxy: [Caddyfile](Caddyfile)
- Backend API and data loading: [www/csrs/csrs_api/main.py](www/csrs/csrs_api/main.py)
- Frontend rendering and fetch calls: [www/csrs/csrs_api/frontend/index.html](www/csrs/csrs_api/frontend/index.html)

## Flow

1. Reproduce the problem in the browser.
2. Open DevTools and check the Console for JavaScript errors.
3. Check the Network tab for failed requests such as `/api/meta`, `/api/home`, `/api/rankings`, or `/version.txt`.
4. Call the same endpoint with `curl` to separate frontend and backend problems.
5. If the endpoint works, compare the frontend fetch path and expected JSON keys.
6. If the endpoint fails, inspect the FastAPI route, the data file path, and the container environment.

## CSRS Checks

Use these first when something looks wrong:

```bash
curl -sS https://saloraxz.com/csrs | head
curl -sS https://saloraxz.com/api/meta
curl -sS https://saloraxz.com/api/rankings?limit=1
curl -sS https://saloraxz.com/api/home
curl -sS https://saloraxz.com/version.txt
```

For local debugging:

```bash
python -m py_compile www/csrs/csrs_api/main.py
curl -sS http://localhost:8000/health
curl -sS http://localhost:8000/api/meta
```

## Inspecting

- If the page loads but widgets show errors, inspect [www/csrs/csrs_api/frontend/index.html](www/csrs/csrs_api/frontend/index.html) and the matching backend route in [www/csrs/csrs_api/main.py](www/csrs/csrs_api/main.py).
- If `data.save` is missing or stale, inspect the `CSRS_DATA_FILE` environment variable and the mounted data volume.
- If requests fail at the edge, inspect [Caddyfile](Caddyfile) for path routing and reverse proxy rules.

## Basics

- Frontend bug: browser loads the page, API requests succeed, but the UI renders incorrectly.
- Backend bug: API requests fail, return the wrong shape, or return stale data.
- Deployment bug: local works, production fails.