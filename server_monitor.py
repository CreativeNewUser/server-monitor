#!/usr/bin/env python3
"""
Server Monitor - Parallel SSH
Collects system information from multiple servers via SSH in parallel batches.
"""

import argparse
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

try:
    import paramiko
except ImportError:
    paramiko = None

# --- Self-provisioning: install paramiko + re-exec ---
# Order matters: distro package first (fast, no PEP 668 issues), pip last.

def _run(cmd: List[str], timeout: int = 120) -> bool:
    """Run a command, return True on exit code 0."""
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if res.returncode != 0:
            print(f"    ! {' '.join(cmd[:3])} failed: {(res.stderr or res.stdout).strip()[:300]}", file=sys.stderr)
        return res.returncode == 0
    except Exception as e:
        print(f"    ! {' '.join(cmd[:3])} error: {e}", file=sys.stderr)
        return False


def _install_paramiko() -> bool:
    """Install paramiko using whatever package manager this host has."""
    def _importable() -> bool:
        try:
            importlib.import_module("paramiko")
            return True
        except ImportError:
            return False

    if _importable():
        return True

    strategies: List[List[List[str]]] = []

    if shutil.which("apt-get"):
        strategies.append([
            ["apt-get", "update", "-qq"],
            ["apt-get", "install", "-y", "-qq", "python3-paramiko"],
        ])
    if shutil.which("apk"):
        strategies.append([["apk", "add", "--no-cache", "py3-paramiko"]])
    if shutil.which("dnf"):
        strategies.append([["dnf", "install", "-y", "-q", "python3-paramiko"]])
    if shutil.which("yum"):
        strategies.append([["yum", "install", "-y", "-q", "python3-paramiko"]])

    pip_cmds = []
    for pip in ("pip3", "pip"):
        if shutil.which(pip):
            pip_cmds.append([pip, "install", "paramiko"])
            pip_cmds.append([pip, "install", "--break-system-packages", "paramiko"])
            pip_cmds.append([pip, "install", "--user", "paramiko"])
            break
    for extra in ([], ["--break-system-packages"], ["--user"]):
        pip_cmds.append([sys.executable, "-m", "pip", "install"] + extra + ["paramiko"])
    if shutil.which("uv"):
        pip_cmds.append(["uv", "pip", "install", "--system", "--break-system-packages", "paramiko"])
    strategies.append(pip_cmds)

    for group in strategies:
        for cmd in group:
            if _run(cmd) and _importable():
                return True

    # Last resort: private venv (works without root), then import from it.
    venv_dir = os.path.join(tempfile.gettempdir(), "server_monitor_venv")
    if _run([sys.executable, "-m", "venv", venv_dir], timeout=180):
        vpy = os.path.join(venv_dir, "bin", "python3")
        if not os.path.exists(vpy):
            vpy = os.path.join(venv_dir, "Scripts", "python.exe")
        vpip = os.path.join(venv_dir, "bin", "pip")
        if os.path.exists(vpip):
            if _run([vpip, "install", "paramiko"], timeout=300):
                _venv_site_packages(venv_dir)
                return _importable()
        # ensurepip missing -> bootstrap pip with get-pip.py (stdlib only)
        try:
            import urllib.request
            getpip = os.path.join(venv_dir, "get-pip.py")
            urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", getpip)
            if _run([vpy, getpip], timeout=300):
                if _run([vpy, "-m", "pip", "install", "paramiko"], timeout=300):
                    _venv_site_packages(venv_dir)
                    return _importable()
        except Exception as e:
            print(f"    ! venv bootstrap error: {e}", file=sys.stderr)

    return _importable()


def _venv_site_packages(venv_dir: str) -> None:
    """Make a venv's site-packages importable from the running interpreter."""
    import site
    for lib in ("lib", "lib64"):
        base = os.path.join(venv_dir, lib)
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            sp = os.path.join(base, entry, "site-packages")
            if os.path.isdir(sp) and sp not in sys.path:
                sys.path.append(sp)
                site.addsitedir(sp)


def _bootstrap() -> None:
    """Ensure paramiko is importable; install it if needed."""
    global paramiko
    if paramiko is not None:
        return
    print("[*] 'paramiko' not found -> auto-installing dependencies...", file=sys.stderr)
    if not _install_paramiko():
        print("[-] Auto-install failed. Install manually:\n"
              "      apt install -y python3-paramiko   (Debian/Ubuntu)\n"
              "      apk add py3-paramiko              (Alpine)\n"
              "      pip install paramiko              (pip)", file=sys.stderr)
        sys.exit(1)
    paramiko = importlib.import_module("paramiko")
    print("[+] 'paramiko' ready.", file=sys.stderr)

# --- Data Models ---

@dataclass
class Server:
    """Represents a server from the input file."""
    ip: str
    port: int = 22
    username: str = ""
    password: str = ""
    raw_line: str = ""


