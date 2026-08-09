"""Concrete agent backends: a CLI subprocess and an HTTP LLM API.

Each takes an injected runner/poster so the whole layer is testable with no real subprocess
and no network. A backend classifies failures: a terminal HTTP status (bad key/billing) raises
ResearcherError; everything else declines with 'uncertain' so one bad call never aborts a batch.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

from .models import ResearchAnswer, ResearcherError, ResearchQuestion
from .prompt import build_research_prompt, parse_answer

logger = logging.getLogger(__name__)

type CliRunner = Callable[[list[str], str], str]
type HttpPoster = Callable[[str, dict[str, str], dict[str, object]], dict[str, object]]

# HTTP statuses that never succeed on retry: a config problem the user must fix.
_TERMINAL_STATUS = {401, 402, 403}


class CliAgentResearcher:
    """Runs an agent CLI as a subprocess (default `claude -p --output-format json`), feeding
    the prompt on stdin and parsing stdout. No API key — reuses the CLI's own auth. The runner
    is injectable so tests drive it without spawning a process."""

    def __init__(self, command: list[str] | None = None, runner: CliRunner | None = None, timeout: int = 120) -> None:
        self._command = command or ["claude", "-p", "--output-format", "json"]
        self._timeout = timeout
        self._runner = runner or (lambda cmd, prompt: _subprocess_runner(cmd, prompt, self._timeout))

    def resolve(self, question: ResearchQuestion) -> ResearchAnswer:
        try:
            out = self._runner(list(self._command), build_research_prompt(question))
        except subprocess.TimeoutExpired:
            # A silent 'uncertain' would read as a real decline and hide a hung agent
            logger.warning("researcher timed out on %s; treating as uncertain", question.context.get("path", "?"))
            return ResearchAnswer(decision="uncertain")
        except Exception as exc:  # any other subprocess failure -> decline, never abort a batch
            logger.debug("researcher failed: %s", exc)
            return ResearchAnswer(decision="uncertain")
        return parse_answer(out)


def _subprocess_runner(command: list[str], prompt: str, timeout: int) -> str:
    # command is an app-controlled fixed list (no shell=True, no user argv); the prompt goes via stdin
    proc = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=timeout, encoding="utf-8")  # noqa: S603 -- fixed argv, no shell, stdin prompt
    if proc.returncode != 0:
        raise RuntimeError(f"agent exited {proc.returncode}: {(proc.stderr or '')[:200]}")
    return proc.stdout


class HttpAgentResearcher:
    """Calls an LLM HTTP API directly. Detects the wire (Anthropic vs OpenAI) from the base URL,
    forces a JSON reply, and optionally enables a web-search tool. The poster is injectable so
    tests never touch the network."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        poster: HttpPoster | None = None,
        web_search: bool = True,
        max_tokens: int = 1024,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._key = api_key
        self._anthropic = "anthropic" in base_url.lower()
        self._poster = poster or _requests_poster
        self._web = web_search
        self._max_tokens = max_tokens

    def resolve(self, question: ResearchQuestion) -> ResearchAnswer:
        prompt = build_research_prompt(question)
        url, headers, payload = self._request(prompt)
        try:
            resp = self._poster(url, headers, payload)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in _TERMINAL_STATUS:  # bad key / billing / forbidden -> fail loud, don't fake uncertain
                raise ResearcherError(f"researcher HTTP {status}: check the API key and billing") from exc
            return ResearchAnswer(decision="uncertain")  # transient/unknown -> decline, keep the batch alive
        return parse_answer(self._extract_text(resp))

    def _request(self, prompt: str) -> tuple[str, dict[str, str], dict[str, object]]:
        msgs = [{"role": "user", "content": prompt}]
        if self._anthropic:
            headers = {
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload: dict[str, object] = {"model": self._model, "max_tokens": self._max_tokens, "messages": msgs}
            if self._web:
                payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
            return f"{self._base}/messages", headers, payload
        headers = {"Authorization": f"Bearer {self._key}", "content-type": "application/json"}
        payload = {"model": self._model, "messages": msgs, "response_format": {"type": "json_object"}}
        return f"{self._base}/chat/completions", headers, payload

    def _extract_text(self, resp: dict[str, object]) -> str:
        if self._anthropic:
            content = resp.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return str(block.get("text", ""))
            return ""
        choices = resp.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                return str(message.get("content", ""))
        return ""


def _requests_poster(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
    import requests

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data: dict[str, object] = resp.json()
    return data
