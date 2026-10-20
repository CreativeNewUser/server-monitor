# Server Monitor (Parallel SSH)

Standalone script that reads a credentials file and collects system metrics from many servers over SSH in parallel. It also detects (and where possible installs) the 3x-ui panel on each host.

## Input format

One server per line:

```
IP:PORT@USERNAME:PASS
IP:PORT@USERNAME:PASS
```

- `PORT` optional — defaults to `22`.
- Lines starting with `#` and blank lines are ignored.

## Install

Nothing to install manually. The script auto-installs `paramiko` on first run
(uses `apt-get` / `apk` / `dnf` / `yum` / `pip`, whichever the host has).

## Usage

On a fresh server, with `servers.txt` already created:

```bash
curl -sSL https://raw.githubusercontent.com/CreativeNewUser/server-monitor/main/server_monitor.py -o server_monitor.py
python3 server_monitor.py servers.txt --max-workers 10 --timeout 15 --output report.json
```

Or in one line:

```bash
curl -sSL https://raw.githubusercontent.com/CreativeNewUser/server-monitor/main/server_monitor.py | python3 - servers.txt --max-workers 10 --timeout 15 --output report.json
```

## Options

- `input_file` (positional) — path to the credentials file
- `--max-workers N` — max concurrent SSH sessions (default `10`)
- `--timeout N` — per-command timeout in seconds (default `15`)
- `--install-timeout N` — timeout for the 3x-ui installer in seconds (default `600`)
- `--output PATH` — JSON report path (default `report_<ts>.json`)
- `--text-output PATH` — plain-text report path (default `report_<ts>.txt`)
- `--log PATH` — log file path (default `server_monitor.log`)

## Collected metrics

OS (name/version/kernel), CPU (model, cores, usage), RAM (total/used/free MB),
Disk (root filesystem total/used/free GB), server type (VM / container / bare metal),
access level (root / user), public IP, bandwidth (interface + speed),
listening ports, top processes by CPU/RAM.

Notes on how a few of these are measured:

- **CPU usage** is a `/proc/stat` delta sampled 1 second apart, not `top -bn1`
  (which reports the since-boot average and reads ~0% on an idle host).
- **Bandwidth** comes from `/sys/class/net/*/speed`; `ip link` carries no speed.
  Interfaces that report `-1` (virtual, bridge, tunnel) show as `NA`.
- **Public IP** only accepts a bare address; if the lookup service returns an
  error page the field is `unknown` rather than the HTML body.
- **Disk** uses GNU `df --output` with a POSIX `df -P -h` fallback
  (busybox/Alpine have no `--output`). Only the root filesystem is reported.
- **A probe fails only when the remote command exits non-zero** or its output
  cannot be parsed. A warning on stderr alone is not a failure — many minimal
  hosts print one while still returning a valid result.

## 3x-ui panel

For every host the script:

1. checks whether a 3x-ui panel is already installed
   (`/usr/local/x-ui/x-ui`, the `x-ui` command, or `/etc/x-ui/x-ui.db`);
2. if not, installs it with the upstream installer and all options at their
   defaults (`XUI_NONINTERACTIVE=1`);
3. reads back the panel port, web base path, username and password.

The installer needs root. A root session runs it directly; a non-root user is
elevated with `sudo -S` and the same password as the SSH user. The password is
written to the remote stdin, never into the command line.

Panel state per server is one of:

- `installed` — installed by this run
- `preexisting` — already present, left untouched
- `not_attempted` — could not be installed because root access was unavailable
- `failed` — the installer ran but did not succeed

The report lists the servers with a working panel in their own section, with the
panel URL, username and password.

## Output

- Files: a JSON report and a plain-text report. Both contain the 3x-ui results,
  per-server `partial_errors` and `error` fields. Both are written with mode
  `600` because they contain panel credentials.
- Log: `server_monitor.log` with per-connection and per-command failures.

There is no terminal table. A server that fails to connect is reported as
failed and the run continues. A single metric that fails does not abort the
rest of that server's checks; those failures land in `partial_errors`.

## License

MIT