@dataclass
class Panel3xUI:
    """3x-ui panel state for one server."""
    state: str = "not_attempted"  # installed | preexisting | failed | not_attempted
    url: Optional[str] = None
    port: Optional[int] = None
    web_base_path: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    reason: Optional[str] = None
    install_log: Optional[str] = None


@dataclass
class ServerInfo:
    """Structured data collected from a server."""
    server: Server
    success: bool = False
    error: Optional[str] = None
    os: Optional[Dict[str, str]] = None
    cpu: Optional[Dict[str, Any]] = None
    ram: Optional[Dict[str, int]] = None
    disk: Optional[List[Dict[str, Any]]] = None
    server_type: Optional[str] = None
    access_level: Optional[str] = None
    public_ip: Optional[str] = None
    bandwidth: Optional[Dict[str, Any]] = None
    ports: Optional[List[int]] = None
    top_processes: Optional[List[Dict[str, Any]]] = None
    partial_errors: List[str] = field(default_factory=list)
    x3ui: Optional[Panel3xUI] = None


@dataclass
class Report:
    """Aggregated results for all servers."""
    timestamp: str
    input_file: str
    servers: List[ServerInfo] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)
    x3ui_summary: Dict[str, int] = field(default_factory=dict)


# --- SSH Commands ---
# Commands defined in contracts/ssh-commands.md

SSH_COMMANDS = {
    "os": "cat /etc/os-release 2>/dev/null || (cat /etc/redhat-release 2>/dev/null && cat /etc/issue 2>/dev/null) || uname -srm",
    "cpu_info": "lscpu 2>/dev/null || cat /proc/cpuinfo | head -20",
    # CPU usage from a /proc/stat delta.
    # `top -bn1` reports the since-boot average (~0% on an idle host) and is
    # locale-dependent, so it cannot be trusted. Two samples 1s apart give a
    # real instantaneous figure. Uses POSIX sh only (no bashisms).
    "cpu_usage": r"""t0=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); sleep 1; t1=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); set -- $t0 $t1; awk -v t0=$1 -v i0=$2 -v t1=$3 -v i1=$4 'BEGIN{dt=t1-t0; di=i1-i0; if(dt>0) printf "%.1f\n", 100-100*di/dt}'""",
    "ram": "free -m 2>/dev/null | awk 'NR==2{printf \"%s,%s,%s\", $2,$3,$4}'",
    # GNU df --output first, POSIX df -P as fallback (busybox/Alpine).
    "disk": "(df -h --output=source,size,used,avail,pcent,target 2>/dev/null || df -P -h 2>/dev/null) | tail -n +2 | tr -s ' '",
    "server_type": "(systemd-detect-virt 2>/dev/null || echo 'unknown') && (dmidecode -s system-product-name 2>/dev/null || echo 'unknown')",
    "access_level": "whoami",
    "public_ip": "curl -s --max-time 5 ifconfig.me 2>/dev/null | head -1; curl -s --max-time 5 icanhazip.com 2>/dev/null | head -1; curl -s --max-time 5 ipinfo.io/ip 2>/dev/null | head -1",
    # Interface speed lives in sysfs, not in `ip link` output. -1 means unknown.
    "bandwidth": "for d in /sys/class/net/*; do n=$(basename $d); s=$(cat $d/speed 2>/dev/null); [ -z \"$s\" ] && s=NA; [ \"$s\" = \"-1\" ] && s=NA; echo \"$n $s\"; done",
    "ports": "ss -tulnH 2>/dev/null | awk '{print $5}' | awk -F: '{print $NF}' | sort -un",
    "top_processes": "ps aux --sort=-%cpu,-%mem | head -6 | tail -5 | awk '{printf \"%s,%s,%s,%s\\n\", $2,$11,$3,$4}'",
}


# --- Parsing ---

def parse_os(output: str) -> Dict[str, str]:
    """Parse OS info from command output."""
    result = {"name": "unknown", "version": "unknown", "kernel": "unknown"}
    for line in output.splitlines():
        if line.startswith("NAME="):
            result["name"] = line.split("=", 1)[1].strip('"').strip("'")
        elif line.startswith("VERSION="):
            result["version"] = line.split("=", 1)[1].strip('"').strip("'")
    if result["name"] == "unknown":
        if "redhat" in output.lower() or "centos" in output.lower():
            for line in output.splitlines():
                if "release" in line.lower():
                    result["name"] = line.split()[0]
                    result["version"] = " ".join(line.split()[1:3])
        else:
            # Fallback to uname
            parts = output.strip().split()
            if len(parts) >= 3:
                result["kernel"] = parts[2]
                result["name"] = parts[0]
    return result


