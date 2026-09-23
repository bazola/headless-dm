#!/usr/bin/env python3
"""
03-router.py — Ollama-impersonating LLM router with failover.

Sits on the realm's own host at port 11434 (Ollama's default) so mod-ollama-chat needs no
special endpoint config. Translates Ollama API calls (/api/generate,
/api/chat) into OpenAI /v1/chat/completions against llama-server or
LM Studio backends across the tailnet, with ordered failover per route
(including optional OpenRouter as last resort).

"Model" names sent by clients are ROUTE names (ambient, quality, chronicle,
prose, utility, moment). Unknown names fall back to DEFAULT_ROUTE.

Stdlib only. Run: python3 03-router.py   (env: OPENROUTER_API_KEY optional)
"""

import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- CONFIG --
# EDIT tailnet IPs to match your machines. Order within a route = failover
# order. "model" is what the backend is told (llama-server ignores it;
# OpenRouter requires a real slug).

# The machines, the routes and the caps all come from site/fleet.toml now (plan 23 W3). They used to be
# written out here AND again in services/lore/fleet.py -- two copies of six addresses, which is the kind
# of duplication that is fine until the day one of them is edited.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
from common import fleet_config as _fleet  # noqa: E402

_CFG = _fleet.load()
_ROUTER = _fleet.router(_CFG)
QUEUE_WAIT = int(_ROUTER.get("queue_wait", 20))

# Each physical backend is built ONCE and the same object is reused in every route it appears in, so its
# semaphore is shared. Building a fresh one per route would give each its own cap, and the cap would
# quietly stop meaning anything -- the machine would take four concurrent requests per route instead of
# four in total.
_BUILT = {}

def _backend(name):
    """A fleet.toml backend in the shape this router's request path expects.

    The shapes differ on purpose and have to be translated here: fleet.toml holds a BASE url, because
    the batch side appends its own path, while call_backend passes ours straight to urllib. It also
    names the live-chat cap and timeout router_slots/router_timeout, because the same machine carries
    different numbers for batch work.
    """
    if name in _BUILT:
        return _BUILT[name]
    b = _fleet.backend(name, _CFG)
    url = b["url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")
    slots = int(b["router_slots"] or 0)
    _BUILT[name] = {
        "url": url,
        "model": b["model"],
        "key": b["api_key"],
        "timeout": int(b["router_timeout"]),
        "sem": threading.BoundedSemaphore(slots) if slots else None,
        "system_suffix": b["system_suffix"],
    }
    return _BUILT[name]

# A paid backend with no key configured drops out of every route it is named in, which is what an
# optional fallback has to mean for a realm that never bought one. fleet_config has already filtered it.
ROUTES = {name: [_backend(b["name"]) for b in chain]
          for name, chain in _fleet.routes(_CFG).items()}
ROUTES = {name: chain for name, chain in ROUTES.items() if chain}

DEFAULT_ROUTE = "ambient" if "ambient" in ROUTES else next(iter(ROUTES), "ambient")
BIND = (_ROUTER.get("bind", "0.0.0.0"), int(os.environ.get("ROUTER_PORT", _ROUTER.get("port", 11434))))

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# --------------------------------------------------------------- helpers --

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def call_backend(backend, messages, opts):
    suffix = backend.get("system_suffix")
    if suffix:
        # Copy so the suffix never leaks into the next backend on failover.
        messages = [dict(m) for m in messages]
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        if sys_msgs:
            sys_msgs[-1]["content"] = f"{sys_msgs[-1].get('content') or ''} {suffix}".strip()
        else:
            messages.insert(0, {"role": "system", "content": suffix})
    body = {
        "model": backend["model"],
        "messages": messages,
        "stream": False,
    }
    # Diversity controls are forwarded too: mod-ollama-chat only emits these when the
    # operator moves them off the module's hardcoded sentinels, so anything that arrives
    # here was asked for deliberately. presence/frequency are OpenAI-standard; repeat_penalty,
    # top_k and min_p are llama.cpp/LM Studio extensions that both backends accept and
    # OpenRouter ignores. Dropping them (as this loop used to) silently defeated every
    # anti-repetition setting in the module's config.
    for k_src, k_dst in (("temperature", "temperature"), ("top_p", "top_p"),
                         ("presence_penalty", "presence_penalty"),
                         ("frequency_penalty", "frequency_penalty"),
                         ("repeat_penalty", "repeat_penalty"),
                         ("top_k", "top_k"), ("min_p", "min_p")):
        if k_src in opts and opts[k_src] is not None:
            body[k_dst] = opts[k_src]
    if opts.get("num_predict"):
        body["max_tokens"] = opts["num_predict"]
    if opts.get("format") == "json":
        body["response_format"] = {"type": "json_object"}

    data = json.dumps(body).encode()
    req = urllib.request.Request(backend["url"], data=data,
                                 headers={"Content-Type": "application/json"})
    if backend.get("key"):
        req.add_header("Authorization", f"Bearer {backend['key']}")
    with urllib.request.urlopen(req, timeout=backend["timeout"]) as resp:
        out = json.loads(resp.read().decode())
    content = out["choices"][0]["message"]["content"] or ""
    return THINK_RE.sub("", content).strip()

