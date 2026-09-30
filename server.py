import asyncio
import uuid
import time
import json
import os
import tempfile
import re
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

TG_TOKEN = os.environ.get("TG_TOKEN", "8705350376:AAHwmgyNaoFgQfWPmb0_ZftRGAALz6t-qMU")


async def tg_send(chat_id, text):
    if not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
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
    # для лестничного прогона
    steps: Optional[list] = None  # например [50, 200, 500, 1200]


def build_stages(req: StartRequest):
    """Возвращает stages для k6 или лестницу."""
    if req.steps:
        # лестничный: каждый шаг идёт duration/len секунд, плавный переход
        step_time = max(5, req.duration // len(req.steps))
        stages = []
        for p in req.steps:
            stages.append(f"{{ duration: '{step_time}s', target: {p} }}")
        # финальный откат до нуля
        stages.append(f"{{ duration: '5s', target: 0 }}")
        return "stages: [" + ", ".join(stages) + "],", max(req.steps)

    pps = req.pps
    d = req.duration

    if req.pattern == "ramp":
        return f"""stages: [
            {{ duration: '{max(1,int(d*0.25))}s', target: {max(1, pps//5)} }},
            {{ duration: '{max(1,int(d*0.5))}s', target: {pps} }},
            {{ duration: '{max(1,int(d*0.25))}s', target: {max(1, pps//2)} }},
        ],""", pps
    elif req.pattern == "spike":
        return f"""stages: [
            {{ duration: '{max(1,int(d*0.4))}s', target: {max(1, pps//10)} }},
            {{ duration: '{max(1,int(d*0.2))}s', target: {pps} }},
            {{ duration: '{max(1,int(d*0.4))}s', target: {max(1, pps//10)} }},
        ],""", pps
    elif req.pattern == "wave":
        half = max(1, d // 6)
        return f"""stages: [
            {{ duration: '{half}s', target: {pps} }},
            {{ duration: '{half}s', target: {max(1, pps//5)} }},
            {{ duration: '{half}s', target: {pps} }},
            {{ duration: '{half}s', target: {max(1, pps//5)} }},
            {{ duration: '{half}s', target: {pps} }},
            {{ duration: '{half}s', target: {max(1, pps//5)} }},
        ],""", pps
    else:
        return f"stages: [ {{ duration: '{d}s', target: {pps} }} ],", pps


def build_k6_script(req: StartRequest) -> str:
    method = req.method.upper()
    headers = dict(req.headers or {})
    if req.user_agent:
        headers["User-Agent"] = req.user_agent
    if req.cookie:
        headers["Cookie"] = req.cookie

    headers_json = json.dumps(headers)
    body_json = json.dumps(req.body or "")

    stages_str, max_pps = build_stages(req)

    pre_vus = min(max(max_pps, 10), 3000)
    max_vus = min(max(max_pps * 3, 20), 6000)

    redirects = "false" if req.strict else "true"
    ok_expr = "(r.status === 200)" if req.strict else "(r.status >= 200 && r.status < 400)"

    script = f"""
import http from 'k6/http';
import {{ check }} from 'k6';

export const options = {{
    {stages_str}
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


def parse_k6_summary(raw: str) -> dict:
    if not raw:
        return {}
    matches = list(re.finditer(r'\{"metrics":\s*\{', raw))
    if matches:
        start = matches[-1].start()
        depth = 0; i = start; in_string = False; escape = False
        while i < len(raw):
            ch = raw[i]
            if escape: escape = False; i += 1; continue
            if ch == '\\': escape = True; i += 1; continue
            if ch == '"': in_string = not in_string; i += 1; continue
            if not in_string:
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try: return json.loads(raw[start:i+1])
                        except Exception: pass
            i += 1
    return {}


async def run_k6(test_id: str, req: StartRequest, is_ladder: bool = False):
    script = build_k6_script(req)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()

    summary_file = f"/tmp/k6sum_{test_id}.json"
    stream_file = f"/tmp/k6stream_{test_id}.json"

    proc = await asyncio.create_subprocess_exec(
        "k6", "run",
        "--summary-mode=full",
        f"--summary-export={summary_file}",
        f"--out=json={stream_file}",
        tmp.name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    TESTS[test_id]["proc"] = proc
    TESTS[test_id]["status"] = "running"
    TESTS[test_id]["started"] = time.time()
    TESTS[test_id]["stream_file"] = stream_file
    TESTS[test_id]["stream_pos"] = 0

    stdout_data = b""
    stderr_data = b""

    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=req.duration * 3 + 120
        )
        TESTS[test_id]["status"] = "finished"
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        TESTS[test_id]["status"] = "timeout"
    except Exception as e:
        TESTS[test_id]["status"] = "error"
        TESTS[test_id]["error"] = str(e)
    finally:
        stdout_text = stdout_data.decode('utf-8', errors='ignore') if stdout_data else ""
        stderr_text = stderr_data.decode('utf-8', errors='ignore') if stderr_data else ""

        print(f"=== k6 {test_id} STDOUT tail ===")
        print(stdout_text[-2000:])

        summary = {}
        try:
            with open(summary_file, "r") as f:
                summary = json.load(f)
        except Exception as e:
            print(f"summary file failed: {e}")

        if not summary or not summary.get("metrics"):
            summary = parse_k6_summary(stdout_text)

        TESTS[test_id]["summary"] = summary
        TESTS[test_id]["finished"] = time.time()

        # мониторинг после теста — 30 секунд пинга
        if summary:
            TESTS[test_id]["recovery"] = await check_recovery(req.target, req.timeout)

        try: os.unlink(tmp.name)
        except Exception: pass
        try: os.unlink(summary_file)
        except Exception: pass

        if req.tg_chat_id and summary:
            m = summary.get("metrics", {})
            reqs = m.get("http_reqs", {}) or {}
            dur = m.get("http_req_duration", {}) or {}
            text = (
                f"*Тест завершён*\n"
                f"Цель: `{req.target}`\n"
                f"RPS: `{round(reqs.get('rate', 0), 1)}`\n"
                f"P95: `{round(dur.get('p(95)', 0))}ms`"
            )
            await tg_send(req.tg_chat_id, text)


async def check_recovery(target: str, timeout: int) -> dict:
    """Пингуем цель 30 секунд после теста, засекаем когда ответит снова."""
    import urllib.request
    start = time.time()
    first_ok_at = None
    checks = 0
    ok_checks = 0

    for _ in range(30):
        await asyncio.sleep(1)
        checks += 1
        try:
            req = urllib.request.Request(target, method="HEAD",
                headers={"User-Agent": "LoadTester-Recovery/1.0"})
            loop = asyncio.get_event_loop()
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=timeout)),
                timeout=timeout + 1
            )
            if resp.status < 500:
                ok_checks += 1
                if first_ok_at is None:
                    first_ok_at = time.time() - start
        except Exception:
            pass

    return {
        "checks": checks,
        "ok_checks": ok_checks,
        "first_ok_after_sec": round(first_ok_at, 1) if first_ok_at is not None else None,
        "available_percent": round(ok_checks / checks * 100, 1) if checks else 0,
    }


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
        "is_ladder": bool(req.steps),
    }
    asyncio.create_task(run_k6(test_id, req, is_ladder=bool(req.steps)))
    return {"id": test_id}


@app.get("/api/status/{test_id}")
async def status(test_id: str):
    t = TESTS.get(test_id)
    if not t:
        raise HTTPException(404, "test not found")
    elapsed = time.time() - t["started"]
    remaining = max(0, t["duration"] - elapsed)
    resp = {
        "id": test_id, "status": t["status"],
        "elapsed": round(elapsed, 1), "remaining": round(remaining, 1),
        "target": t["target"], "pps": t["pps"],
        "is_ladder": t.get("is_ladder", False),
    }
    if t["status"] in ("finished", "timeout", "stopped", "error"):
        resp["summary"] = t.get("summary", {})
        resp["recovery"] = t.get("recovery", {})
        if t.get("error"):
            resp["error"] = t["error"]
    return resp


@app.get("/api/stream/{test_id}")
async def stream(test_id: str):
    """Возвращает новые строки из json-out k6 с последнего запроса."""
    t = TESTS.get(test_id)
    if not t:
        raise HTTPException(404, "test not found")
    path = t.get("stream_file")
    if not path or not os.path.exists(path):
        return {"lines": [], "pos": 0}

    pos = t.get("stream_pos", 0)
    lines = []
    try:
        with open(path, "r") as f:
            f.seek(pos)
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
            new_pos = f.tell()
        t["stream_pos"] = new_pos
    except Exception as e:
        return {"lines": [], "pos": pos, "error": str(e)}

    # чтобы не раздувать ответ — только последние 200
    return {"lines": lines[-200:], "pos": t["stream_pos"]}


@app.post("/api/stop/{test_id}")
async def stop(test_id: str):
    t = TESTS.get(test_id)
    if not t:
        raise HTTPException(404, "test not found")
    proc = t.get("proc")
    if proc and proc.returncode is None:
        try: proc.kill()
        except Exception: pass
        t["status"] = "stopped"
    return {"ok": True}


@app.get("/")
async def root():
    return FileResponse("index.html")


@app.get("/health")
async def health():
    return {"ok": True, "tg_configured": TG_TOKEN != "PASTE_YOUR_BOT_TOKEN_HERE"}
