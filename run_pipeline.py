"""
Orchestrates the full weekly pipeline:
  1. Ingest: fetch all RSS sources → DuckDB raw_articles
  2. Transform: dbt run (stg_articles + fct_weekly_digest)
  3. Deliver: build and send Buttondown newsletter

Each step is independently re-runnable. Failures are logged but only fatal
if explicitly non-recoverable. A JSON run log is written to logs/.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
DBT_DIR = PROJECT_ROOT / "dbt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("run_pipeline")


def step_ingest() -> dict:
    logger.info("=== STEP 1: Ingest ===")
    sys.path.insert(0, str(PROJECT_ROOT))
    from ingest.fetch_rss import run as fetch_run
    return fetch_run()


def step_transform() -> dict:
    logger.info("=== STEP 2: Transform (dbt run) ===")
    db_path_raw = os.getenv("DB_PATH", "data/warehouse.duckdb")
    db_path = str((PROJECT_ROOT / db_path_raw).resolve())
    env = {**os.environ, "DB_PATH": db_path}

    # Resolve dbt executable relative to this script's venv
    venv_scripts = PROJECT_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    dbt_exe = venv_scripts / ("dbt.exe" if os.name == "nt" else "dbt")
    dbt_cmd = str(dbt_exe) if dbt_exe.exists() else "dbt"

    result = subprocess.run(
        [dbt_cmd, "run", "--profiles-dir", str(DBT_DIR)],
        cwd=str(DBT_DIR),
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        logger.error(f"dbt run failed:\n{result.stdout}\n{result.stderr}")
        raise RuntimeError("dbt run failed")
    logger.info(result.stdout)
    return {"status": "ok"}


def step_deliver() -> dict:
    logger.info("=== STEP 3: Deliver ===")
    from deliver.send_newsletter import run as deliver_run
    return deliver_run()


def write_run_log(log: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = LOGS_DIR / f"run_{ts}.json"
    log_file.write_text(json.dumps(log, indent=2, default=str))
    logger.info(f"Run log written to {log_file}")


def main(skip_deliver: bool = False) -> None:
    run_log = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    try:
        run_log["steps"]["ingest"] = step_ingest()
    except Exception as e:
        logger.error(f"Ingest failed: {e}")
        run_log["steps"]["ingest"] = {"status": "error", "error": str(e)}
        run_log["outcome"] = "failed_at_ingest"
        write_run_log(run_log)
        sys.exit(1)

    try:
        run_log["steps"]["transform"] = step_transform()
    except Exception as e:
        logger.error(f"Transform failed: {e}")
        run_log["steps"]["transform"] = {"status": "error", "error": str(e)}
        run_log["outcome"] = "failed_at_transform"
        write_run_log(run_log)
        sys.exit(1)

    if not skip_deliver:
        try:
            run_log["steps"]["deliver"] = step_deliver()
        except Exception as e:
            logger.error(f"Deliver failed: {e}")
            run_log["steps"]["deliver"] = {"status": "error", "error": str(e)}
            run_log["outcome"] = "failed_at_deliver"
            write_run_log(run_log)
            sys.exit(1)
    else:
        run_log["steps"]["deliver"] = {"status": "skipped"}

    run_log["outcome"] = "success"
    run_log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_run_log(run_log)
    logger.info("Pipeline completed successfully.")


if __name__ == "__main__":
    skip = "--skip-deliver" in sys.argv
    main(skip_deliver=skip)
