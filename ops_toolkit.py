"""
Shared Windows Server Operations Toolkit (`ops_toolkit.py`).

Used by BOTH:
1. `sdk_apikey` mode — via OpenCode (`opencode run`) or imported directly in Python.
2. `cli_subscription` mode — invoked via CLI (`python ops_toolkit.py <tool> ...`) or imported.

Supports:
- Multi-file action logs per service (`list_service_log_files` + `get_service_logs` across named log categories or all log files)
- Source code inspection (`search_service_code` + `read_service_code_file` jailed inside `working_dir`)
- PM2, ngrok, n8n, Windows Services / Event Viewer, Read-Only MySQL, and Gated Admin Approvals.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
import psutil

from agent.config import AppConfig, ServiceDefinition, load_config
from agent.security import redact_secrets, validate_readonly_sql

IGNORED_CODE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
}


def _run_cmd(
    argv: List[str],
    timeout: int = 15,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    shell: bool = False,
) -> Dict[str, Any]:
    """Run a subprocess safely with timeout and secret redaction."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            argv if not shell else " ".join(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd if cwd and Path(cwd).exists() else None,
            env=merged_env,
            shell=shell,
            errors="replace",
        )
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": redact_secrets(completed.stdout.strip()),
            "stderr": redact_secrets(completed.stderr.strip()),
        }
    except FileNotFoundError as exc:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"Command not found: {exc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -2, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "exit_code": -3, "stdout": "", "stderr": f"Subprocess error: {exc}"}


def _tail_file(file_path: Path, lines: int = 100, filter_keyword: str = "") -> str:
    """Read the last `lines` lines from a file, optionally filtering by keyword."""
    if not file_path.exists() or not file_path.is_file():
        return f"[File not found: {file_path}]"
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        if filter_keyword:
            kw = filter_keyword.lower()
            all_lines = [ln for ln in all_lines if kw in ln.lower()]
        selected = all_lines[-max(1, min(lines, 500)) :]
        return redact_secrets("".join(selected).strip())
    except Exception as exc:
        return f"[Error reading {file_path}: {exc}]"


