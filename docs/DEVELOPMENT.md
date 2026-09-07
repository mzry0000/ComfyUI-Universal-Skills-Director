# Development and compatibility

The repository root remains the ComfyUI package root. `tests/`, `tools/`, this file,
`AGENTS.md`, CI and development dependencies are source-only, not part of the installable ZIP.

## Offline checks

Use a separate development virtual environment, not ComfyUI's production environment:

```text
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install -r requirements-dev.txt
python -B -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check --line-length 96 .
python tools/package_release.py ../ComfyUI-Universal-Skills-Director.zip
```

Tests use temporary directories, mocked API responses and the real OpenAI SDK with
`httpx.MockTransport`. Socket connections are blocked in tests. Registration is loaded
with an empty mocked config; the operator's `ush_config.json` is not read. Torch is
stubbed where a tensor adapter is needed. No ComfyUI server or GPU is required.
CI runs these offline checks on Python 3.10/3.12 and Linux/Windows, without API secrets.
Running CI in GitHub itself is separate from local verification; a workflow file alone
does not demonstrate that those remote jobs have passed.

## API wire contract (2026-09-07)

`api_schema()` projects the Pydantic contract for transmission. It omits string
`minLength`/`maxLength` as a conservative compatibility policy, plus dispensable schema
annotations/defaults. It preserves field names (including `title`), `$defs`, enum,
pattern, numeric and array constraints. Host-side Pydantic validation keeps all limits.
`assert_strict_objects()` checks required properties and extra-key prohibition; it is
not a full replica of the API server's schema validator.

The official [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas)
documents pattern, numeric and array constraints for ordinary models and separate
fine-tuned-model restrictions. String length support is not clearly enumerated in the
positive support list. SDK strict conversion equality does not prove keyword support.
This policy targets ordinary OpenAI Responses models, not every Azure/compatible endpoint.

Stage profile choices are shared Literal enums; the host additionally checks that the
profile matches image/text media. Profiles label the downstream target, not a Python
renderer or a promise of a specific vendor dialect. Put precise formatting/style rules
in the Skill or request; model-specific adapters are intentionally absent.

Composer defaults to 8,192 output tokens for one prompt; the Plan Project widget defaults
to 16,384 for multiple stages. These are output-budget defaults, not guaranteed quality
thresholds. Reasoning uses the same budget. Incomplete responses fail explicitly; the
operator can adjust the budget independently of reasoning. No automatic budget increase
or regeneration is performed. The SDK normally retains its own retry default.

Live checks require explicit authorization, a known model, a small synthetic request,
fixed maximum calls/tokens, and disabled retries. Never print the key or raw SDK errors.
Test Composer and Director separately; success proves only the tested model/schema
combination, not all providers, keyword enforcement, image quality or Floyo integration.

After authorization, `python -B tools/live_smoke.py --live` runs at most two requests
against the official OpenAI endpoint using the key from private `ush_config.json`.
It uses two synthetic color swatches, the configured default model, low reasoning,
and 2,048 output tokens per request. No private Skill/images are sent. Without `--live`,
it does not read config or call the API. It prints a bounded JSON result with actual
prompts/usage, never credentials or raw exceptions. CI only exercises its HTTP-mocked path.

## Warnings and diagnostics

Image labels are matched with ASCII token boundaries so Japanese text adjacent to
`image1` is recognized. Matching is case-insensitive and includes out-of-range numbers.
This is a warning-only heuristic, not semantic analysis. For Director, check each
stage's local binding slots rather than global source names. Prompt text is never changed.
Warnings are deduplicated and capped at 64 with an explicit truncation notice.

Validation diagnostics show only known contract field names, bounded array indices
and allowlisted error types. Unknown keys are replaced with `<unknown>`; input values,
validator messages/context and underlying exception objects are not forwarded.

## Image artifact compatibility

New image records add `file_sha256`. Their `fingerprint` hashes versioned RGB8 pixels
and dimensions; `file_sha256` verifies PNG bytes and names the stored file. A codec or
compression change may create a second file for a separate attempt, but does not change
pixel identity or invalidate downstream dependencies solely because of encoding.

Repeated Record for an existing attempt validates the saved file and compares RGB8
pixels, then returns the exact previous artifact without re-encoding or writing.
Legacy three-field image artifacts retain their byte-based fingerprint and filename.
They still load and re-record without changing old ledger identities or session data.
Old identity semantics are deliberately not upgraded in place. New artifacts require
this updated reader; downgrading to the earlier v2 reader is not supported for them.
File and pixel hashes detect corruption, not authenticated provenance.

## Packaging

Only allowlisted runtime files enter the release. Private config is excluded without
reading it; templates must contain an empty key. A secondary credential-pattern check
rejects accidental keys in runtime source. The builder does not overwrite existing ZIPs.
Publish the source repository for contributors, and the runtime-only ZIP for installation.
Do not recursively zip the checkout: `.gitignore` does not protect arbitrary ZIP commands.
