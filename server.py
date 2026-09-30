import asyncio
import uuid
import time
import json
import os
import tempfile
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TESTS = {}

# ============================================================
# TELEGRAM TOKEN
# ОСТАВИЛ ТВОЙ ТЕСТОВЫЙ. ПОСЛЕ /revoke В BotFather ЗАМЕНИ.
# Или через Render Environment Variable TG_TOKEN
# ============================================================
TG_TOKEN = os.environ.get("TG_TOKEN", "8705350376:AAHwmgyNaoFgQfWPmb0_ZftRGAALz6t-qMU")


async def tg_send(chat_id, text):
    if not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }).encode()
        req = urllib.request.Request(url, data=data)
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=10)
        )
    except Exception as e:
        print("TG send failed:", e)


class StartRequest(BaseModel):
    target: str
    pps: int = 200
    duration: int = 60
    method: str = "GET"
    headers: Optional[dict] = None
    body: Optional[str] = None
    pattern: str = "steady"
    timeout: int = 5
    user_agent: Optional[str] = None
    cookie: Optional[str] = None
    strict: bool = False
    tg_chat_id: Optional[str] = None


def build_k6_script(req: StartRequest) -> str:
    method = req.method.upper()
    headers = dict(req.headers or {})
    if req.user_agent:
        headers["User-Agent"] = req.user_agent
    if req.cookie:
        headers["Cookie"] = req.cookie

    headers_json = json.dumps(headers)
    body_json = json.dumps(req.body or "")

    if req.pattern == "ramp":
        stages = f"""
        stages: [
            {{ duration: '{max(1,int(req.duration*0.25))}s', target: {max(1, req.pps//5)} }},
            {{ duration: '{max(1,int(req.duration*0.5))}s', target: {req.pps} }},
            {{ duration: '{max(1,int(req.duration*0.25))}s', target: {max(1, req.pps//2)} }},
        ],"""
    elif req.pattern == "spike":
        stages = f"""
        stages: [
            {{ duration: '{max(1,int(req.duration*0.4))}s', target: {max(1, req.pps//10)} }},
            {{ duration: '{max(1,int(req.duration*0.2))}s', target: {req.pps} }},
            {{ duration: '{max(1,int(req.duration*0.4))}s', target: {max(1, req.pps//10)} }},
        ],"""
    elif req.pattern == "wave":
        half = max(1, req.duration // 6)
        stages = f"""
        stages: [
            {{ duration: '{half}s', target: {req.pps} }},
            {{ duration: '{half}s', target: {max(1, req.pps//5)} }},
            {{ duration: '{half}s', target: {req.pps} }},
            {{ duration: '{half}s', target: {max(1, req.pps//5)} }},
            {{ duration: '{half}s', target: {req.pps} }},
            {{ duration: '{half}s', target: {max(1, req.pps//5)} }},
        ],"""
    else:
        stages = f"""
        stages: [
            {{ duration: '{req.duration}s', target: {req.pps} }},
        ],"""

    pre_vus = min(max(req.pps, 10), 3000)
    max_vus = min(max(req.pps * 3, 20), 6000)

    redirects = "false" if req.strict else "true"
    ok_expr = "(r.status === 200)" if req.strict else "(r.status >= 200 && r.status < 400)"

    script = f"""
import http from 'k6/http';
import {{ check }} from 'k6';

export const options = {{
    {stages}
    preAllocatedVUs: {pre_vus},
    maxVUs: {max_vus},
    timeout: '{req.timeout}s',
    thresholds: {{}},
    summaryTrendStats: ['avg','min','med','p(50)','p(90)','p(95)','p(99)','max'],
    noConnectionReuse: false,
}};

const TARGET = {json.dumps(req.target)};
const METHOD = {json.dumps(method)};
const HEADERS = {headers_json};
const BODY = {body_json};

export default function () {{
    const params = {{ headers: HEADERS, redirects: {redirects}, timeout: '{req.timeout}s' }};
    let res;
    if (METHOD === 'GET' || METHOD === 'HEAD') {{
        res = http.request(METHOD, TARGET, null, params);
    }} else {{
        res = http.request(METHOD, TARGET, BODY, params);
    }}
    check(res, {{ 'ok': (r) => {ok_expr} }});
}}
"""
    return script


async def run_k6(test_id: str, req: StartRequest):
    script = build_k6_script(req)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()

    summary_file = f"/tmp/k6sum_{test_id}.json"

    proc = await asyncio.create_subprocess_exec(
        "k6", "run",
        "--quiet",
        f"--summary-export={summary_file}",
        tmp.name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    TESTS[test_id]["proc"] = proc
    TESTS[test_id]["status"] = "running"
    TESTS[test_id]["started"] = time.time()

    try:
        await asyncio.wait_for(proc.wait(), timeout=req.duration * 2 + 60)
        TESTS[test_id]["status"] = "finished"
    except asyncio.TimeoutError:
        proc.kill()
        TESTS[test_id]["status"] = "timeout"
    except Exception as e:
        TESTS[test_id]["status"] = "error"
        TESTS[test_id]["error"] = str(e)
    finally:
        try:
            with open(summary_file, "r") as f:
                summary = json.load(f)
            TESTS[test_id]["summary"] = summary
        except Exception as e:
            TESTS[test_id]["summary"] = {"error": str(e)}
        TESTS[test_id]["finished"] = time.time()
        try: os.unlink(tmp.name)
        except Exception: pass
        try: os.unlink(summary_file)
        except Exception: pass

        if req.tg_chat_id and TESTS[test_id].get("summary"):
            s = TESTS[test_id]["summary"]
            m = s.get("metrics", {})
            reqs = m.get("http_reqs", {}).get("values", {})
            dur = m.get("http_req_duration", {}).get("values", {})
            failed = m.get("http_req_failed", {}).get("values", {})
            text = (
                f"*Тест завершён*\n"
                f"Цель: `{req.target}`\n"
                f"RPS: `{round(reqs.get('rate', 0), 1)}`\n"
                f"P95: `{round(dur.get('p(95)', 0))}ms`\n"
                f"Ошибки: `{round(failed.get('rate', 0) * 100, 2)}%`\n"
                f"Всего запросов: `{reqs.get('count', 0)}`"
            )
            await tg_send(req.tg_chat_id, text)


@app.post("/api/start")
async def start(req: StartRequest):
    if not req.target.startswith(("http://", "https://")):
        req.target = "http://" + req.target

    test_id = str(uuid.uuid4())[:8]
    TESTS[test_id] = {
        "id": test_id,
        "target": req.target,
        "pps": req.pps,
        "duration": req.duration,
        "pattern": req.pattern,
        "method": req.method,
        "status": "starting",
        "started": time.time(),
    }
    asyncio.create_task(run_k6(test_id, req))
    return {"id": test_id}


@app.get("/api/status/{test_id}")
async def status(test_id: str):
    t = TESTS.get(test_id)
    if not t:
        raise HTTPException(404, "test not found")
    elapsed = time.time() - t["started"]
    remaining = max(0, t["duration"] - elapsed)
    resp = {
        "id": test_id,
        "status": t["status"],
        "elapsed": round(elapsed, 1),
        "remaining": round(remaining, 1),
        "target": t["target"],
        "pps": t["pps"],
    }
    if t["status"] in ("finished", "timeout", "stopped", "error"):
        resp["summary"] = t.get("summary", {})
        if t.get("error"):
            resp["error"] = t["error"]
    return resp


@app.post("/api/stop/{test_id}")
async def stop(test_id: str):
    t = TESTS.get(test_id)
    if not t:
        raise HTTPException(404, "test not found")
    proc = t.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
        t["status"] = "stopped"
    return {"ok": True}


@app.get("/")
async def root():
    return FileResponse("index.html")


@app.get("/health")
async def health():
    return {"ok": True, "tg_configured": TG_TOKEN != "PASTE_YOUR_BOT_TOKEN_HERE"}