def parse_cpu_info(output: str) -> Dict[str, Any]:
    """Parse CPU info from lscpu output."""
    result = {"model": "unknown", "cores": 0}
    for line in output.splitlines():
        if "Model name:" in line:
            result["model"] = line.split(":", 1)[1].strip()
        elif "CPU(s):" in line and "On-line" not in line:
            try:
                result["cores"] = int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return result


def parse_cpu_usage(output: str) -> float:
    """Parse CPU usage percentage."""
    try:
        return float(output.strip())
    except ValueError:
        return 0.0


def parse_ram(output: str) -> Dict[str, int]:
    """Parse RAM info (total, used, free in MB)."""
    parts = output.strip().split(",")
    if len(parts) >= 3:
        return {
            "total": int(parts[0]),
            "used": int(parts[1]),
            "free": int(parts[2]),
        }
    return {"total": 0, "used": 0, "free": 0}


def parse_disk(output: str) -> List[Dict[str, Any]]:
    """Parse disk info from df output — root filesystem only.

    The fleet report only needs the total and used space of "/"; per-mount
    detail (tmpfs, devtmpfs, efivarfs, ...) is noise.
    """
    disks = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 6:
            try:
                size = float(parts[1].replace("G", "").replace("M", ""))
                used = float(parts[2].replace("G", "").replace("M", ""))
                avail = float(parts[3].replace("G", "").replace("M", ""))
                if parts[1].endswith("M"):
                    size /= 1024
                    used /= 1024
                    avail /= 1024
                disks.append({
                    "mount": parts[5],
                    "total": round(size, 2),
                    "used": round(used, 2),
                    "free": round(avail, 2),
                    "unit": "GB",
                })
            except (ValueError, IndexError):
                continue

    root = next((d for d in disks if d["mount"] == "/"), None)
    if root:
        return [root]
    # No "/" in the output (unusual) — fall back to the largest real filesystem.
    real = [d for d in disks if d["total"] > 0]
    if not real:
        return []
    return [max(real, key=lambda d: d["total"])]


def parse_server_type(output: str) -> str:
    """Parse server type from systemd-detect-virt and dmidecode.

    systemd-detect-virt prints 'none' on bare metal, and a virt name on VMs.
    'unknown' means the probe itself is unavailable.
    """
    lines = output.strip().splitlines()
    if not lines:
        return "unknown"
    virt = lines[0].strip().lower()
    if virt in ("", "unknown"):
        # Probe missing: fall back to container markers, else assume bare metal.
        if "docker" in output.lower() or "lxc" in output.lower():
            return "container"
        return "unknown"
    if virt == "none":
        return "bare_metal"
    if virt in ("docker", "lxc", "podman", "containerd"):
        return "container"
    if virt in ("kvm", "qemu", "vmware", "virtualbox", "xen", "hyperv", "virtualmachine"):
        return "VM"
    if "virtual" in virt:
        return "VM"
    return "bare_metal"


def parse_access_level(output: str) -> str:
    """Parse access level from whoami."""
    return "root" if output.strip() == "root" else "user"


def parse_public_ip(output: str) -> str:
    """Parse public IP: keep the first line that is a bare IPv4/IPv6 address."""
    for line in output.splitlines():
        candidate = line.strip()
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", candidate) and not candidate.startswith("."):
            return candidate
    return "unknown"


def parse_bandwidth(output: str) -> Dict[str, Any]:
    """Parse bandwidth from `name speed` lines (sysfs /sys/class/net/*/speed)."""
    interfaces = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            interfaces.append({
                "name": parts[0],
                "speed": parts[1],
            })
    return {"interfaces": interfaces}


def parse_ports(output: str) -> List[int]:
    """Parse open ports."""
    ports = []
    for line in output.strip().splitlines():
        try:
            ports.append(int(line.strip()))
        except ValueError:
            continue
    return ports


def parse_top_processes(output: str) -> List[Dict[str, Any]]:
    """Parse top 5 processes."""
    processes = []
    for line in output.strip().splitlines():
        parts = line.split(",")
        if len(parts) >= 4:
            try:
                processes.append({
                    "pid": int(parts[0]),
                    "name": parts[1],
                    "cpu_percent": float(parts[2]),
                    "ram_percent": float(parts[3]),
                })
            except (ValueError, IndexError):
                continue
    return processes


# --- SSH Connection ---

