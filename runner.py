# runner.py — вспомогательный модуль (используется server.py, отдельно не запускается)
# здесь логика парсинга k6-результатов, оставил отдельно чтобы можно было расширять
import json

def parse_summary(summary_path: str) -> dict:
    with open(summary_path, "r") as f:
        data = json.load(f)

    metrics = data.get("metrics", {})
    http_reqs = metrics.get("http_reqs", {}).get("values", {})
    http_req_duration = metrics.get("http_req_duration", {}).get("values", {})
    http_req_failed = metrics.get("http_req_failed", {}).get("values", {})

    return {
        "rps": round(http_reqs.get("rate", 0), 2),
        "total_requests": http_reqs.get("count", 0),
        "latency_avg": round(http_req_duration.get("avg", 0), 2),
        "latency_p50": round(http_req_duration.get("p(50)", 0), 2),
        "latency_p95": round(http_req_duration.get("p(95)", 0), 2),
        "latency_p99": round(http_req_duration.get("p(99)", 0), 2),
        "latency_max": round(http_req_duration.get("max", 0), 2),
        "error_rate": round(http_req_failed.get("rate", 0) * 100, 2),
        "error_count": int(http_req_failed.get("count", 0)),
    }