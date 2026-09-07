"""Explicitly authorized, two-call API smoke check; never run by normal tests/CI."""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_CALLS = 2
MAX_OUTPUT_TOKENS = 2048
API_BASE = "https://api.openai.com/v1"
SKILL_TEXT = """# Minimal POP design Skill
Create a single still-image retail POP display design, not a video or a design report.
Use reference images as color references only. Write one short Japanese paragraph.
Keep the exact requested headline. Avoid visual inventories, QA notes and repetition.
"""
REQUEST = "遠距離から見出しが読める店頭POP什器の静止画案を1枚。image1を主色、image2をアクセント色とし、見出しは「NEW」。背景は白。短い日本語プロンプトにしてください。"
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


def run_checks(client):
    """Accept an injected SDK client so the smoke harness itself can be mocked."""
    import numpy as np
    from universal_skills.composer import PromptComposer
    from universal_skills.config import DEFAULT_MODEL
    from universal_skills.contracts import fingerprint
    from universal_skills.director.planner import DirectorPlanner
    from universal_skills.director.state import select_work_item
    from universal_skills.openai_client import OpenAIResponsesClient
    from universal_skills.specification import load_specification_content

    rows = []

    class RecordingClient(OpenAIResponsesClient):
        def create_response(self, payload):
            if len(rows) >= MAX_CALLS:
                raise RuntimeError("Smoke check call limit reached.")
            row = {
                "schema": payload["text"]["format"]["name"],
                "schema_sha256": fingerprint(payload["text"]["format"]["schema"]),
                "model_requested": payload["model"],
                "max_output_tokens": payload["max_output_tokens"],
                "reasoning_effort": payload["reasoning"]["effort"],
                "input_images": 2,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            rows.append(row)
            response = super().create_response(payload)
            row["status"] = response.status
            row["model_returned"] = response.model
            usage = response.usage
            row["usage"] = (
                {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                }
                if usage
                else None
            )
            return response

    wrapped = RecordingClient(client=client)
    blue = np.broadcast_to(
        np.array([0.10, 0.35, 0.85], dtype=np.float32), (1, 16, 16, 3)
    ).copy()
    orange = np.broadcast_to(
        np.array([0.95, 0.65, 0.20], dtype=np.float32), (1, 16, 16, 3)
    ).copy()
    settings = {
        "specification": load_specification_content("live-smoke-pop.md", SKILL_TEXT),
        "target_profile": "gpt_image_2",
        "model": DEFAULT_MODEL,
        "reasoning_effort": "low",
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "images": [("image1", blue), ("image2", orange)],
    }
    for kind in ("composer", "director"):
        request = (
            REQUEST
            if kind == "composer"
            else REQUEST + "画像生成1工程だけのPlanにしてください。"
        )
        before = len(rows)
        try:
            if kind == "composer":
                result = PromptComposer(client=wrapped).compose(request=request, **settings)
                prompt, warnings = result.final_prompt, list(result.warnings)
            else:
                plan, ledger = DirectorPlanner(client=wrapped).plan(request=request, **settings)
                selected, _, _ = select_work_item(plan, ledger)
                prompt, warnings = selected["prompt"], plan["warnings"]
                rows[-1]["stages"] = len(plan["stages"])
            if type(prompt) is not str or not prompt.strip():
                raise ValueError("Missing standard STRING output.")
            rows[-1].update(validated=True, final_prompt=prompt, warnings=warnings)
        except Exception as error:
            # Never print SDK bodies, exception repr/tracebacks or config data.
            if len(rows) == before:
                rows.append({"kind": kind, "api_call_attempted": False})
            rows[-1].update(validated=False, error_category=type(error).__name__)
            for code in (
                "model_not_found",
                "invalid_json_schema",
                "invalid_api_key",
                "insufficient_quota",
            ):
                if f"code: {code}" in str(error):
                    rows[-1]["api_error_code"] = code
            break
    return {
        "endpoint": API_BASE + "/responses",
        "max_calls": MAX_CALLS,
        "sdk_max_retries": 0,
        "skill": SKILL_TEXT,
        "request": REQUEST,
        "image_inputs": "Two synthetic 16x16 RGB swatches: blue primary and warm orange accent; no private images.",
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Authorize up to two paid API calls"
    )
    args = parser.parse_args()
    if not args.live:
        print("Not run. Explicit --live authorization is required; API usage is billable.")
        return 0

    # Prevent SDK/HTTP debug logging even if enabled in the surrounding environment.
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import httpx
        import openai
        from universal_skills.local_config import load_local_config

        key = load_local_config().api_key
        if not key:
            raise ValueError("No API key configured.")
        sent = 0

        def restrict_request(request):
            nonlocal sent
            if (
                request.method != "POST"
                or str(request.url) != API_BASE + "/responses"
                or sent >= MAX_CALLS
            ):
                raise RuntimeError("Request outside authorized smoke scope.")
            sent += 1

        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [restrict_request]},
        ) as http:
            with openai.OpenAI(
                api_key=key,
                base_url=API_BASE,
                max_retries=0,
                timeout=httpx.Timeout(45, connect=10),
                http_client=http,
            ) as client:
                result = run_checks(client)
        result["http_requests"] = sent
        serialized = json.dumps(result, ensure_ascii=True)
        if key in serialized or _KEY_PATTERN.search(serialized):
            raise ValueError("Potential credential in output; not exported.")
        print(serialized)
        return (
            0
            if len(result["results"]) == 2
            and all(row.get("validated") for row in result["results"])
            else 1
        )
    except Exception as error:
        print(json.dumps({"validated": False, "error_category": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
