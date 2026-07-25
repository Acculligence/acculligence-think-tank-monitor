#!/usr/bin/env python3
"""Acculligence Think Tank Monitor v4 controller.

Production safety properties:
- Each source runs in its own child process.
- A hard timeout kills a stuck source without stopping the run.
- Several sources run concurrently.
- Every completed source writes an atomic checkpoint.
- Reruns resume from existing checkpoints.
- Final CSV files are built from checkpoints, so partial progress is preserved.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ARTICLE_FIELDS = [
    'source','domain','title','published_at','url','matched_keywords','body',
    'collection_route','review_status','saudi_summary_ar','topic','sentiment','author'
]
AUDIT_FIELDS = [
    'source','domain','status','routes_used','candidate_urls','dated_items','feed_items',
    'matched_items','duration_seconds','notes'
]


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def safe_key(source: dict) -> str:
    raw = str(source.get('source_id') or source.get('domain') or source.get('source_name'))
    return ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in raw)[:120]


def run_source(index: int, total: int, source: dict, args, checkpoint_dir: Path) -> dict:
    key = safe_key(source)
    checkpoint = checkpoint_dir / f'{key}.json'
    if checkpoint.exists() and not args.force:
        try:
            data = json.loads(checkpoint.read_text(encoding='utf-8'))
            print(f'[{index}/{total}] RESUME {source["source_name"]}: {data["audit"]["status"]}', flush=True)
            return data
        except Exception:
            checkpoint.unlink(missing_ok=True)

    if source.get('status') != 'validated':
        payload = {
            'articles': [],
            'audit': {
                'source': source.get('source_name',''),
                'domain': source.get('domain',''),
                'status': 'blocked',
                'routes_used': '',
                'candidate_urls': 0,
                'dated_items': 0,
                'feed_items': 0,
                'matched_items': 0,
                'duration_seconds': 0,
                'notes': 'No validated official route.'
            }
        }
        atomic_json(checkpoint, payload)
        print(f'[{index}/{total}] BLOCKED {source["source_name"]}', flush=True)
        return payload

    print(f'[{index}/{total}] START {source["source_name"]}', flush=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='acculligence-source-') as td:
        source_file = Path(td) / 'source.json'
        result_file = Path(td) / 'result.json'
        source_file.write_text(json.dumps(source, ensure_ascii=False), encoding='utf-8')
        cmd = [
            sys.executable, 'source_worker.py',
            '--source', str(source_file),
            '--keywords', args.keywords,
            '--start', args.start,
            '--end', args.end,
            '--result', str(result_file),
            '--request-timeout', str(args.request_timeout),
            '--max-candidates', str(args.max_candidates),
            '--article-workers', str(args.article_workers),
        ]
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=args.source_timeout,
                check=False,
            )
            if result_file.exists():
                payload = json.loads(result_file.read_text(encoding='utf-8'))
            else:
                message = (completed.stderr or completed.stdout or f'Worker exit code {completed.returncode}').strip()
                payload = {'articles': [], 'audit': {
                    'source': source['source_name'], 'domain': source['domain'],
                    'status': 'worker_error', 'routes_used': '', 'candidate_urls': 0,
                    'dated_items': 0, 'feed_items': 0, 'matched_items': 0,
                    'duration_seconds': round(time.monotonic()-started, 2),
                    'notes': message[-1200:],
                }}
        except subprocess.TimeoutExpired:
            payload = {'articles': [], 'audit': {
                'source': source['source_name'], 'domain': source['domain'],
                'status': 'source_timeout', 'routes_used': '', 'candidate_urls': 0,
                'dated_items': 0, 'feed_items': 0, 'matched_items': 0,
                'duration_seconds': round(time.monotonic()-started, 2),
                'notes': f'Hard source timeout after {args.source_timeout} seconds; source skipped safely.',
            }}
        except Exception as exc:
            payload = {'articles': [], 'audit': {
                'source': source['source_name'], 'domain': source['domain'],
                'status': 'controller_error', 'routes_used': '', 'candidate_urls': 0,
                'dated_items': 0, 'feed_items': 0, 'matched_items': 0,
                'duration_seconds': round(time.monotonic()-started, 2),
                'notes': repr(exc),
            }}

    payload['audit']['duration_seconds'] = round(time.monotonic()-started, 2)
    atomic_json(checkpoint, payload)
    print(
        f'[{index}/{total}] DONE {source["source_name"]}: '
        f'{payload["audit"]["status"]}, {payload["audit"]["matched_items"]} matches, '
        f'{payload["audit"]["duration_seconds"]}s',
        flush=True,
    )
    return payload


def deduplicate_articles(rows: list[dict]) -> list[dict]:
    seen = set(); unique = []
    for row in rows:
        key = (row.get('url','').rstrip('/').lower(), row.get('title','').strip().lower())
        if key in seen: continue
        seen.add(key); unique.append(row)
    return unique


def write_outputs(payloads: list[dict], output_dir: Path, start: str, end: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    articles = deduplicate_articles([a for p in payloads for a in p.get('articles',[])])
    audits = [p['audit'] for p in payloads]
    articles.sort(key=lambda x: (x.get('published_at',''), x.get('source',''), x.get('title','')))
    audits.sort(key=lambda x: x.get('source','').lower())

    article_path = output_dir / f'articles_{start}_{end}.csv'
    audit_path = output_dir / f'audit_{start}_{end}.csv'
    with article_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=ARTICLE_FIELDS, extrasaction='ignore')
        writer.writeheader(); writer.writerows(articles)
    with audit_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=AUDIT_FIELDS, extrasaction='ignore')
        writer.writeheader(); writer.writerows(audits)
    return article_path, audit_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--map', default='config/source_registry.json')
    ap.add_argument('--keywords', default='config/keywords.json')
    ap.add_argument('--output', default='output')
    ap.add_argument('--source-workers', type=int, default=8)
    ap.add_argument('--article-workers', type=int, default=6)
    ap.add_argument('--source-timeout', type=int, default=75)
    ap.add_argument('--request-timeout', type=int, default=12)
    ap.add_argument('--max-candidates', type=int, default=240)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    sources = json.loads(Path(args.map).read_text(encoding='utf-8'))['sources']
    output_dir = Path(args.output)
    checkpoint_dir = output_dir / 'checkpoints' / f'{args.start}_{args.end}'
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    payloads = []
    with ThreadPoolExecutor(max_workers=max(1, args.source_workers)) as pool:
        futures = {
            pool.submit(run_source, i, len(sources), source, args, checkpoint_dir): source
            for i, source in enumerate(sources, 1)
        }
        for future in as_completed(futures):
            try:
                payloads.append(future.result())
            except Exception as exc:
                source = futures[future]
                payloads.append({'articles': [], 'audit': {
                    'source': source.get('source_name',''), 'domain': source.get('domain',''),
                    'status': 'unexpected_error', 'routes_used': '', 'candidate_urls': 0,
                    'dated_items': 0, 'feed_items': 0, 'matched_items': 0, 'duration_seconds': 0,
                    'notes': repr(exc),
                }})

    article_path, audit_path = write_outputs(payloads, output_dir, args.start, args.end)
    status_counts = {}
    for p in payloads:
        status = p['audit']['status']
        status_counts[status] = status_counts.get(status, 0) + 1
    summary = {
        'sources': len(sources),
        'articles': sum(len(p.get('articles',[])) for p in payloads),
        'statuses': status_counts,
        'articles_file': str(article_path),
        'audit_file': str(audit_path),
        'checkpoint_directory': str(checkpoint_dir),
    }
    (output_dir / f'summary_{args.start}_{args.end}.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
