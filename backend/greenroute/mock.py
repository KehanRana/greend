"""Local deterministic upstream. All regions are simulated on this same process."""
import asyncio
import json
import secrets
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from greenroute.config import Settings
from greenroute.contracts import ChatRequest

app = FastAPI(title="GreenRoute simulated inference")


@app.post("/{region}/v1/chat/completions")
async def complete(region: str, payload: ChatRequest, request: Request):
    settings = Settings.from_env()
    if not secrets.compare_digest(request.headers.get("authorization", ""),
                                  f"Bearer {settings.upstream_key.get_secret_value()}"):
        return JSONResponse({"error": {"message": "Invalid upstream key"}}, status_code=401)
    if region not in {"us-east-1", "eu-central-1", "ap-southeast-2"} or payload.model != "demo-model":
        return JSONResponse({"error": {"message": "Unknown simulated deployment"}}, status_code=404)
    await asyncio.sleep(settings.mock_delay_seconds)
    fixture = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "summaries.json").read_text())
    return {"id": "chatcmpl-demo-fixture", "object": "chat.completion", "created": 1793145600,
            "model": "demo-model", "choices": [{"index": 0, "message": {"role": "assistant",
            "content": fixture["summary"]}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": fixture["prompt_tokens"], "completion_tokens": fixture["completion_tokens"],
                      "total_tokens": fixture["prompt_tokens"] + fixture["completion_tokens"]}}


@app.get("/{region}/health")
async def health(region: str, request: Request):
    settings=Settings.from_env()
    if not secrets.compare_digest(request.headers.get("authorization","").encode(),
            f"Bearer {settings.upstream_key.get_secret_value()}".encode()):
        return JSONResponse({"error":{"message":"Invalid upstream key"}},status_code=401)
    if region not in {"us-east-1","eu-central-1","ap-southeast-2"}:
        return JSONResponse({"error":{"message":"Unknown simulated deployment"}},status_code=404)
    return {"status":"ok","mode":"demo"}
