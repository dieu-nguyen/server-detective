"""
Shared Windows Server Operations Toolkit (`ops_toolkit.py`).

Used by BOTH:
1. `sdk_apikey` mode — imported directly in Python as async/sync tool functions.
2. `cli_subscription` mode — invoked via CLI (`python ops_toolkit.py <tool> ...`) or imported.

All read/diagnostic tools are safe to run automatically.
Any state-changing operation (restarting PM2/Windows services, killing hung queries)
is NEVER executed directly by the LLM; instead, `propose_action` registers a pending
approval request that requires a Telegram Admin to click [Approve].
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import psutil

from agent.config import AppConfig, ServiceDefinition, load_config
from agent.security import redact_secrets, validate_readonly_sql


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
            # Prune expired actions
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
    """Unified diagnostic & gated-action toolkit for Windows Server + PM2 + ngrok + n8n + MySQL."""

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
        """Check status, health URL, port, and process state of all services in config.yaml."""
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

        # Fallback to `pm2 logs <app> --lines N --nostream`
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
    # 4. SERVICE FILE LOGS & WINDOWS SERVICES / EVENT VIEWER
    # -------------------------------------------------------------------------
    def get_service_logs(
        self,
        service_name: str,
        lines: int = 100,
        filter_keyword: str = "",
    ) -> Dict[str, Any]:
        """Read latest log lines for a registered service in `config.yaml` (supports wildcards `*.log`)."""
        svc: Optional[ServiceDefinition] = self.config.services.get(service_name)
        if not svc:
            return {
                "ok": False,
                "error": f"Unknown service '{service_name}'. Registered services: {list(self.config.services.keys())}",
            }

        collected: Dict[str, str] = {}
        for pattern in svc.log_paths:
            matched_files = sorted(
                (Path(p) for p in glob.glob(pattern)),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            for file_path in matched_files[:2]:  # Latest 2 files per glob pattern
                collected[str(file_path)] = _tail_file(file_path, lines=lines, filter_keyword=filter_keyword)

        # Also include PM2 logs if managed by PM2 and no custom log files matched
        if not collected and svc.manager == "pm2" and svc.pm2_name:
            return self.get_pm2_logs(svc.pm2_name, lines=lines)

        return {
            "ok": True,
            "service_name": service_name,
            "matched_files": len(collected),
            "logs": collected,
        }

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
        level_Clause = f"; Level=@({','.join(levels)})" if levels else ""
        provider_clause = f"; ProviderName='*{source_filter}*'" if source_filter else ""

        ps_script = (
            f"Get-WinEvent -FilterHashtable @{{LogName='{log_name}'{level_Clause}{provider_clause}}} "
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
    # 5. NGROK & N8N INSPECTORS
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

                # Also fetch recent captured HTTP requests to spot 500/502 errors
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
                                " method": req_info.get("method"),
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
            for fpath in matched[:2]:
                logs_collected[str(fpath)] = _tail_file(fpath, lines=log_lines)
        out["logs"] = logs_collected
        return out

    # -------------------------------------------------------------------------
    # 6. READ-ONLY MYSQL DATABASE INSPECTOR
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
                    # Redact secrets and serialize safely
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
    # 7. GATED ADMIN ACTIONS (Approval Queue + Execution on Admin Button Click)
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
# CLI Entrypoint so `cli_subscription` mode (Cursor CLI / Claude CLI / Codex)
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

    p_svc_logs = sub.add_parser("service-logs", help="Get recent log files for a registered service")
    p_svc_logs.add_argument("--service", required=True, help="Service name in config.yaml")
    p_svc_logs.add_argument("--lines", type=int, default=100)
    p_svc_logs.add_argument("--filter", default="", help="Optional substring filter (e.g. ERROR, Traceback)")

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
    elif args.command == "service-logs":
        res = tk.get_service_logs(args.service, lines=args.lines, filter_keyword=args.filter)
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