def route_call(route_name, messages, opts):
    backends = ROUTES.get(route_name) or ROUTES[DEFAULT_ROUTE]
    errors = []
    for b in backends:
        name = b['url'].split('/v1')[0]
        sem = b.get("sem")
        t0 = time.time()
        if sem and not sem.acquire(timeout=QUEUE_WAIT):
            log(f"route={route_name} backend={name} BUSY after {QUEUE_WAIT}s wait")
            errors.append(f"{name} busy")
            continue
        waited = int((time.time()-t0)*1000)
        try:
            text = call_backend(b, messages, opts)
            log(f"route={route_name} backend={name} "
                f"ok {int((time.time()-t0)*1000)}ms (queued {waited}ms) {len(text)}ch")
            return text
        except Exception as e:  # noqa: BLE001 — any backend failure → next
            log(f"route={route_name} backend={name} "
                f"FAIL {type(e).__name__}: {e}")
            errors.append(str(e))
        finally:
            if sem:
                sem.release()
    raise RuntimeError(f"all backends failed for route '{route_name}': {errors}")

# --------------------------------------------------------------- server ---

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, *a):  # quiet default access log; we log ourselves
        pass

    # ---- GET
    def do_GET(self):
        if self.path.startswith("/api/tags"):
            models = [{"name": r, "model": r, "modified_at": now_iso(),
                       "size": 0, "details": {"family": "router"}}
                      for r in ROUTES]
            self._send(200, {"models": models})
        elif self.path.startswith("/api/version"):
            self._send(200, {"version": "0.1.0-router"})
        elif self.path.startswith("/health"):
            self._send(200, {"ok": True, "routes": list(ROUTES)})
        else:
            self._send(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        body = self._read_body()
        try:
            if self.path.startswith("/api/generate"):
                self.handle_generate(body)
            elif self.path.startswith("/api/chat"):
                self.handle_chat(body)
            elif self.path.startswith("/api/show"):
                # capability probe (mod-ollama-chat think-mode). Advertise
                # plain completion only, so 'think' is never requested.
                self._send(200, {"capabilities": ["completion"],
                                 "details": {"family": "router"}})
            elif self.path.startswith("/v1/chat/completions"):
                self.handle_openai(body)
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            log(f"ERROR {self.path}: {e}")
            self._send(500, {"error": str(e)})

    # ---- Ollama /api/generate
    def handle_generate(self, body):
        route = body.get("model") or DEFAULT_ROUTE
        opts = dict(body.get("options") or {})
        opts["format"] = body.get("format")
        messages = []
        if body.get("system"):
            messages.append({"role": "system", "content": body["system"]})
        messages.append({"role": "user", "content": body.get("prompt", "")})
        text = route_call(route, messages, opts)
        self._send(200, {"model": route, "created_at": now_iso(),
                         "response": text, "done": True,
                         "done_reason": "stop"})

    # ---- Ollama /api/chat
    def handle_chat(self, body):
        route = body.get("model") or DEFAULT_ROUTE
        opts = dict(body.get("options") or {})
        opts["format"] = body.get("format")
        messages = body.get("messages") or []
        text = route_call(route, messages, opts)
        self._send(200, {"model": route, "created_at": now_iso(),
                         "message": {"role": "assistant", "content": text},
                         "done": True, "done_reason": "stop"})

    # ---- OpenAI passthrough (for your own later services: chronicler etc.)
    def handle_openai(self, body):
        route = body.get("model") or DEFAULT_ROUTE
        opts = {"temperature": body.get("temperature"),
                "top_p": body.get("top_p"),
                "num_predict": body.get("max_tokens")}
        text = route_call(route, body.get("messages") or [], opts)
        self._send(200, {
            "id": "router-1", "object": "chat.completion",
            "created": int(time.time()), "model": route,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
        })

def main():
    log(f"router up on {BIND[0]}:{BIND[1]} routes={list(ROUTES)} "
        f"openrouter={'on' if os.environ.get('OPENROUTER_API_KEY') else 'off'}")
    ThreadingHTTPServer(BIND, Handler).serve_forever()

if __name__ == "__main__":
    sys.exit(main())