def ssh_connect(server: Server, timeout: int = 15) -> Optional[Any]:
    """Establish SSH connection to a server."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=server.ip,
            port=server.port,
            username=server.username,
            password=server.password,
            timeout=timeout,
            banner_timeout=5,
            auth_timeout=5,
        )
        return client
    except Exception as e:
        logging.error(f"SSH connection failed for {server.ip}:{server.port}@{server.username}: {e}")
        return None


def run_ssh_command(client: Any, command: str, timeout: int = 15) -> Tuple[str, str, int]:
    """Run a command on a remote server via SSH.

    Returns (stdout, stderr, exit_code).

    The exit code is what decides whether a probe failed. stderr is returned
    for diagnostics only: on minimal hosts many tools print a warning to
    stderr while still producing a valid result, and treating that as a
    failure is what made 35 of 36 servers look "partial".
    """
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="ignore")
        error = stderr.read().decode("utf-8", errors="ignore")
        # Drain both pipes before asking for the status, otherwise
        # recv_exit_status() can block until the remote command closes.
        try:
            exit_code = stdout.channel.recv_exit_status()
        except Exception:
            exit_code = 0
        return output, error.strip(), exit_code
    except Exception as e:
        logging.error(f"Command failed: {command[:50]}... -> {e}")
        return "", str(e), 255


# --- Data Collection ---

def collect_server_info(server: Server, timeout: int = 15, install_timeout: int = 600) -> ServerInfo:
    """Collect all information from a server, including the 3x-ui panel state."""
    info = ServerInfo(server=server, success=False, error=None, partial_errors=[])
    client = ssh_connect(server, timeout)
    if not client:
        info.error = f"SSH connection failed for {server.ip}:{server.port}"
        info.x3ui = Panel3xUI(state="not_attempted", reason="server unreachable")
        return info

    def probe(name: str, parser: Any) -> Any:
        """Run one probe.

        A probe fails only when the remote command exited non-zero or its
        output could not be parsed. stderr is recorded for diagnostics but
        never decides the verdict on its own, and an empty output with a
        zero exit code is a legitimate empty value, not a failure.
        """
        raw, err, code = run_ssh_command(client, SSH_COMMANDS[name], timeout)
        if code != 0:
            detail = err[:120] if err else f"exit status {code}"
            info.partial_errors.append(f"{name}: {detail}")
            return None
        if not raw.strip():
            return parser(raw)
        try:
            return parser(raw)
        except Exception as e:
            info.partial_errors.append(f"{name}: parse error {e}")
            return None

    try:
        info.os = probe("os", parse_os)
        info.cpu = probe("cpu_info", parse_cpu_info)

        usage_raw, _, usage_code = run_ssh_command(client, SSH_COMMANDS["cpu_usage"], timeout)
        if usage_code != 0:
            info.partial_errors.append(f"cpu_usage: exit status {usage_code}")
        if info.cpu is not None:
            info.cpu["usage"] = parse_cpu_usage(usage_raw) if usage_raw.strip() else None

        info.ram = probe("ram", parse_ram)
        info.disk = probe("disk", parse_disk)
        info.server_type = probe("server_type", parse_server_type)
        info.access_level = probe("access_level", parse_access_level)
        info.public_ip = probe("public_ip", parse_public_ip)
        info.bandwidth = probe("bandwidth", parse_bandwidth)
        info.ports = probe("ports", parse_ports)
        info.top_processes = probe("top_processes", parse_top_processes)

        info.success = True

        # 3x-ui phase: detect, install if missing, read back the access data.
        # Runs while the connection is still open.
        try:
            info.x3ui = collect_xui(server, client, timeout, install_timeout)
        except Exception as e:
            info.x3ui = Panel3xUI(state="failed", reason=f"3x-ui check error: {e}")
    except Exception as e:
        info.error = str(e)
        logging.error(f"Error collecting info from {server.ip}: {e}")
    finally:
        client.close()

    return info


# --- Input Parsing ---

def parse_input_file(file_path: str) -> List[Server]:
    """Parse input file into Server objects."""
    servers = []
    with open(file_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Format: IP:PORT@USERNAME:PASS or IP@USERNAME:PASS
            if "@" not in line:
                logging.warning(f"Line {line_num}: Invalid format (missing @): {line}")
                continue
            try:
                # Split into IP:PORT and USERNAME:PASS
                creds_part, auth_part = line.split("@", 1)
                if ":" in creds_part:
                    ip, port = creds_part.rsplit(":", 1)
                    port = int(port)
                else:
                    ip = creds_part
                    port = 22
                if ":" in auth_part:
                    username, password = auth_part.split(":", 1)
                else:
                    username = auth_part
                    password = ""
                servers.append(Server(
                    ip=ip,
                    port=port,
                    username=username,
                    password=password,
                    raw_line=line,
                ))
            except Exception as e:
                logging.warning(f"Line {line_num}: Failed to parse: {line} -> {e}")
    return servers


# --- 3x-ui Panel ---

XUI_INSTALL_URL = "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"
XUI_RESULT_FILE = "/etc/x-ui/install-result.env"
XUI_BIN = "/usr/local/x-ui/x-ui"

# Is the panel already installed? The Go binary is the primary signal; the
# management script and the service unit are fallbacks.
XUI_DETECT_CMD = (
    "if [ -x /usr/local/x-ui/x-ui ]; then echo PRESENT; "
    "elif command -v x-ui >/dev/null 2>&1; then echo PRESENT; "
    "elif [ -f /etc/x-ui/x-ui.db ]; then echo PRESENT; "
    "else echo ABSENT; fi"
)

# The Go binary prints the current panel settings. `x-ui setting` is NOT a
# subcommand of the bash management script, so the binary is called directly.
XUI_SHOW_CMD = f"{XUI_BIN} setting -show true 2>/dev/null"

# The installer's own machine-readable result file (root-only, mode 600).
XUI_RESULT_CMD = f"cat {XUI_RESULT_FILE} 2>/dev/null"

# Root check without touching anything.
XUI_ROOT_CMD = "id -u"


def parse_xui_settings(output: str) -> Dict[str, Any]:
    """Parse `x-ui setting -show true` output.

    Yields port, web_base_path and has_default_credential. The password is
    not recoverable from the binary — only whether the default one is set.
    """
    result: Dict[str, Any] = {"port": None, "web_base_path": None, "has_default_credential": None}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("port:"):
            try:
                result["port"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("webBasePath:"):
            result["web_base_path"] = line.split(":", 1)[1].strip() or None
        elif line.startswith("hasDefaultCredential:"):
            result["has_default_credential"] = line.split(":", 1)[1].strip().lower() == "true"
    return result


def parse_xui_result_env(output: str) -> Dict[str, Any]:
    """Parse /etc/x-ui/install-result.env (the installer's own output)."""
    result: Dict[str, Any] = {
        "username": None, "password": None, "port": None,
        "web_base_path": None, "url": None,
    }
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key == "XUI_USERNAME":
            result["username"] = value or None
        elif key == "XUI_PASSWORD":
            result["password"] = value or None
        elif key == "XUI_PANEL_PORT":
            try:
                result["port"] = int(value)
            except ValueError:
                pass
        elif key == "XUI_WEB_BASE_PATH":
            result["web_base_path"] = value or None
        elif key == "XUI_ACCESS_URL":
            result["url"] = value or None
    return result


def build_xui_url(host: str, port: Optional[int], web_base_path: Optional[str], scheme: str = "http") -> Optional[str]:
    """Compose the panel web URL."""
    if not port:
        return None
    base = f"{scheme}://{host}:{port}"
    if web_base_path:
        base += f"/{web_base_path}"
    return base


def is_root(client: Any, timeout: int = 30) -> bool:
    """Return True when the SSH session already runs as root."""
    out, _, _ = run_ssh_command(client, XUI_ROOT_CMD, timeout)
    return out.strip() == "0"


def run_privileged(client: Any, password: str, command: str, timeout: int = 15) -> Tuple[str, str, int]:
    """Run a command with root privileges.

    Already-root sessions run the command as-is. Otherwise `sudo -S -p ''`
    takes the password from the remote stdin — the password is never put in
    the command string, so it cannot leak into the process list or the log.
    """
    if is_root(client):
        return run_ssh_command(client, command, timeout)

    sudo_cmd = f"sudo -S -p '' {command}"
    try:
        stdin, stdout, stderr = client.exec_command(sudo_cmd, timeout=timeout)
        stdin.write(password + "\n")
        stdin.flush()
        output = stdout.read().decode("utf-8", errors="ignore")
        error = stderr.read().decode("utf-8", errors="ignore")
        try:
            exit_code = stdout.channel.recv_exit_status()
        except Exception:
            exit_code = 0
    except Exception as e:
        return "", str(e), 255
    return output, error.strip(), exit_code


def detect_xui(client: Any, timeout: int = 15) -> bool:
    """Return True when a 3x-ui panel is already installed on the host."""
    out, _, _ = run_ssh_command(client, XUI_DETECT_CMD, timeout)
    return "PRESENT" in out


def read_xui_state(client: Any, password: str, timeout: int = 15) -> Dict[str, Any]:
    """Read the panel's port/base path and credentials, if readable.

    The credential file is root-only, so the read is retried with sudo when
    the plain read comes back empty.
    """
    state: Dict[str, Any] = {
        "port": None, "web_base_path": None, "username": None, "password": None,
    }

    out, _, _ = run_ssh_command(client, XUI_RESULT_CMD, timeout)
    if not out.strip():
        out, _, _ = run_privileged(client, password, f"cat {XUI_RESULT_FILE}", timeout)
    if out.strip():
        state.update({k: v for k, v in parse_xui_result_env(out).items() if v})

    show_out, _, _ = run_ssh_command(client, XUI_SHOW_CMD, timeout)
    if not show_out.strip():
        show_out, _, _ = run_privileged(client, password, XUI_SHOW_CMD, timeout)
    if show_out.strip():
        settings = parse_xui_settings(show_out)
        if state.get("port") is None:
            state["port"] = settings["port"]
        if state.get("web_base_path") is None:
            state["web_base_path"] = settings["web_base_path"]
    return state


def install_xui(client: Any, password: str, timeout: int = 600) -> Tuple[bool, str, str]:
    """Install 3x-ui with the upstream installer, all options at their defaults.

    The installer requires EUID 0, so a non-root user is elevated with
    `sudo -S`; the SSH password is written to the remote stdin, never into
    the command string. NONINTERACTIVE=1 makes the installer take its own
    defaults instead of prompting. Returns (ok, output_tail, mode) where
    mode is "root" or "sudo".
    """
    # `bash <(curl ...)` needs process substitution, which only works in bash.
    # A dash-based login shell aborts on it, so the script is piped into bash
    # instead — same result, no bashism in the outer command.
    install_cmd = f"curl -Ls {XUI_INSTALL_URL} | bash"
    root = is_root(client)

    if root:
        command = f"export XUI_NONINTERACTIVE=1; {install_cmd}"
        mode = "root"
    else:
        # `sudo -S -p ''` takes the password from stdin and prints no prompt,
        # so only the password reaches the remote stdin. `env` passes the
        # non-interactive switch through to the installer.
        command = f"sudo -S -p '' env XUI_NONINTERACTIVE=1 bash -c '{install_cmd}'"
        mode = "sudo"

    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        if not root:
            stdin.write(password + "\n")
            stdin.flush()
        # Close stdin so the installer gets EOF instead of blocking on a
        # prompt that NONINTERACTIVE=1 was supposed to skip.
        try:
            stdin.channel.shutdown_write()
        except Exception:
            pass
        output = stdout.read().decode("utf-8", errors="ignore")
        error = stderr.read().decode("utf-8", errors="ignore")
        try:
            exit_code = stdout.channel.recv_exit_status()
        except Exception:
            exit_code = 0
    except Exception as e:
        return False, f"install command error: {e}", mode

    tail = (output + "\n" + error).strip()
    if exit_code != 0:
        return False, tail, mode
    lowered = tail.lower()
    if "root privilege" in lowered or "please run this script with root" in lowered:
        return False, tail, mode
    return True, tail, mode


def _xui_failure_reason(tail: str) -> str:
    """Turn the installer's output into a one-line reason."""
    lowered = tail.lower()
    if "not in the sudoers" in lowered:
        return "root required (user is not in the sudoers file)"
    if "incorrect password" in lowered or "authentication failure" in lowered:
        return "root required (sudo password rejected)"
    if "command not found" in lowered and "curl" in lowered:
        return "curl is not available on this host"
    if "no space left" in lowered:
        return "not enough disk space on the host"
    if "could not resolve" in lowered or "couldn't resolve" in lowered:
        return "host cannot reach the internet (DNS failure)"
    if "connection refused" in lowered or "timed out" in lowered:
        return "host cannot reach the download host"
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line:
            return line[:200]
    return "installer failed"


def collect_xui(server: Server, client: Any, timeout: int = 15, install_timeout: int = 600) -> Panel3xUI:
    """Detect, install if needed, and read back the 3x-ui panel state."""
    panel = Panel3xUI()

    try:
        if detect_xui(client, timeout):
            panel.state = "preexisting"
            state = read_xui_state(client, server.password, timeout)
            panel.port = state.get("port")
            panel.web_base_path = state.get("web_base_path")
            panel.username = state.get("username")
            panel.password = state.get("password")
            if not panel.port:
                panel.reason = "panel present but settings unreadable (root required)"
            else:
                panel.url = build_xui_url(server.ip, panel.port, panel.web_base_path)
                if not panel.username:
                    panel.reason = "panel present; credentials not readable without root"
            return panel

        # Not installed. The installer requires root; a non-root user is
        # elevated with `sudo -S` and the SSH password.
        ok, tail, mode = install_xui(client, server.password, install_timeout)
        panel.install_log = tail[-2000:]
        if not ok:
            lowered = tail.lower()
            if ("not in the sudoers" in lowered
                    or "incorrect password" in lowered
                    or "authentication failure" in lowered
                    or "root privilege" in lowered
                    or "please run this script with root" in lowered):
                panel.state = "not_attempted"
                panel.reason = "root required (sudo unavailable for this user)"
            else:
                panel.state = "failed"
                panel.reason = _xui_failure_reason(tail)
            return panel

        panel.state = "installed"
        # Verify the panel is really there before claiming success.
        if not detect_xui(client, timeout):
            panel.state = "failed"
            panel.reason = "installer finished but the panel binary was not found"
            return panel
        state = read_xui_state(client, server.password, timeout)
        panel.port = state.get("port")
        panel.web_base_path = state.get("web_base_path")
        panel.username = state.get("username")
        panel.password = state.get("password")
        panel.url = build_xui_url(server.ip, panel.port, panel.web_base_path)
        if not panel.username or not panel.password:
            panel.reason = "installed, but credentials not readable (credential file is root-only)"
        return panel

    except Exception as e:
        panel.state = "failed"
        panel.reason = f"3x-ui check error: {e}"
        return panel

# --- Output ---

def generate_text_report(report: Report) -> str:
    """Generate a plain-text report — one readable block per server.

    No table: the fleet report has to stay readable without horizontal
    scrolling, and the 3x-ui credentials need their own labelled lines.
    """
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("SERVER MONITOR REPORT")
    lines.append(f"Generated: {report.timestamp}")
    lines.append(f"Input:     {report.input_file}")
    lines.append("=" * 72)
    lines.append("")

    for idx, info in enumerate(report.servers, 1):
        host = f"{info.server.ip}:{info.server.port}"
        lines.append("-" * 72)
        lines.append(f"Server {idx}: {host}   (user: {info.server.username})")
        lines.append("-" * 72)

        if info.error:
            lines.append(f"  Error:      {info.error}")
        else:
            os_info = info.os or {}
            os_str = " ".join(x for x in (os_info.get("name"), os_info.get("version")) if x) or "?"
            lines.append(f"  OS:         {os_str}")

            cpu = info.cpu or {}
            model = cpu.get("model", "?")
            usage = cpu.get("usage")
            lines.append(f"  CPU:        {model}" + (f"  ({usage}%)" if usage is not None else ""))

            ram = info.ram or {}
            if ram:
                lines.append(f"  RAM:        {ram.get('total', 0)} MB total, {ram.get('used', 0)} MB used")

            disk = (info.disk or [None])[0]
            if disk:
                lines.append(f"  Disk:       /  {disk['total']} GB total, {disk['used']} GB used")

            lines.append(f"  Type:       {info.server_type or '?'}")
            lines.append(f"  Access:     {info.access_level or '?'}")
            lines.append(f"  Public IP:  {info.public_ip or '?'}")

            ifaces = (info.bandwidth or {}).get("interfaces", [])
            known = [i for i in ifaces if i["name"] != "lo" and i["speed"] != "NA"]
            if known:
                lines.append("  Bandwidth:  " + ", ".join(f"{i['name']}: {i['speed']}Mb" for i in known))
            elif ifaces:
                lines.append("  Bandwidth:  " + ", ".join(f"{i['name']}: {i['speed']}" for i in ifaces if i["name"] != "lo"))

            if info.ports:
                lines.append(f"  Ports:      {', '.join(map(str, info.ports))}")

            if info.top_processes:
                procs = ", ".join(
                    f"{p['name']} ({p['cpu_percent']}%CPU, {p['ram_percent']}%MEM)"
                    for p in info.top_processes[:5]
                )
                lines.append(f"  Processes:  {procs}")

        panel = info.x3ui
        if panel:
            label = {
                "installed": "INSTALLED (by this run)",
                "preexisting": "ALREADY PRESENT",
                "failed": "INSTALL FAILED",
                "not_attempted": "NOT INSTALLED",
            }.get(panel.state, panel.state.upper())
            lines.append(f"  3x-ui:      {label}")
            if panel.url:
                lines.append(f"  Panel URL:  {panel.url}")
            if panel.username:
                lines.append(f"  Username:   {panel.username}")
            if panel.password:
                lines.append(f"  Password:   {panel.password}")
            if panel.reason:
                lines.append(f"  Note:       {panel.reason}")

        if info.partial_errors:
            lines.append(f"  Warnings:   {len(info.partial_errors)} metric(s) unavailable")
            for err in info.partial_errors:
                lines.append(f"               - {err}")

        lines.append("")

    # --- 3x-ui categories ---
    lines.append("=" * 72)
    lines.append("3x-ui SUMMARY")
    lines.append("=" * 72)

    categories = {
        "installed": "INSTALLED BY THIS RUN",
        "preexisting": "ALREADY PRESENT",
        "failed": "INSTALL FAILED",
        "not_attempted": "NOT INSTALLED",
    }
    for state, title in categories.items():
        servers = [
            f"{i.server.ip}:{i.server.port}"
            for i in report.servers
            if i.x3ui and i.x3ui.state == state
        ]
        lines.append("")
        lines.append(f"--- {title} ({len(servers)}) ---")
        if not servers:
            lines.append("    (none)")
            continue
        for entry in servers:
            lines.append(f"    {entry}")

    # Panel credentials, grouped for the servers that have them.
    with_creds = [i for i in report.servers if i.x3ui and i.x3ui.username]
    lines.append("")
    lines.append("=" * 72)
    lines.append("3x-ui PANEL ACCESS")
    lines.append("=" * 72)
    if not with_creds:
        lines.append("    (no panel credentials available)")
    for info in with_creds:
        panel = info.x3ui
        lines.append("")
        lines.append(f"  {info.server.ip}:{info.server.port}")
        lines.append(f"      URL:       {panel.url or '?'}")
        lines.append(f"      Username:  {panel.username}")
        lines.append(f"      Password:  {panel.password or '?'}")

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"SUMMARY: {report.summary}")
    lines.append(f"3x-ui:   {report.x3ui_summary}")
    lines.append("=" * 72)
    return "\n".join(lines)