class PendingActionStore:
    """Thread/process-safe JSON store for gated admin actions awaiting Telegram button click."""

    def __init__(self, file_path: str | Path = "./.pending_actions.json", expiry_seconds: int = 600) -> None:
        self.file_path = Path(file_path)
        self.expiry_seconds = expiry_seconds

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        if not self.file_path.exists():
            return {}
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            now = time.time()
            return {
                k: v
                for k, v in data.items()
                if isinstance(v, dict) and (now - float(v.get("created_at", 0))) < self.expiry_seconds
            }
        except Exception:
            return {}

    def _save_all(self, actions: Dict[str, Dict[str, Any]]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(json.dumps(actions, indent=2, ensure_ascii=False), encoding="utf-8")

    def create_action(
        self,
        action_type: str,
        target: str,
        reason: str,
        requested_by_user_id: Optional[int] = None,
        requested_by_username: str = "",
    ) -> Dict[str, Any]:
        actions = self._load_all()
        action_id = uuid.uuid4().hex[:8]
        record = {
            "action_id": action_id,
            "action_type": action_type,
            "target": target,
            "reason": reason,
            "status": "pending",
            "created_at": time.time(),
            "requested_by_user_id": requested_by_user_id,
            "requested_by_username": requested_by_username,
        }
        actions[action_id] = record
        self._save_all(actions)
        return record

    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        return self._load_all().get(action_id)

    def list_pending(self) -> List[Dict[str, Any]]:
        return [a for a in self._load_all().values() if a.get("status") == "pending"]

    def pop_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        actions = self._load_all()
        item = actions.pop(action_id, None)
        self._save_all(actions)
        return item


class OpsToolkit:
    """Unified diagnostic, multi-log, source-code, and gated-action toolkit for Windows Server."""

    ALLOWED_ACTION_TYPES = (
        "pm2_restart",
        "pm2_reload",
        "windows_service_restart",
        "mysql_kill_query",
    )

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.action_store = PendingActionStore(
            file_path=config.engine.pending_actions_file,
            expiry_seconds=config.telegram.action_expiry_seconds,
        )

    # -------------------------------------------------------------------------
    # 1. SYSTEM RESOURCES & OVERVIEW
    # -------------------------------------------------------------------------
    def get_system_resources(self) -> Dict[str, Any]:
        """Inspect CPU, RAM, disk usage across drives, boot time, and top processes by CPU/Memory."""
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu_pct = psutil.cpu_percent(interval=0.4)

        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append(
                    {
                        "mount": part.mountpoint,
                        "fstype": part.fstype,
                        "total_gb": round(usage.total / (1024**3), 2),
                        "used_gb": round(usage.used / (1024**3), 2),
                        "free_gb": round(usage.free / (1024**3), 2),
                        "percent": usage.percent,
                    }
                )
            except Exception:
                continue

        procs = []
        for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_info", "cmdline"]):
            try:
                info = p.info
                mem_mb = round((info.get("memory_info").rss if info.get("memory_info") else 0) / (1024**2), 1)
                cmdline_list = info.get("cmdline") or []
                cmdline_str = redact_secrets(" ".join(cmdline_list)[:180])
                procs.append(
                    {
                        "pid": info.get("pid"),
                        "name": info.get("name"),
                        "cpu_percent": info.get("cpu_percent") or 0.0,
                        "memory_mb": mem_mb,
                        "cmdline": cmdline_str,
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        top_by_mem = sorted(procs, key=lambda x: x["memory_mb"], reverse=True)[:10]
        top_by_cpu = sorted(procs, key=lambda x: x["cpu_percent"], reverse=True)[:10]

        return {
            "server_name": self.config.server_name,
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 2),
            "cpu_percent": cpu_pct,
            "cpu_count": psutil.cpu_count(logical=True),
            "memory": {
                "total_gb": round(vm.total / (1024**3), 2),
                "available_gb": round(vm.available / (1024**3), 2),
                "used_gb": round(vm.used / (1024**3), 2),
                "percent": vm.percent,
                "swap_percent": swap.percent,
            },
            "disks": disks,
            "top_processes_by_memory": top_by_mem,
            "top_processes_by_cpu": top_by_cpu,
        }

    # -------------------------------------------------------------------------
    # 2. MULTI-SERVICE REGISTRY OVERVIEW
    # -------------------------------------------------------------------------
    def list_registered_services_status(self) -> Dict[str, Any]:
        """Check status, health URL, port, log categories, and process state of all services in config.yaml."""
        pm2_map: Dict[str, Dict[str, Any]] = {}
        if self.config.integrations.pm2.enabled:
            pm2_data = self.get_pm2_status()
            for item in pm2_data.get("processes", []):
                if isinstance(item, dict) and item.get("name"):
                    pm2_map[str(item["name"])] = item

        results: Dict[str, Any] = {}
        for name, svc in self.config.services.items():
            entry: Dict[str, Any] = {
                "description": svc.description,
                "runtime": svc.runtime,
                "manager": svc.manager,
                "working_dir": svc.working_dir,
                "logs_dir": svc.logs_dir,
                "log_categories": list(svc.log_files.keys()),
                "port": svc.port,
            }
            if svc.manager == "pm2" and svc.pm2_name:
                entry["pm2"] = pm2_map.get(svc.pm2_name, {"status": "not_found_in_pm2"})
            elif svc.manager == "windows_service" and svc.win_service_name:
                entry["windows_service"] = self._check_single_windows_service(svc.win_service_name)

            if svc.health_url:
                entry["health_check"] = self._check_http_endpoint(svc.health_url)

            if svc.port:
                entry["port_listening"] = self._is_port_listening(svc.port)

            results[name] = entry

        return {"server": self.config.server_name, "services": results}

    def _is_port_listening(self, port: int) -> bool:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                    return True
        except Exception:
            pass
        return False

    def _check_http_endpoint(self, url: str, timeout: float = 4.0) -> Dict[str, Any]:
        start = time.time()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url)
                return {
                    "reachable": True,
                    "status_code": resp.status_code,
                    "latency_ms": round((time.time() - start) * 1000, 1),
                    "body_preview": redact_secrets(resp.text[:300]),
                }
        except Exception as exc:
            return {
                "reachable": False,
                "error": str(exc),
                "latency_ms": round((time.time() - start) * 1000, 1),
            }

    # -------------------------------------------------------------------------
    # 3. PM2 INSPECTOR (Python & Node.js Services)
    # -------------------------------------------------------------------------
    def get_pm2_status(self) -> Dict[str, Any]:
        """Run `pm2 jlist` and return structured status for all PM2 processes."""
        if not self.config.integrations.pm2.enabled:
            return {"enabled": False, "error": "PM2 integration is disabled in config.yaml"}

        pm2_bin = self.config.integrations.pm2.pm2_bin
        env = {}
        if self.config.integrations.pm2.pm2_home:
            env["PM2_HOME"] = self.config.integrations.pm2.pm2_home

        is_win = platform.system().lower().startswith("win")
        res = _run_cmd([pm2_bin, "jlist"], timeout=15, env=env, shell=is_win)
        if not res["ok"]:
            return {"ok": False, "error": res["stderr"] or res["stdout"]}

        try:
            raw_list = json.loads(res["stdout"] or "[]")
            processes = []
            for proc in raw_list:
                pm2_env = proc.get("pm2_env", {}) or {}
                monit = proc.get("monit", {}) or {}
                processes.append(
                    {
                        "pm_id": proc.get("pm_id"),
                        "name": proc.get("name"),
                        "pid": proc.get("pid"),
                        "status": pm2_env.get("status"),
                        "restart_time": pm2_env.get("restart_time"),
                        "unstable_restarts": pm2_env.get("unstable_restarts"),
                        "uptime_ms": int(time.time() * 1000 - pm2_env.get("pm_uptime", time.time() * 1000))
                        if pm2_env.get("pm_uptime")
                        else None,
                        "cpu_percent": monit.get("cpu"),
                        "memory_mb": round((monit.get("memory") or 0) / (1024**2), 1),
                        "script_path": pm2_env.get("pm_exec_path"),
                        "cwd": pm2_env.get("pm_cwd"),
                        "exec_interpreter": pm2_env.get("exec_interpreter"),
                        "out_log_path": pm2_env.get("pm_out_log_path"),
                        "err_log_path": pm2_env.get("pm_err_log_path"),
                    }
                )
            return {"ok": True, "count": len(processes), "processes": processes}
        except Exception as exc:
            return {"ok": False, "error": f"Failed to parse pm2 jlist output: {exc}", "raw": res["stdout"][:500]}

    def get_pm2_logs(self, app_name: str, lines: int = 100, log_type: str = "all") -> Dict[str, Any]:
        """Read PM2 stdout and error logs for a given PM2 process name."""
        lines = max(10, min(int(lines), 400))
        pm2_home = self.config.integrations.pm2.pm2_home or str(Path.home() / ".pm2")
        logs_dir = Path(pm2_home) / "logs"

        safe_name = app_name.replace("_", "-")
        candidates = [
            logs_dir / f"{app_name}-error.log",
            logs_dir / f"{app_name}-out.log",
            logs_dir / f"{safe_name}-error.log",
            logs_dir / f"{safe_name}-out.log",
        ]
        found_logs: Dict[str, str] = {}
        for path in candidates:
            if path.exists():
                if log_type == "error" and "out.log" in path.name:
                    continue
                if log_type == "out" and "error.log" in path.name:
                    continue
                found_logs[str(path)] = _tail_file(path, lines=lines)

        if found_logs:
            return {"ok": True, "app_name": app_name, "source": "pm2_log_files", "logs": found_logs}

        pm2_bin = self.config.integrations.pm2.pm2_bin
        is_win = platform.system().lower().startswith("win")
        res = _run_cmd([pm2_bin, "logs", app_name, "--lines", str(lines), "--nostream"], timeout=15, shell=is_win)
        return {
            "ok": res["ok"],
            "app_name": app_name,
            "source": "pm2_cli",
            "output": res["stdout"] or res["stderr"],
        }

    # -------------------------------------------------------------------------
    # 4. MULTI-FILE ACTION LOG DISCOVERY & READING
    # -------------------------------------------------------------------------
    def _discover_service_log_paths(self, svc: ServiceDefinition) -> List[Dict[str, Any]]:
        """Discover all log files for a service across `log_files`, `logs_dir`, and `log_paths`."""
        seen_paths: Set[str] = set()
        entries: List[Dict[str, Any]] = []

        def _add_file(fpath: Path, category: str) -> None:
            try:
                resolved = str(fpath.resolve())
            except Exception:
                resolved = str(fpath)
            if resolved in seen_paths or not fpath.exists() or not fpath.is_file():
                return
            seen_paths.add(resolved)
            try:
                stat = fpath.stat()
                entries.append(
                    {
                        "category": category,
                        "filename": fpath.name,
                        "path": str(fpath),
                        "size_kb": round(stat.st_size / 1024, 1),
                        "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        "mtime": stat.st_mtime,
                    }
                )
            except Exception:
                pass

        # 1. Named action log categories (`log_files` map in config.yaml)
        for cat_name, pattern in svc.log_files.items():
            for p_str in glob.glob(pattern):
                _add_file(Path(p_str), category=cat_name)

        # 2. All `.log` / `.txt` files inside `logs_dir` (including subdirectories)
        if svc.logs_dir and Path(svc.logs_dir).exists():
            logs_root = Path(svc.logs_dir)
            for ext in ("*.log", "*.log.*", "*.txt", "*.jsonl"):
                for p in logs_root.rglob(ext):
                    _add_file(p, category=p.stem.split(".")[0])

        # 3. General `log_paths` globs
        for pattern in svc.log_paths:
            for p_str in glob.glob(pattern):
                p = Path(p_str)
                _add_file(p, category=p.stem.split(".")[0])

        entries.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        for e in entries:
            e.pop("mtime", None)
        return entries

    def list_service_log_files(self, service_name: str) -> Dict[str, Any]:
        """
        List all available log files and action log categories for a registered service,
        including file sizes and last-modified timestamps.
        """
        svc: Optional[ServiceDefinition] = self.config.services.get(service_name)
        if not svc:
            return {
                "ok": False,
                "error": f"Unknown service '{service_name}'. Registered services: {list(self.config.services.keys())}",
            }

        discovered = self._discover_service_log_paths(svc)
        return {
            "ok": True,
            "service_name": service_name,
            "configured_categories": svc.log_files,
            "logs_dir": svc.logs_dir,
            "total_files_found": len(discovered),
            "files": discovered,
        }

    def get_service_logs(
        self,
        service_name: str,
        lines: int = 100,
        filter_keyword: str = "",
        category_or_file: str = "",
        max_files: int = 10,
    ) -> Dict[str, Any]:
        """
        Read recent log lines across a service's multiple action log files.
        - `category_or_file`: Optional action log category key (e.g. 'payment', 'webhook', 'auth')
          or filename substring (e.g. 'payment_2026.log'). If empty, reads across all active log files!
        - `filter_keyword`: Optional substring filter (e.g. 'ERROR', 'Traceback', 'order_123').
        """
        svc: Optional[ServiceDefinition] = self.config.services.get(service_name)
        if not svc:
            return {
                "ok": False,
                "error": f"Unknown service '{service_name}'. Registered services: {list(self.config.services.keys())}",
            }

        discovered = self._discover_service_log_paths(svc)
        target_filter = category_or_file.strip().lower()

        if target_filter:
            # First check if `category_or_file` matches a configured category or discovered filename
            filtered_entries = [
                e
                for e in discovered
                if target_filter in e["category"].lower()
                or target_filter in e["filename"].lower()
                or target_filter in e["path"].lower()
            ]
        else:
            filtered_entries = discovered

        collected: Dict[str, Dict[str, Any]] = {}
        for entry in filtered_entries[: max(1, min(int(max_files), 25))]:
            fpath = Path(entry["path"])
            content = _tail_file(fpath, lines=lines, filter_keyword=filter_keyword)
            if filter_keyword and not content:
                continue
            collected[entry["path"]] = {
                "category": entry["category"],
                "filename": entry["filename"],
                "size_kb": entry["size_kb"],
                "modified_utc": entry["modified_utc"],
                "tail": content,
            }

        if not collected and not target_filter and svc.manager == "pm2" and svc.pm2_name:
            return self.get_pm2_logs(svc.pm2_name, lines=lines)

        return {
            "ok": True,
            "service_name": service_name,
            "category_or_file_filter": category_or_file or "ALL",
            "keyword_filter": filter_keyword or None,
            "matched_files_count": len(collected),
            "available_categories": list(svc.log_files.keys()),
            "logs": collected,
        }

    # -------------------------------------------------------------------------
    # 5. SERVICE SOURCE CODE INSPECTION (Search & Read Code Safely)
    # -------------------------------------------------------------------------
    def _resolve_jailed_service_path(self, svc: ServiceDefinition, relative_path: str = "") -> Path:
        """Ensure any requested file path stays strictly inside the service's `working_dir`."""
        if not svc.working_dir:
            raise ValueError(f"Service '{svc.name}' does not have a `working_dir` configured.")
        base = Path(svc.working_dir).resolve()
        if not base.exists():
            raise FileNotFoundError(f"Service working_dir does not exist on disk: {base}")
        target = (base / relative_path).resolve() if relative_path else base
        if base != target and base not in target.parents:
            raise PermissionError(f"Path traversal outside service working_dir ({base}) is blocked.")
        return target

    def search_service_code(
        self,
        service_name: str,
        query: str,
        file_pattern: str = "*",
        max_matches: int = 40,
    ) -> Dict[str, Any]:
        """
        Search (`grep`) across a service's source code inside `working_dir` for a function,
        error string, route, or variable name. Automatically skips `node_modules`, `.venv`, `.git`, etc.
        """
        svc = self.config.services.get(service_name)
        if not svc:
            return {
                "ok": False,
                "error": f"Unknown service '{service_name}'. Registered: {list(self.config.services.keys())}",
            }
        if not query or not query.strip():
            return {"ok": False, "error": "Search query cannot be empty."}

        try:
            root = self._resolve_jailed_service_path(svc)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        needle = query.strip().lower()
        matches: List[Dict[str, Any]] = []
        max_matches = max(1, min(int(max_matches), 100))

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune heavy or irrelevant directories in-place
            dirnames[:] = [d for d in dirnames if d not in IGNORED_CODE_DIRS and not d.startswith(".git")]
            for fname in filenames:
                if file_pattern and file_pattern != "*" and not fnmatch.fnmatch(fname, file_pattern):
                    continue
                # Skip dotenv or binary files
                if fname == ".env" or fname.endswith((".pyc", ".pyo", ".exe", ".dll", ".so", ".png", ".jpg", ".zip")):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_size > 2 * 1024 * 1024:  # Skip files > 2MB
                        continue
                    with fpath.open("r", encoding="utf-8", errors="replace") as f:
                        for idx, line in enumerate(f, start=1):
                            if needle in line.lower():
                                rel = str(fpath.relative_to(root))
                                matches.append(
                                    {
                                        "file": rel,
                                        "line": idx,
                                        "content": redact_secrets(line.rstrip()[:240]),
                                    }
                                )
                                if len(matches) >= max_matches:
                                    return {
                                        "ok": True,
                                        "service_name": service_name,
                                        "working_dir": str(root),
                                        "query": query,
                                        "truncated": True,
                                        "matches": matches,
                                    }
                except Exception:
                    continue

        return {
            "ok": True,
            "service_name": service_name,
            "working_dir": str(root),
            "query": query,
            "truncated": False,
            "matches": matches,
        }

    def read_service_code_file(
        self,
        service_name: str,
        relative_path: str,
        start_line: int = 1,
        end_line: int = 160,
    ) -> Dict[str, Any]:
        """
        Read source code lines from a file inside a service's `working_dir` (with line numbers).
        Blocks reading `.env` files or traversing outside `working_dir`.
        """
        svc = self.config.services.get(service_name)
        if not svc:
            return {
                "ok": False,
                "error": f"Unknown service '{service_name}'. Registered: {list(self.config.services.keys())}",
            }

        try:
            target = self._resolve_jailed_service_path(svc, relative_path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if target.name == ".env" or target.name.startswith(".env."):
            return {"ok": False, "error": "Reading `.env` secret files is blocked by security policy."}

        if not target.exists() or not target.is_file():
            return {"ok": False, "error": f"Source file not found: {relative_path}"}

        try:
            with target.open("r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            total = len(all_lines)
            s_idx = max(1, int(start_line))
            e_idx = min(total, max(s_idx, min(int(end_line), s_idx + 250)))
            numbered = [
                f"{line_no:4d} | {redact_secrets(all_lines[line_no - 1].rstrip())}"
                for line_no in range(s_idx, e_idx + 1)
            ]
            return {
                "ok": True,
                "service_name": service_name,
                "file": relative_path,
                "start_line": s_idx,
                "end_line": e_idx,
                "total_lines": total,
                "content": "\n".join(numbered),
            }
        except Exception as exc:
            return {"ok": False, "error": f"Failed to read {relative_path}: {exc}"}

    # -------------------------------------------------------------------------
    # 6. WINDOWS SERVICES & EVENT VIEWER
    # -------------------------------------------------------------------------
    def _check_single_windows_service(self, win_service_name: str) -> Dict[str, Any]:
        if not platform.system().lower().startswith("win"):
            return {"status": "unavailable_on_non_windows", "service_name": win_service_name}
        try:
            svc = psutil.win_service_get(win_service_name)
            return svc.as_dict()
        except Exception as exc:
            return {"status": "error_or_not_found", "service_name": win_service_name, "error": str(exc)}

    def get_windows_event_logs(
        self,
        log_name: str = "Application",
        source_filter: str = "",
        max_events: int = 25,
        level: str = "Error,Warning",
    ) -> Dict[str, Any]:
        """Query Windows Event Viewer (`Get-WinEvent`) for recent application crashes or service errors."""
        max_events = max(1, min(int(max_events), 100))
        if not platform.system().lower().startswith("win"):
            return {
                "ok": False,
                "note": "Windows Event Viewer (Get-WinEvent) only runs on Windows Server hosts.",
            }

        levels = []
        if "critical" in level.lower():
            levels.append("1")
        if "error" in level.lower():
            levels.append("2")
        if "warn" in level.lower():
            levels.append("3")
        level_clause = f"; Level=@({','.join(levels)})" if levels else ""
        provider_clause = f"; ProviderName='*{source_filter}*'" if source_filter else ""

        ps_script = (
            f"Get-WinEvent -FilterHashtable @{{LogName='{log_name}'{level_clause}{provider_clause}}} "
            f"-MaxEvents {max_events} -ErrorAction SilentlyContinue | "
            f"Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message | ConvertTo-Json -Depth 3"
        )
        res = _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script], timeout=20)
        if not res["ok"] and not res["stdout"]:
            return {"ok": False, "error": res["stderr"] or "No matching Windows events found."}

        try:
            parsed = json.loads(res["stdout"]) if res["stdout"] else []
            if isinstance(parsed, dict):
                parsed = [parsed]
            return {"ok": True, "log_name": log_name, "events": parsed}
        except Exception:
            return {"ok": True, "log_name": log_name, "raw_output": res["stdout"][:3000]}

    # -------------------------------------------------------------------------
    # 7. NGROK & N8N INSPECTORS
    # -------------------------------------------------------------------------
    def check_ngrok_status(self) -> Dict[str, Any]:
        """Inspect ngrok local API (`http://127.0.0.1:4040/api/tunnels` and `/api/requests/http`)."""
        if not self.config.integrations.ngrok.enabled:
            return {"enabled": False, "error": "ngrok integration is disabled in config.yaml"}

        base = self.config.integrations.ngrok.inspector_url.rstrip("/")
        result: Dict[str, Any] = {"inspector_url": base}
        try:
            with httpx.Client(timeout=4.0) as client:
                tunnels_resp = client.get(f"{base}/api/tunnels")
                tunnels_data = tunnels_resp.json()
                tunnels = []
                for t in tunnels_data.get("tunnels", []):
                    tunnels.append(
                        {
                            "name": t.get("name"),
                            "public_url": t.get("public_url"),
                            "proto": t.get("proto"),
                            "forwards_to": (t.get("config") or {}).get("addr"),
                            "metrics": t.get("metrics"),
                        }
                    )
                result["ok"] = True
                result["active_tunnels"] = tunnels

                reqs_resp = client.get(f"{base}/api/requests/http?limit=15")
                if reqs_resp.status_code == 200:
                    reqs_list = reqs_resp.json().get("requests", [])
                    recent_requests = []
                    for r in reqs_list[:15]:
                        req_info = r.get("request", {}) or {}
                        resp_info = r.get("response", {}) or {}
                        recent_requests.append(
                            {
                                "id": r.get("id"),
                                "start": r.get("start"),
                                "method": req_info.get("method"),
                                "uri": req_info.get("uri"),
                                "status_code": resp_info.get("status_code"),
                                "duration_ms": round((r.get("duration") or 0) / 1e6, 1)
                                if r.get("duration")
                                else None,
                            }
                        )
                    result["recent_http_requests"] = recent_requests
        except Exception as exc:
            result["ok"] = False
            result["error"] = f"Could not reach ngrok local API at {base}: {exc}"
        return result

    def check_n8n_status(self, log_lines: int = 80) -> Dict[str, Any]:
        """Inspect n8n healthz endpoint, PM2/service state, and recent n8n log lines."""
        cfg = self.config.integrations.n8n
        if not cfg.enabled:
            return {"enabled": False, "error": "n8n integration is disabled in config.yaml"}

        out: Dict[str, Any] = {
            "health_check": self._check_http_endpoint(cfg.health_url),
        }
        if cfg.pm2_name and self.config.integrations.pm2.enabled:
            pm2_all = self.get_pm2_status()
            for p in pm2_all.get("processes", []):
                if p.get("name") == cfg.pm2_name:
                    out["pm2_process"] = p
                    break

        logs_collected: Dict[str, str] = {}
        for pattern in cfg.log_paths:
            matched = sorted(
                (Path(p) for p in glob.glob(pattern)),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            for fpath in matched[:3]:
                logs_collected[str(fpath)] = _tail_file(fpath, lines=log_lines)
        out["logs"] = logs_collected
        return out

    # -------------------------------------------------------------------------
    # 8. READ-ONLY MYSQL DATABASE INSPECTOR
    # -------------------------------------------------------------------------
    def query_mysql_readonly(self, sql: str, database: str = "") -> Dict[str, Any]:
        """
        Execute a strictly validated READ-ONLY SQL query (`SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN`)
        against MySQL with automatic row limit and timeout protection.
        """
        mysql_cfg = self.config.integrations.mysql
        if not mysql_cfg.enabled:
            return {"ok": False, "error": "MySQL integration is disabled in config.yaml"}

        is_valid, safe_sql, err_msg = validate_readonly_sql(sql, max_rows=mysql_cfg.max_rows)
        if not is_valid:
            return {"ok": False, "blocked_by_guardrail": True, "error": err_msg}

        db_name = (database or mysql_cfg.default_database).strip()
        if mysql_cfg.allowed_databases and db_name and db_name not in mysql_cfg.allowed_databases:
            return {
                "ok": False,
                "blocked_by_guardrail": True,
                "error": f"Database '{db_name}' is not in mysql.allowed_databases {mysql_cfg.allowed_databases}.",
            }

        password = os.environ.get(mysql_cfg.password_env, "")
        try:
            import pymysql
            import pymysql.cursors

            conn = pymysql.connect(
                host=mysql_cfg.host,
                port=mysql_cfg.port,
                user=mysql_cfg.user,
                password=password,
                database=db_name or None,
                connect_timeout=mysql_cfg.query_timeout_seconds,
                read_timeout=mysql_cfg.query_timeout_seconds,
                write_timeout=mysql_cfg.query_timeout_seconds,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY;")
                    cursor.execute(safe_sql)
                    rows = cursor.fetchall()
                    serialized_rows = json.loads(
                        redact_secrets(json.dumps(rows, default=str, ensure_ascii=False))
                    )
                    return {
                        "ok": True,
                        "database": db_name,
                        "executed_sql": safe_sql,
                        "row_count": len(serialized_rows),
                        "rows": serialized_rows[: mysql_cfg.max_rows],
                    }
            finally:
                conn.close()
        except Exception as exc:
            return {"ok": False, "database": db_name, "executed_sql": safe_sql, "error": redact_secrets(str(exc))}

    # -------------------------------------------------------------------------
    # 9. GATED ADMIN ACTIONS (Approval Queue + Execution on Admin Button Click)
    # -------------------------------------------------------------------------
    def propose_action(
        self,
        action_type: str,
        target: str,
        reason: str,
        requested_by_user_id: Optional[int] = None,
        requested_by_username: str = "",
    ) -> Dict[str, Any]:
        """
        Queue a state-changing action (`pm2_restart`, `pm2_reload`, `windows_service_restart`, `mysql_kill_query`)
        for Telegram Admin approval. Does NOT execute the action immediately.
        """
        if action_type not in self.ALLOWED_ACTION_TYPES:
            return {
                "ok": False,
                "error": f"Unsupported action_type '{action_type}'. Allowed: {self.ALLOWED_ACTION_TYPES}",
            }

        record = self.action_store.create_action(
            action_type=action_type,
            target=target,
            reason=reason,
            requested_by_user_id=requested_by_user_id,
            requested_by_username=requested_by_username,
        )
        return {
            "ok": True,
            "status": "awaiting_admin_approval",
            "message": (
                f"Action `{action_type}` on `{target}` has been queued (ID: `{record['action_id']}`). "
                "An interactive [Approve] / [Reject] button will be displayed in Telegram for an Admin to confirm."
            ),
            "action": record,
        }

    def execute_approved_action(self, action_id: str) -> Dict[str, Any]:
        """Execute a previously proposed action ONLY after an Admin clicks [Approve] in Telegram."""
        record = self.action_store.pop_action(action_id)
        if not record:
            return {"ok": False, "error": f"Action '{action_id}' not found or already expired/executed."}

        action_type = record["action_type"]
        target = str(record["target"]).strip()
        is_win = platform.system().lower().startswith("win")

        if action_type in ("pm2_restart", "pm2_reload"):
            subcmd = "restart" if action_type == "pm2_restart" else "reload"
            pm2_bin = self.config.integrations.pm2.pm2_bin
            res = _run_cmd([pm2_bin, subcmd, target], timeout=30, shell=is_win)
            return {
                "ok": res["ok"],
                "action": record,
                "output": res["stdout"] or res["stderr"],
            }

        if action_type == "windows_service_restart":
            if not is_win:
                return {"ok": False, "action": record, "error": "Windows service restart requires a Windows host."}
            res = _run_cmd(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Restart-Service -Name '{target}' -Force -ErrorAction Stop; Get-Service -Name '{target}' | Select-Object Status, Name | ConvertTo-Json",
                ],
                timeout=45,
            )
            return {"ok": res["ok"], "action": record, "output": res["stdout"] or res["stderr"]}

        if action_type == "mysql_kill_query":
            if not target.isdigit():
                return {"ok": False, "action": record, "error": "MySQL thread ID must be a numeric process ID."}
            mysql_cfg = self.config.integrations.mysql
            password = os.environ.get(mysql_cfg.password_env, "")
            try:
                import pymysql

                conn = pymysql.connect(
                    host=mysql_cfg.host,
                    port=mysql_cfg.port,
                    user=mysql_cfg.user,
                    password=password,
                    connect_timeout=5,
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"KILL QUERY {int(target)}")
                    return {"ok": True, "action": record, "output": f"Executed KILL QUERY {int(target)}."}
                finally:
                    conn.close()
            except Exception as exc:
                return {"ok": False, "action": record, "error": redact_secrets(str(exc))}

        return {"ok": False, "action": record, "error": f"Unhandled action_type: {action_type}"}


# -----------------------------------------------------------------------------
# CLI Entrypoint so `cli_subscription` (Cursor/Claude/Codex) and `opencode`
# can invoke the exact same toolkit via `python ops_toolkit.py <tool> ...`
# -----------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Windows Server Ops Diagnostic & Gated Action Toolkit CLI")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("system-resources", help="Get CPU, RAM, Disk, and top processes")
    sub.add_parser("services-status", help="Get health and status of all registered services")
    sub.add_parser("pm2-status", help="Get structured status of all PM2 apps")

    p_pm2_logs = sub.add_parser("pm2-logs", help="Get recent logs for a PM2 app")
    p_pm2_logs.add_argument("--app", required=True, help="PM2 app name")
    p_pm2_logs.add_argument("--lines", type=int, default=100)
    p_pm2_logs.add_argument("--type", default="all", choices=["all", "error", "out"])

    p_list_logs = sub.add_parser(
        "list-service-logs",
        help="List all log files and action log categories for a service (sizes & timestamps)",
    )
    p_list_logs.add_argument("--service", required=True, help="Service name in config.yaml")

    p_svc_logs = sub.add_parser(
        "service-logs",
        help="Read recent log lines across one or all action log files for a service",
    )
    p_svc_logs.add_argument("--service", required=True, help="Service name in config.yaml")
    p_svc_logs.add_argument(
        "--category",
        default="",
        help="Optional action category or filename substring (e.g. 'payment', 'webhook', 'auth'); empty reads all",
    )
    p_svc_logs.add_argument("--lines", type=int, default=100)
    p_svc_logs.add_argument("--filter", default="", help="Optional substring filter (e.g. ERROR, Traceback)")
    p_svc_logs.add_argument("--max-files", type=int, default=10)

    p_search_code = sub.add_parser(
        "search-code",
        help="Search (grep) across a service's source code files inside its working_dir",
    )
    p_search_code.add_argument("--service", required=True, help="Service name in config.yaml")
    p_search_code.add_argument("--query", required=True, help="Text or symbol to search for")
    p_search_code.add_argument("--glob", default="*", help="Optional filename pattern (e.g. '*.py', '*.js')")

    p_read_code = sub.add_parser(
        "read-code",
        help="Read source code lines from a file inside a service's working_dir",
    )
    p_read_code.add_argument("--service", required=True, help="Service name in config.yaml")
    p_read_code.add_argument("--file", required=True, help="Relative path inside working_dir")
    p_read_code.add_argument("--start-line", type=int, default=1)
    p_read_code.add_argument("--end-line", type=int, default=160)

    p_win_ev = sub.add_parser("windows-events", help="Get recent Windows Event Viewer logs")
    p_win_ev.add_argument("--log-name", default="Application")
    p_win_ev.add_argument("--source", default="")
    p_win_ev.add_argument("--max-events", type=int, default=25)

    sub.add_parser("ngrok-status", help="Inspect ngrok tunnels and recent HTTP requests")
    sub.add_parser("n8n-status", help="Inspect n8n healthz and logs")

    p_mysql = sub.add_parser("mysql-query", help="Run a strictly read-only SQL query (SELECT/SHOW/DESCRIBE/EXPLAIN)")
    p_mysql.add_argument("--sql", required=True, help="Read-only SQL statement")
    p_mysql.add_argument("--database", default="", help="Target database name")

    p_act = sub.add_parser("propose-action", help="Queue a gated restart/kill action for Admin Telegram approval")
    p_act.add_argument("--type", required=True, choices=list(OpsToolkit.ALLOWED_ACTION_TYPES))
    p_act.add_argument("--target", required=True, help="PM2 app name, Windows Service name, or MySQL query ID")
    p_act.add_argument("--reason", required=True, help="Brief explanation shown on the Telegram approval card")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    tk = OpsToolkit(cfg)

    if args.command == "system-resources":
        res = tk.get_system_resources()
    elif args.command == "services-status":
        res = tk.list_registered_services_status()
    elif args.command == "pm2-status":
        res = tk.get_pm2_status()
    elif args.command == "pm2-logs":
        res = tk.get_pm2_logs(args.app, lines=args.lines, log_type=args.type)
    elif args.command == "list-service-logs":
        res = tk.list_service_log_files(args.service)
    elif args.command == "service-logs":
        res = tk.get_service_logs(
            args.service,
            lines=args.lines,
            filter_keyword=args.filter,
            category_or_file=args.category,
            max_files=args.max_files,
        )
    elif args.command == "search-code":
        res = tk.search_service_code(args.service, query=args.query, file_pattern=args.glob)
    elif args.command == "read-code":
        res = tk.read_service_code_file(
            args.service,
            relative_path=args.file,
            start_line=args.start_line,
            end_line=args.end_line,
        )
    elif args.command == "windows-events":
        res = tk.get_windows_event_logs(
            log_name=args.log_name, source_filter=args.source, max_events=args.max_events
        )
    elif args.command == "ngrok-status":
        res = tk.check_ngrok_status()
    elif args.command == "n8n-status":
        res = tk.check_n8n_status()
    elif args.command == "mysql-query":
        res = tk.query_mysql_readonly(sql=args.sql, database=args.database)
    elif args.command == "propose-action":
        res = tk.propose_action(action_type=args.type, target=args.target, reason=args.reason)
    else:
        res = {"ok": False, "error": f"Unknown command {args.command}"}

    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
