# server.py
import asyncio
import uuid
import time
import json
import os
import tempfile
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

class StartRequest(BaseModel):
    target: str
    pps: int = 200          # requests per second
    duration: int = 60      # seconds
    method: str = "GET"
    headers: Optional[dict] = None
    body: Optional[str] = None
    pattern: str = "steady" # steady / ramp / spike

def build_k6_script(req: StartRequest) -> str:
    method = req.method.upper()
    headers_json = json.dumps(req.headers or {})
    body_json = json.dumps(req.body or "")

    # для ramp: 20% -> 100% за 20% времени
    if req.pattern == "ramp":
        stages = f"""
        stages: [
            {{ duration: '{req.duration*0.2}s', target: {req.pps} }},
            {{ duration: '{req.duration*0.8}s', target: {req.pps} }},
        ],"""
    elif req.pattern == "spike":
        stages = f"""
        stages: [
            {{ duration: '{req.duration*0.5}s', target: {max(1, req.pps//10)} }},
            {{ duration: '{req.duration*0.1}s', target: {req.pps} }},
            {{ duration: '{req.duration*0.4}s', target: {max(1, req.pps//10)} }},
        ],"""
    else:  # steady
        stages = f"""
        stages: [
            {{ duration: '{req.duration}s', target: {req.pps} }},
        ],"""

    # preAllocatedVUs даём с запасом
    pre_vus = min(max(req.pps, 10), 2000)
    max_vus = min(max(req.pps * 2, 20), 4000)

    script = f"""
import http from 'k6/http';
import {{ check }} from 'k6';

export const options = {{
    {stages}
    preAllocatedVUs: {pre_vus},
    maxVUs: {max_vus},
    thresholds: {{}},
    summaryTrendStats: ['avg','min','med','p(50)','p(95)','p(99)','max'],
}};

const TARGET = {json.dumps(req.target)};
const METHOD = {json.dumps(method)};
const HEADERS = {headers_json};
const BODY = {body_json};

export default function () {{
    const params = {{ headers: HEADERS }};
    let res;
    if (METHOD === 'GET' || METHOD === 'HEAD') {{
        res = http.request(METHOD, TARGET, null, params);
    }} else {{
        res = http.request(METHOD, TARGET, BODY, params);
    }}
    check(res, {{ 'status ok': (r) => r.status >= 200 && r.status < 400 }});
}}
"""
    return script


async def run_k6(test_id: str, req: StartRequest):
    """Запускает k6, парсит результат, обновляет TESTS."""
    script = build_k6_script(req)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()

    proc = await asyncio.create_subprocess_exec(
        "k6", "run",
        "--quiet",
        "--summary-export=/tmp/k6sum.json",
        tmp.name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    TESTS[test_id]["proc"] = proc
    TESTS[test_id]["status"] = "running"
    TESTS[test_id]["started"] = time.time()

    try:
        await asyncio.wait_for(proc.wait(), timeout=req.duration + 60)
        TESTS[test_id]["status"] = "finished"
    except asyncio.TimeoutError:
        proc.kill()
        TESTS[test_id]["status"] = "timeout"
    finally:
        try:
            with open("/tmp/k6sum.json", "r") as f:
                summary = json.load(f)
            TESTS[test_id]["summary"] = summary
        except Exception as e:
            TESTS[test_id]["summary"] = {"error": str(e)}
        TESTS[test_id]["finished"] = time.time()
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


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

    if t["status"] in ("finished", "timeout"):
        resp["summary"] = t.get("summary", {})

    return resp


@app.post("/api/stop/{test_id}")
async def stop(test_id: str):
    t = TESTS.get(test_id)
    if not t:
        raise HTTPException(404, "test not found")
    proc = t.get("proc")
    if proc and proc.returncode is None:
        proc.kill()
        t["status"] = "stopped"
    return {"ok": True}


@app.get("/")
async def root():
    return FileResponse("index.html")


@app.get("/health")
async def health():
    return {"ok": True}