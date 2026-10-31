"""
Main Entrypoint for the Windows Server Multi-Service Telegram Debug Agent.

Usage:
    python main.py --config config.yaml
    python main.py --config config.yaml --check
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agent.config import load_config
from agent.telegram_bot import TelegramOpsBot
from ops_toolkit import OpsToolkit


def run_preflight_check(config_path: str) -> int:
    """Validate configuration, registered services, and local system resource inspection."""
    cfg = load_config(config_path)
    tk = OpsToolkit(cfg)
    sys_info = tk.get_system_resources()
    summary = {
        "ok": True,
        "server_name": cfg.server_name,
        "active_engine_mode": cfg.engine.mode,
        "cli_subscription_provider": cfg.engine.cli_subscription.provider,
        "sdk_apikey_harness": cfg.engine.sdk_apikey.harness,
        "sdk_apikey_provider": f"{cfg.engine.sdk_apikey.provider}:{cfg.engine.sdk_apikey.model}",
        "cli_restrictions_enabled": cfg.engine.cli_restrictions.enabled,
        "session_lifecycle": {
            "enabled": cfg.engine.session.enabled,
            "state_db_file": cfg.engine.session.state_db_file,
            "idle_timeout_minutes": cfg.engine.session.idle_timeout_minutes,
            "max_turns_per_session": cfg.engine.session.max_turns_per_session,
        },
        "registered_services": {
            name: {
                "working_dir": svc.working_dir,
                "logs_dir": svc.logs_dir,
                "log_categories": list(svc.log_files.keys()),
            }
            for name, svc in cfg.services.items()
        },
        "admins_count": len(cfg.telegram.admins),
        "viewers_count": len(cfg.telegram.viewers),
        "system_snapshot": {
            "os": sys_info["os"],
            "cpu_percent": sys_info["cpu_percent"],
            "memory_percent": sys_info["memory"]["percent"],
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Server Multi-Service Telegram Debug Agent")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run configuration and local toolkit preflight check without connecting to Telegram",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.check:
        return run_preflight_check(args.config)

    cfg = load_config(args.config)
    bot = TelegramOpsBot(cfg)
    app = bot.build_application()
    logging.info(
        "Starting Telegram Server Debug Agent for '%s' in mode='%s' (CLI='%s', SDK='%s/%s')...",
        cfg.server_name,
        cfg.engine.mode,
        cfg.engine.cli_subscription.provider,
        cfg.engine.sdk_apikey.harness,
        cfg.engine.sdk_apikey.provider,
    )
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
