# Server Monitor (Parallel SSH)

Standalone script that reads a credentials file and collects system metrics from many servers over SSH in parallel.

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

| Flag | Default | Meaning |
|---|---|---|
| `input_file` (positional) | — | Path to the credentials file |
| `--max-workers N` | `10` | Max concurrent SSH sessions |
| `--timeout N` | `15` | Per-command timeout in seconds |
| `--output PATH` | `report_<ts>.json` | JSON report path |
| `--quiet` | off | Suppress the terminal table |
| `--log PATH` | `server_monitor.log` | Log file path |

## Collected metrics

OS (name/version/kernel), CPU (model, cores, usage), RAM (total/used/free MB),
Disk (per-mount total/used/free GB), server type (VM / container / bare metal),
access level (root / user), public IP, bandwidth (interface + speed),
listening ports, top processes by CPU/RAM.

## Output

- Terminal: one row per server in a table.
- File: full JSON report, including per-server `partial_errors` and `error` fields.
- Log: `server_monitor.log` with per-connection and per-command failures.

A server that fails to connect is reported as failed and the run continues.
A single metric that fails does not abort the rest of that server's checks.

## License

MIT
