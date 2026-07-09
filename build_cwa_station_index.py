import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import radar_live_server as server


ROOT = Path(__file__).resolve().parent
OUT_FILE = ROOT / "cwa_station_index.json"
PARTIAL_FILE = ROOT / "cwa_station_index.partial.json"


def load_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("stations", [])
    return rows if isinstance(rows, list) else []


def save_rows(path: Path, rows: List[Dict[str, Any]], pages_done: List[int], errors: List[Dict[str, Any]]) -> None:
    payload = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "pages_done": sorted(set(pages_done)),
        "stations": rows,
        "errors": errors[-80:],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge(existing: List[Dict[str, Any]], observed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return server.merge_station_index(existing, observed)


def fetch_page(page: int, page_limit: int, timeout: int) -> List[Dict[str, Any]]:
    data = server.fetch_cwa_json(
        server.CWA_OBS_URL,
        {"limit": page_limit, "offset": page * page_limit},
        timeout=timeout,
    )
    return server.normalize_cwa_observations(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build local CWA auto-station index from O-A0001-001 pages.")
    parser.add_argument("--page-limit", type=int, default=int(os.environ.get("CWA_BUILD_PAGE_LIMIT", "50")))
    parser.add_argument("--max-pages", type=int, default=int(os.environ.get("CWA_BUILD_MAX_PAGES", "80")))
    parser.add_argument("--sleep", type=float, default=float(os.environ.get("CWA_BUILD_SLEEP", "0.7")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("CWA_BUILD_TIMEOUT", "25")))
    parser.add_argument("--stop-empty", type=int, default=int(os.environ.get("CWA_BUILD_STOP_EMPTY", "3")))
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()

    if not server.CWA_API_KEY:
        print("CWA_API_KEY is required", file=sys.stderr)
        return 2

    rows = [] if args.fresh else load_rows(PARTIAL_FILE) or load_rows(OUT_FILE)
    pages_done = []
    errors: List[Dict[str, Any]] = []
    empty_pages = 0

    for page in range(args.max_pages):
        started = time.time()
        try:
            observed = fetch_page(page, args.page_limit, args.timeout)
            rows = merge(rows, observed)
            pages_done.append(page)
            empty_pages = empty_pages + 1 if not observed else 0
            print(
                f"page={page:03d} observed={len(observed):03d} "
                f"index={len(rows):04d} seconds={time.time() - started:.1f}",
                flush=True,
            )
            save_rows(PARTIAL_FILE, rows, pages_done, errors)
            if empty_pages >= args.stop_empty:
                print(f"stop: {empty_pages} consecutive empty pages", flush=True)
                break
        except Exception as exc:
            error = {"page": page, "error": f"{type(exc).__name__}: {exc}"}
            errors.append(error)
            print(f"page={page:03d} ERROR {error['error']}", flush=True)
            save_rows(PARTIAL_FILE, rows, pages_done, errors)
        time.sleep(args.sleep)

    save_rows(OUT_FILE, rows, pages_done, errors)
    print(f"written {OUT_FILE} stations={len(rows)} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