def save_json(report: Report, output_path: Optional[str] = None) -> str:
    """Save report as JSON (mode 600 — it contains panel credentials)."""
    if output_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = f"report_{timestamp}.json"

    # asdict converts nested dataclasses automatically
    report_dict = asdict(report)
    # Never write the SSH credentials of the input file into the report.
    for entry in report_dict.get("servers", []):
        entry.get("server", {}).pop("password", None)
        entry.get("server", {}).pop("raw_line", None)

    with open(output_path, "w") as f:
        json.dump(report_dict, f, indent=2, ensure_ascii=False, default=str)
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass

    return output_path


def save_text_report(report: Report, output_path: Optional[str] = None) -> str:
    """Save the plain-text report (mode 600 — it contains panel credentials)."""
    if output_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = f"report_{timestamp}.txt"
    with open(output_path, "w") as f:
        f.write(generate_text_report(report))
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass
    return output_path


# --- Parallel Execution ---

def run_parallel(servers: List[Server], max_workers: int = 10, timeout: int = 15,
                 install_timeout: int = 600) -> Report:
    """Run server monitoring in parallel batches.

    A server counts as a success when its 3x-ui panel is installed by this
    run or was already present; every other server is a failure.
    """
    report = Report(
        timestamp=datetime.now(timezone.utc).isoformat(),
        input_file="",
        servers=[],
        summary={"total": len(servers), "success": 0, "failed": 0, "reachable": 0, "partial": 0},
        x3ui_summary={"installed": 0, "preexisting": 0, "failed": 0, "not_attempted": 0},
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect_server_info, server, timeout, install_timeout): server
            for server in servers
        }
        for future in as_completed(futures):
            server = futures[future]
            try:
                info = future.result()
                report.servers.append(info)

                if info.success:
                    report.summary["reachable"] += 1
                if info.partial_errors:
                    report.summary["partial"] += 1

                panel = info.x3ui
                if panel is not None:
                    state = panel.state if panel.state in report.x3ui_summary else "not_attempted"
                    report.x3ui_summary[state] += 1

                if panel is not None and panel.state in ("installed", "preexisting"):
                    report.summary["success"] += 1
                else:
                    report.summary["failed"] += 1
            except Exception as e:
                logging.error(f"Error processing {server.ip}: {e}")
                report.servers.append(ServerInfo(
                    server=server,
                    success=False,
                    error=str(e),
                    x3ui=Panel3xUI(state="not_attempted", reason=f"error: {e}"),
                ))
                report.summary["failed"] += 1
                report.x3ui_summary["not_attempted"] += 1

    # Keep the report in input order, not completion order.
    def _order_key(info: ServerInfo) -> Tuple[Any, ...]:
        return (info.server.ip, info.server.port, info.server.username)

    report.servers.sort(key=_order_key)

    return report


