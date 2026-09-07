# Development contract

- Read this file and `docs/` before changing behavior. Write the implementation plan before coding.
- Keep Load Skill + Prompt Composer as the default path. Director stays opt-in and manual.
- Preserve node IDs, widget/output positions, D&D embedding, and unmodified `final_prompt: STRING`.
- Pydantic is the single contract source. API schema projection must not weaken host validation.
- Verify API fields against current official OpenAI documentation; record compatibility decisions in `docs/DEVELOPMENT.md`.
- API tests and CI are offline by default. Live calls require explicit user authorization and bounded calls/tokens, with automatic retries disabled for smoke tests.
- Never print credentials, read private config into tool output, or include secrets in workflows, prompts, fixtures, reports, Git or release ZIPs. Do not scan private config contents when inspecting source.
- Do not reintroduce Hosted Skills execution, arbitrary shell/tools, auto-queue, automatic data deletion, or legacy prompt reconstruction.
- Preserve existing sessions and artifacts; test backwards-compatible reads and idempotent retries before changing hashes.
- Run `python -B -m unittest discover -s tests` and `python -m ruff check .`. Use `tools/package_release.py` for release ZIPs; tests/dev files remain in Git but outside the installable ZIP.
- Do not publish or push to GitHub unless explicitly requested.
