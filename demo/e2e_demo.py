#!/usr/bin/env python3
"""
ProofGuard E2E Demo Runner

A small, dependency-light API scenario runner built for presentation/demo flows.

What it does:
- Calls your FastAPI API step by step.
- Reuses values returned by earlier calls using {{variables}}.
- Saves every request/response as JSON.
- Performs simple assertions.
- Pauses at screenshot checkpoints.
- Downloads /openapi.json and can list all API routes.
- Generates a single HTML report that is easy to screenshot.

It does NOT know your exact ProofGuard endpoint paths until you put them in the
scenario JSON. Use --list-routes first, then map the scenario to the real API.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("Missing dependency: requests. Install with: pip install requests", file=sys.stderr)
    raise

VAR_RE = re.compile(r"\{\{([A-Za-z0-9_.-]+)\}\}")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def resolve_var(name: str, context: dict[str, Any]) -> Any:
    cur: Any = context
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(f"Unknown variable '{{{{{name}}}}}'")
    return cur


def interpolate(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        matches = list(VAR_RE.finditer(value))
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return copy.deepcopy(resolve_var(matches[0].group(1), context))

        def repl(match: re.Match[str]) -> str:
            return str(resolve_var(match.group(1), context))

        return VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [interpolate(v, context) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, context) for k, v in value.items()}
    return value


def get_path(data: Any, dotted: str) -> Any:
    if dotted in ("", "$", None):
        return data
    path = dotted
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:]

    cur = data
    for raw_part in path.split("."):
        if raw_part == "":
            continue
        # Supports simple list indices such as items.0.id
        if isinstance(cur, list):
            cur = cur[int(raw_part)]
        elif isinstance(cur, dict):
            cur = cur[raw_part]
        else:
            raise KeyError(f"Cannot traverse '{dotted}' through non-container value")
    return cur


def make_serializable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return str(obj)


def run_assertions(assertions: list[dict[str, Any]], response_json: Any, context: dict[str, Any]) -> list[str]:
    results: list[str] = []
    for i, spec in enumerate(assertions, start=1):
        spec = interpolate(spec, context)
        actual = get_path(response_json, spec.get("path", "$"))
        ok = True
        description = spec.get("description") or f"assertion {i}"

        if "equals" in spec:
            ok = actual == spec["equals"]
            expected_text = f"== {spec['equals']!r}"
        elif "not_equals" in spec:
            ok = actual != spec["not_equals"]
            expected_text = f"!= {spec['not_equals']!r}"
        elif "contains" in spec:
            ok = spec["contains"] in actual
            expected_text = f"contains {spec['contains']!r}"
        elif "in" in spec:
            ok = actual in spec["in"]
            expected_text = f"in {spec['in']!r}"
        elif "len" in spec:
            ok = len(actual) == int(spec["len"])
            expected_text = f"len == {spec['len']}"
        elif "gte" in spec:
            ok = actual >= spec["gte"]
            expected_text = f">= {spec['gte']!r}"
        elif "lte" in spec:
            ok = actual <= spec["lte"]
            expected_text = f"<= {spec['lte']!r}"
        elif "truthy" in spec:
            ok = bool(actual) is bool(spec["truthy"])
            expected_text = f"truthy == {bool(spec['truthy'])}"
        else:
            raise ValueError(f"Unsupported assertion: {spec}")

        line = f"{'PASS' if ok else 'FAIL'}: {description} | actual={actual!r} | expected {expected_text}"
        results.append(line)
        if not ok:
            raise AssertionError(line)
    return results


def html_json(obj: Any) -> str:
    text = json.dumps(make_serializable(obj), indent=2, ensure_ascii=False)
    return f"<pre>{html.escape(text)}</pre>"


def build_html_report(results: list[dict[str, Any]], context: dict[str, Any], out_file: Path, title: str) -> None:
    cards = []
    for r in results:
        status = r["status"]
        cls = "pass" if status == "PASS" else "fail"
        response_block = html_json(r.get("response_json")) if r.get("response_json") is not None else f"<pre>{html.escape(r.get('response_text',''))}</pre>"
        assertion_html = ""
        if r.get("assertions"):
            assertion_html = "<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in r["assertions"]) + "</ul>"

        checkpoint = ""
        if r.get("checkpoint"):
            checkpoint = f"<div class='checkpoint'>SCREENSHOT CHECKPOINT: {html.escape(r['checkpoint'])}</div>"

        request_body = ""
        if r.get("request_json") is not None:
            request_body = "<details><summary>Request JSON</summary>" + html_json(r["request_json"]) + "</details>"

        cards.append(f"""
        <section class="card {cls}">
          <div class="heading">
            <h2>{r['index']:02d}. {html.escape(r['id'])}</h2>
            <span class="badge">{status}</span>
          </div>
          <p><b>{html.escape(r['method'])}</b> {html.escape(r['url'])}</p>
          <p>HTTP: {r.get('status_code', 'n/a')} | {r.get('duration_ms', 0):.1f} ms</p>
          {checkpoint}
          {request_body}
          <details open><summary>Response</summary>{response_block}</details>
          {assertion_html}
        </section>
        """)

    ctx = html_json(context)
    doc = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 30px; background:#f5f6f8; color:#1f2328; }}
h1 {{ margin-bottom: 4px; }}
.meta {{ color:#5f6368; margin-bottom:24px; }}
.card {{ background:white; border-radius:12px; padding:18px; margin:16px 0; box-shadow:0 1px 4px rgba(0,0,0,.10); border-left:6px solid #999; }}
.card.pass {{ border-left-color:#2e7d32; }}
.card.fail {{ border-left-color:#c62828; }}
.heading {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }}
.badge {{ font-weight:700; padding:5px 10px; border-radius:999px; background:#eceff1; }}
.checkpoint {{ margin:12px 0; padding:10px 12px; background:#fff8e1; border:1px solid #f2c94c; border-radius:8px; font-weight:700; }}
pre {{ white-space:pre-wrap; word-break:break-word; background:#0d1117; color:#e6edf3; padding:14px; border-radius:8px; max-height:520px; overflow:auto; }}
details {{ margin-top:10px; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="meta">Generated {html.escape(utc_now())}</div>
{''.join(cards)}
<section class="card">
<h2>Final extracted context</h2>
{ctx}
</section>
</body>
</html>"""
    out_file.write_text(doc, encoding="utf-8")


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_openapi(base_url: str, outdir: Path) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/openapi.json"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    (outdir / "openapi.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def list_routes(spec: dict[str, Any]) -> None:
    rows = []
    for path, methods in spec.get("paths", {}).items():
        for method, details in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            rows.append((
                method.upper(),
                path,
                details.get("operationId", ""),
                details.get("summary", ""),
            ))
    width = max([len(x[1]) for x in rows], default=10)
    for method, path, opid, summary in sorted(rows, key=lambda x: (x[1], x[0])):
        print(f"{method:6} {path:<{width}}  {opid}  {summary}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=Path("demo_scenario.json"))
    p.add_argument("--base-url", default=None)
    p.add_argument("--outdir", type=Path, default=Path("demo_artifacts"))
    p.add_argument("--no-pause", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--list-routes", action="store_true")
    args = p.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    cfg = load_config(args.config)
    base_url = (args.base_url or cfg.get("base_url") or "http://127.0.0.1:8000").rstrip("/")

    spec = fetch_openapi(base_url, args.outdir)
    if args.list_routes:
        list_routes(spec)
        return 0

    context: dict[str, Any] = {
        "base_url": base_url,
        "target_path": str((args.config.parent / cfg.get("target_path", "")).resolve()) if cfg.get("target_path") else "",
        **cfg.get("variables", {}),
    }

    results: list[dict[str, Any]] = []
    session = requests.Session()
    timeout = cfg.get("timeout_seconds", 120)

    for idx, raw_step in enumerate(cfg.get("steps", []), start=1):
        if raw_step.get("enabled", True) is False:
            continue

        step = interpolate(raw_step, context)
        sid = step["id"]
        method = step.get("method", "GET").upper()
        path = step["path"]
        url = base_url + path
        req_json = step.get("json")
        params = step.get("params")
        headers = step.get("headers")
        checkpoint = step.get("checkpoint")

        print("\n" + "=" * 90)
        print(f"[{idx:02d}] {sid}")
        print(f"{method} {url}")
        if checkpoint:
            print(f"SCREENSHOT CHECKPOINT: {checkpoint}")
        print("=" * 90)

        if args.dry_run:
            print("DRY RUN")
            if req_json is not None:
                print(json.dumps(req_json, indent=2, ensure_ascii=False))
            continue

        t0 = time.perf_counter()
        try:
            r = session.request(
                method,
                url,
                json=req_json,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            elapsed = (time.perf_counter() - t0) * 1000.0

            try:
                response_json = r.json()
            except ValueError:
                response_json = None

            result = {
                "index": idx,
                "id": sid,
                "status": "PASS" if r.ok else "FAIL",
                "method": method,
                "url": url,
                "status_code": r.status_code,
                "duration_ms": elapsed,
                "request_json": req_json,
                "response_json": response_json,
                "response_text": r.text if response_json is None else "",
                "checkpoint": checkpoint,
                "assertions": [],
            }

            step_file = args.outdir / f"{idx:02d}_{sid}.json"
            step_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

            if not r.ok and not step.get("allow_http_error", False):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")

            if response_json is not None:
                for name, dotted in step.get("extract", {}).items():
                    context[name] = get_path(response_json, dotted)
                    print(f"EXTRACT {name} = {context[name]!r}")

                assertions = run_assertions(step.get("assert", []), response_json, context)
                result["assertions"] = assertions
                for line in assertions:
                    print(line)

            results.append(result)
            build_html_report(results, context, args.outdir / "demo_report.html", cfg.get("title", "ProofGuard E2E Demo"))

            if checkpoint and not args.no_pause:
                print("\nTake the screenshot now.")
                input("Press ENTER to continue... ")

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            fail = {
                "index": idx,
                "id": sid,
                "status": "FAIL",
                "method": method,
                "url": url,
                "status_code": getattr(locals().get("r", None), "status_code", None),
                "duration_ms": elapsed,
                "request_json": req_json,
                "response_json": None,
                "response_text": str(exc),
                "checkpoint": checkpoint,
                "assertions": [],
            }
            results.append(fail)
            build_html_report(results, context, args.outdir / "demo_report.html", cfg.get("title", "ProofGuard E2E Demo"))
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

    summary = {
        "generated_at": utc_now(),
        "title": cfg.get("title", "ProofGuard E2E Demo"),
        "base_url": base_url,
        "steps_passed": sum(1 for r in results if r["status"] == "PASS"),
        "steps_failed": sum(1 for r in results if r["status"] != "PASS"),
        "context": context,
    }
    (args.outdir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    build_html_report(results, context, args.outdir / "demo_report.html", cfg.get("title", "ProofGuard E2E Demo"))

    print("\nDemo complete.")
    print(f"Artifacts: {args.outdir.resolve()}")
    print(f"HTML report: {(args.outdir / 'demo_report.html').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