# --- Main ---

def main():
    _bootstrap()
    parser = argparse.ArgumentParser(description="Server Monitor - Parallel SSH")
    parser.add_argument("input_file", help="Path to input file (IP:PORT@USERNAME:PASS per line)")
    parser.add_argument("--output", help="Custom JSON output path")
    parser.add_argument("--text-output", help="Custom plain-text report path")
    parser.add_argument("--max-workers", type=int, default=10, help="Max concurrent SSH sessions (default: 10)")
    parser.add_argument("--timeout", type=int, default=15, help="SSH timeout in seconds (default: 15)")
    parser.add_argument("--install-timeout", type=int, default=600,
                        help="Timeout for the 3x-ui installer in seconds (default: 600)")
    parser.add_argument("--log", help="Log file path (default: server_monitor.log)")
    args = parser.parse_args()

    # Setup logging
    log_path = args.log or "server_monitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Parse input
    servers = parse_input_file(args.input_file)
    if not servers:
        logging.warning("No valid servers found in input file.")
        return

    logging.info(f"Processing {len(servers)} servers with {args.max_workers} parallel workers...")

    # Run parallel monitoring
    report = run_parallel(servers, args.max_workers, args.timeout, args.install_timeout)
    report.input_file = args.input_file

    # Output: JSON + plain-text report. No terminal table.
    json_path = save_json(report, args.output)
    text_path = save_text_report(report, args.text_output)
    logging.info(f"JSON report saved to: {json_path}")
    logging.info(f"Text report saved to: {text_path}")
    logging.info(f"Summary: {report.summary}")
    logging.info(f"3x-ui: {report.x3ui_summary}")


if __name__ == "__main__":
    main()
