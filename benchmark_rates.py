#!/usr/bin/env python3
"""Benchmark free models and write rate-scores.json for the dashboard."""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

OPENROUTER_API = 'https://openrouter.ai/api/v1/models'
KILO_GATEWAY_API = 'https://cors-proxy.fadlicode-mail.workers.dev/kilo/models'

# Keys (optional). If missing, we still benchmark what we can.
OPENROUTER_KEY = os.environ.get('OPENROUTER_API_KEY', '')
KILO_KEY = os.environ.get('KILO_API_KEY', '')


def fetch_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f'[warn] fetch failed {url}: {e}', flush=True)
        return None


def is_free(m):
    p = m.get('pricing') or {}
    return (p.get('prompt') in ('0', '0.000000000000', 0, 0.0) and
            p.get('completion') in ('0', '0.000000000000', 0, 0.0))


def pick_models(or_data, kg_data, max_per_gateway=8):
    or_free = [m for m in (or_data or {}).get('data', []) if is_free(m)]
    kg_free = [m for m in (kg_data or {}).get('data', []) if is_free(m)]

    # simple heuristic: prefer known stable providers, then by context length desc
    def sort_key(m):
        ctx = m.get('context_length') or 0
        pid = (m.get('id') or '').lower()
        score = 0
        if any(k in pid for k in ['google', 'gemma', 'mistral', 'meta', 'nvidia', 'nemotron']):
            score += 1000
        if any(k in pid for k in ['openrouter/free', 'kilo-auto', 'nex-agi']):
            score += 500
        return (score, ctx)

    or_pick = sorted(or_free, key=sort_key, reverse=True)[:max_per_gateway]
    kg_pick = sorted(kg_free, key=sort_key, reverse=True)[:max_per_gateway]

    # de-dup by id
    seen = set()
    out = []
    for m in or_pick + kg_pick:
        mid = m.get('id')
        if mid and mid not in seen:
            seen.add(mid)
            out.append(m)
    return out


def chat_completion(base_url, model, key, prompt='Say hi in 5 words or less.', timeout=60):
    url = base_url.rstrip('/') + '/chat/completions'
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 32,
        'temperature': 0,
    }).encode('utf-8')
    if key:
        headers['Authorization'] = f'Bearer {key}'

    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8'))
            return True, data
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode('utf-8'))
        except Exception:
            payload = {}
        code = payload.get('error', {}).get('code') or e.code
        msg = payload.get('error', {}).get('message') or str(e)
        if code == 429 or 'rate limit' in msg.lower():
            return False, {'code': 429, 'message': msg}
        return False, {'code': code, 'message': msg}
    except Exception as e:
        return False, {'code': 'network', 'message': str(e)}


def benchmark_model(model_id, gateway, base_url, key, hits=3):
    success = 0
    detail = []
    for i in range(hits):
        ok, res = chat_completion(base_url, model_id, key)
        if ok:
            success += 1
            detail.append('ok')
        else:
            detail.append(f"{res.get('code')}:{res.get('message', '')[:40]}")
        time.sleep(1.0)  # be polite
    score = max(1, round((success / hits) * 10))
    return {
        'model_id': model_id,
        'gateway': gateway,
        'score': score,
        'success': success,
        'hits': hits,
        'detail': detail,
        'ts': datetime.now(timezone.utc).isoformat(),
    }


def main():
    print('[step] fetch model lists', flush=True)
    or_data = fetch_json(OPENROUTER_API) or {'data': []}
    kg_data = fetch_json(KILO_GATEWAY_API) or {'data': []}
    candidates = pick_models(or_data, kg_data, max_per_gateway=6)
    print(f'[step] candidates={len(candidates)}', flush=True)

    results = []
    for m in candidates:
        mid = m.get('id')
        if not mid:
            continue
        in_or = is_free(m) and any(x.get('id') == mid for x in or_data.get('data', []))
        in_kg = is_free(m) and any(x.get('id') == mid for x in kg_data.get('data', []))
        gateway = 'openrouter' if in_or else ('kilo' if in_kg else 'openrouter')
        base = 'https://openrouter.ai/api/v1' if gateway == 'openrouter' else 'https://cors-proxy.fadlicode-mail.workers.dev/kilo'
        key = OPENROUTER_KEY if gateway == 'openrouter' else KILO_KEY

        print(f'[bench] {mid} via {gateway}', flush=True)
        res = benchmark_model(mid, gateway, base, key, hits=3)
        results.append(res)
        print(f'       -> {res["success"]}/{res["hits"]} score={res["score"]}', flush=True)

    out = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'results': results,
    }
    out_path = os.path.join(os.path.dirname(__file__), 'rate-scores.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)
    print(f'[done] wrote {out_path} ({len(results)} entries)', flush=True)


if __name__ == '__main__':
    main()
