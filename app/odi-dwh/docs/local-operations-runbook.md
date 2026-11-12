# Local Operations Runbook

This runbook covers local development and operator validation for DWH Studio in
a local installation. It does not install services, create scheduled tasks, change
firewall rules, run ODI scenarios, or execute DB writes.

## Start Order

1. Confirm Oracle database and listener are already available through the local
   operator process.
2. Start WebLogic/ODI only when ODI agent-backed actions are needed:
   - Admin console: `http://<odi-host>:7001/console`.
   - Example ODI managed server command (replace `<oracle-home>`):
     `<oracle-home>\user_projects\domains\base_domain\bin\startManagedWebLogic.cmd ODI_server1 http://localhost:7001`
3. Start the Flask backend from the app root:
   - `start_app.bat`, or
   - `.venv\Scripts\python.exe -X utf8 local_app_control.py start --open-browser`
   The launcher stops existing listeners on the configured app port, starts one
   detached local Flask process, and writes logs under `logs\`.
4. Frontend production assets are served by Flask from `static\react` after a
   Vite build. Use the bundled Node/Vite build path when assets must be
   refreshed.

## Stop Order

1. Stop local Flask backend:
   - `stop_app.bat`, or
   - `.venv\Scripts\python.exe -X utf8 local_app_control.py stop`
2. Confirm port 5000 has no duplicate listeners:
   - `.venv\Scripts\python.exe -X utf8 local_app_control.py status`
3. Stop WebLogic/ODI server only when that server was started for ODI work.

## Local App Logs

The local launcher writes process output to:

- `logs\local_app_stdout.log`
- `logs\local_app_stderr.log`

These files are local troubleshooting artifacts. Do not paste secrets,
connection strings, raw rows, or credential material from logs into Linear or
chat.

## Smoke Check

Run the non-destructive smoke script:

```powershell
.\.venv\Scripts\python.exe operator_smoke.py
```

The script reports:

- port 5000 listener count and PIDs
- HTTP status for backend status
- HTTP status for deployment security profile
- HTTP status for ODI mapping metadata
- HTTP status for mapping run history

It does not print response bodies, environment values, connection strings, raw
rows, or sensitive values.

Expected local result:

- `status`: `pass`
- `port.listener_count`: `1`
- all checks have `ok: true`

`status: warn` means HTTP checks passed but the port listener count is not
exactly one. Treat this as a wrong-process or duplicate-process warning before
trusting browser smoke.

## Recovery

### Port 5000 occupied

1. Run `.venv\Scripts\python.exe -X utf8 local_app_control.py status`.
2. Stop the app with `stop_app.bat`.
3. Start the app with `start_app.bat`; startup clears existing listeners on the
   configured app port before launching one detached Flask process.
4. Run `operator_smoke.py`.

### Wrong-process false green

If `/api/status` returns 200 but behavior looks stale:

1. Check port owner with `netstat -aon | findstr " :5000 "`.
2. Confirm only one listener exists.
3. Restart from `app/odi-dwh` in the repository.
4. Run `operator_smoke.py` before browser QA.

If Windows shows the listener child executable as system Python, the
`.venv\Scripts\python.exe` launcher may have delegated to a Python child
process. The stronger evidence is single listener plus restart from the
app root plus successful smoke.

### PowerShell Start-Process failure

A duplicate `Path`/`PATH` environment-key issue has been observed with
PowerShell `Start-Process`. Use the batch launcher or `local_app_control.py`
instead of relying on `Start-Process`.

### ODI agent unreachable

1. Do not run a scenario or load plan just to test the agent.
2. Check WebLogic/ODI managed server status first.
3. Use read-only status endpoints and metadata screens before any approved ODI
   action.

### Stale frontend assets

1. Rebuild React assets with the bundled Node/Vite path.
2. Hard-refresh the browser.
3. Run `operator_smoke.py` for backend/API status; then do browser QA.

## Approval Boundary

These actions remain approval-gated:

- Task Scheduler or Windows service creation.
- WebLogic service changes.
- Docker persistent changes.
- Firewall, certificate, reverse proxy, or LAN exposure changes.
- ODI scenario/load-plan run.
- DB DDL/DML/truncate/load.
- Credential rotation or Vault service changes.
