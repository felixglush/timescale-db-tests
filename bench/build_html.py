#!/usr/bin/env python3
"""Build docs/report.html (a visual version of EXPERIMENT_LOG.md) from results/*.csv.

  python3 bench/build_html.py [results_dir] [--fragment out.html]

--fragment also writes the page body without the <html>/<head> wrapper (used
when publishing the page as a claude.ai artifact).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Compressed bytes per row by column, one chunk at 10k clients (EXPERIMENT_LOG.md,
# "Storage"); measured with pg_column_size() on the compressed chunk tables.
COL_BYTES = {
    "cmp_7d":  {"txn_id": 3.9, "txn_ts": 10.2, "account_id": 2.4, "vendor_id": 3.2, "txn_type": 3.1, "amount": 8.9, "description": 43.6},
    "cmp_30d": {"txn_id": 2.6, "txn_ts": 8.8, "account_id": 0.9, "vendor_id": 1.9, "txn_type": 0.7, "amount": 6.3, "description": 19.5},
    "cmp_90d": {"txn_id": 2.3, "txn_ts": 8.6, "account_id": 0.6, "vendor_id": 1.7, "txn_type": 0.4, "amount": 5.2, "description": 15.4},
}


def read(res, name):
    with open(res / name) as f:
        return list(csv.DictReader(f))


def main():
    args = sys.argv[1:]
    fragment_out = None
    if "--fragment" in args:
        i = args.index("--fragment")
        fragment_out = Path(args[i + 1])
        del args[i:i + 2]
    res = Path(args[0]) if args else ROOT / "results"

    meta = json.loads((res / "run_meta.json").read_text())
    lat = {}
    for r in read(res, "latency_summary.csv"):
        lat[f"{r['clients']}|{r['variant']}|{r['query']}|{r['mode']}"] = {
            "avg_rows": int(r["avg_rows"]),
            "exec_p50_ms": float(r["exec_p50_ms"]), "exec_p95_ms": float(r["exec_p95_ms"]),
            "avg_shared_read": int(r["avg_shared_read"]), "avg_shared_hit": int(r["avg_shared_hit"]),
            "e2e_p50_ms": float(r["e2e_p50_ms"]) if r["e2e_p50_ms"] else None,
        }
    sizes = {f"{r['clients']}|{r['variant']}": {
        "table": int(r["table_bytes"]), "toast": int(r["toast_bytes"]),
        "index": int(r["index_bytes"]), "total": int(r["total_bytes"])} for r in read(res, "sizes.csv")}
    rows = {int(b["clients"]): int(b["rows"]) for b in read(res, "build.csv")}
    scales = sorted(rows)
    data = {
        "scales": scales, "rows": rows, "rowsTop": rows[scales[-1]],
        "pg": meta["postgres"].split()[1], "ts": meta["timescaledb"], "started": meta["started"],
        "lat": lat, "sizes": sizes, "colBytes": COL_BYTES,
    }
    body = (ROOT / "bench" / "report_template.html").read_text()
    body = body.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))

    out = ROOT / "docs" / "report.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                   '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                   '<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n'
                   '</head>\n<body>\n' + body + '\n</body>\n</html>\n')
    print(f"wrote {out}")
    if fragment_out:
        fragment_out.write_text(body)
        print(f"wrote {fragment_out}")


if __name__ == "__main__":
    main()
