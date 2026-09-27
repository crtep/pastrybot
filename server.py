"""pastrybot: one always-on station where a memoryless model livecodes Strudel.

The music is a single shared version of the code. Turns are aligned to clock time (every
:00 and :30). Five seconds into a turn, if anyone is listening, the model is shown the
playing code and writes the next version, which every listener's browser swaps in on the
next mark, beat-synced. With nobody listening, no model calls are made and the code stays.
"""

import argparse
import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import anthropic
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path(__file__).parent
SYSTEM_PROMPT = (ROOT / "system_prompt.md").read_text()
STATE_FILE = ROOT / "state.json"
ANTHROPIC_OUTPUT_FORMAT = "\n\n# Output format\n\nPut the complete code in the `code` field, with no markdown fences."
OPENROUTER_OUTPUT_FORMAT = (
    "\n\n# Output format\n\nReply with the complete code in exactly one ```js fenced code block. "
    "Put nothing else outside it: no explanation before or after."
)

TURN_SECONDS = 30
REQUEST_DELAY_SECONDS = 5  # into a turn before asking for the next version, so first errors come in
SAFETY_SECONDS = 8  # a version must be ready this long before a mark to start on it (clients poll every 2 s)
LISTENER_TTL_SECONDS = 12  # a listening heartbeat keeps someone counted as listening this long
VERSIONS_SENT = 3  # the live version, the next scheduled one, and what played before
INSPIRATION_EVERY_N_TURNS = 6  # a new Wikipedia article every 3 minutes, on the clock
PREVIEW_TURNS = 2  # the upcoming article is previewed on this many turns before it arrives
MAX_REPORTED_ERRORS = 10
MAX_ERROR_CHARS = 300
# The model is told the time of day here (it's never told where, and the page never shows it).
LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")


class ModelInfo(BaseModel):
    id: str  # Anthropic model ID, or "openrouter:<OpenRouter model ID>"
    name: str
    provider: Literal["anthropic", "openrouter"]
    supports_effort: bool  # Anthropic effort / OpenRouter reasoning effort
    max_tokens: int


# Claude models go direct to Anthropic. Haiku 4.5 rejects the effort parameter (400).
ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    i: ModelInfo(id=i, name=n, provider="anthropic", supports_effort=e, max_tokens=16000)
    for i, n, e in [
        ("claude-fable-5-1", "Claude Fable 5.1", True),
        ("claude-opus-5-5", "Claude Opus 5.5", True),
        ("claude-opus-5", "Claude Opus 5", True),
        ("claude-sonnet-5", "Claude Sonnet 5", True),
        ("claude-haiku-4-5", "Claude Haiku 4.5", False),
    ]
}
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PREFIX = "openrouter:"
FENCED_CODE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
WIKIPEDIA_RANDOM_URL = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "pastrybot/0.1 (+https://crtep.com)"
MAX_ARTICLE_ROLLS = 10
# Articles about a specific piece of music or a musical artist are rerolled (they'd hand the
# model the answer). Wikidata says: an instance of (a subclass of) musical work, an instance
# of (a subclass of) musical group, or someone whose occupation is (a subclass of) musician.
IS_MUSIC_QUERY = """ASK {{
  {{ wd:{q} wdt:P31/wdt:P279* wd:Q2188189 }}
  UNION {{ wd:{q} wdt:P31/wdt:P279* wd:Q215380 }}
  UNION {{ wd:{q} wdt:P106/wdt:P279* wd:Q639669 }}
}}"""


class Article(BaseModel):
    title: str
    extract: str
    extract_html: str  # the page shows this; the subject is marked <b> as it appears in the sentence
    url: str


class Version(BaseModel):
    id: int
    start_turn: int  # starts at start_turn * TURN_SECONDS (Unix time)
    code: str
    model: str
    output_tokens: int
    # what listeners' browsers reported while it played
    eval_error: str | None = None
    runtime_errors: list[str] = []


class TurnOutput(BaseModel):
    code: str


class Reply(BaseModel):
    code: str
    output_tokens: int


class ModelReplyError(Exception):
    """The model answered, but not with usable code."""


class Report(BaseModel):
    version_id: int
    eval_error: str | None
    runtime_errors: list[str]


class SyncRequest(BaseModel):
    client_id: str
    listening: bool  # pressed play, not muted, tab visible
    reports: list[Report]  # what this browser saw while evaluating/playing each version


class Writing(BaseModel):
    model: str
    active: bool  # writing right now; otherwise this is the last finished version's author
    output_tokens: int | None


class SyncResponse(BaseModel):
    server_time: float
    turn_seconds: int
    versions: list[Version]  # oldest first; the page plays the newest that has started
    writing: Writing | None
    article: Article | None  # the article in effect for the current turn
    upcoming: Article | None  # previewed in the turns before it arrives
    error: str | None  # the last model call's error, until one succeeds


def is_inspiration_turn(turn: int) -> bool:
    return turn % INSPIRATION_EVERY_N_TURNS == 0


def inspiration_turn_for(turn: int) -> int:
    return turn - turn % INSPIRATION_EVERY_N_TURNS


def next_inspiration_turn_after(turn: int) -> int:
    return inspiration_turn_for(turn) + INSPIRATION_EVERY_N_TURNS


def local_time_phrase(unix_time: float) -> str:
    t = datetime.fromtimestamp(unix_time, LOCAL_TIME_ZONE)
    return f"It's {t.strftime('%I:%M:%S').lstrip('0')} {t.strftime('%p').lower()}."


def clean_report(text: str) -> str:
    """Error text comes from anyone's browser and ends up in the prompt: keep it short and plain."""
    return re.sub(r"\s+", " ", text).strip()[:MAX_ERROR_CHARS]


async def is_about_music(http: httpx.AsyncClient, wikidata_id: str) -> bool:
    response = await http.get(
        WIKIDATA_SPARQL_URL, params={"query": IS_MUSIC_QUERY.format(q=wikidata_id), "format": "json"}
    )
    response.raise_for_status()
    return bool(response.json()["boolean"])


async def fetch_random_article(http: httpx.AsyncClient) -> Article:
    for _ in range(MAX_ARTICLE_ROLLS):
        response = await http.get(WIKIPEDIA_RANDOM_URL)
        response.raise_for_status()
        data = response.json()
        wikidata_id = data.get("wikibase_item")
        if wikidata_id is None:
            print(f"(rerolling article {data['title']!r}: no Wikidata item, so it can't be checked for music)")
            continue
        if await is_about_music(http, wikidata_id):
            print(f"(rerolling article {data['title']!r}: it's about a musical work or artist)")
            continue
        return Article(
            title=data["title"],
            extract=data["extract"],
            extract_html=data["extract_html"],
            url=data["content_urls"]["desktop"]["page"],
        )
    raise RuntimeError(f"{MAX_ARTICLE_ROLLS} random Wikipedia articles in a row were about music or unverifiable")


async def fetch_openrouter_model(http: httpx.AsyncClient, model_id: str) -> ModelInfo:
    response = await http.get(OPENROUTER_MODELS_URL)
    response.raise_for_status()
    wanted = model_id.removeprefix(OPENROUTER_PREFIX)
    for m in response.json()["data"]:
        if m["id"] == wanted:
            max_completion = m["top_provider"]["max_completion_tokens"]
            return ModelInfo(
                id=model_id,
                name=m["name"],
                provider="openrouter",
                supports_effort="reasoning" in m["supported_parameters"],
                max_tokens=min(16000, max_completion) if max_completion else 16000,
            )
    raise RuntimeError(f"OpenRouter has no model {wanted!r}")


async def call_anthropic(
    client: anthropic.AsyncAnthropic, model: ModelInfo, effort: str, user_message: str
) -> Reply:
    response = await client.messages.parse(
        model=model.id,
        max_tokens=model.max_tokens,
        system=SYSTEM_PROMPT + ANTHROPIC_OUTPUT_FORMAT,
        messages=[{"role": "user", "content": user_message}],
        output_format=TurnOutput,
        **({"output_config": {"effort": effort}} if model.supports_effort else {}),
    )
    if response.stop_reason != "end_turn":
        raise ModelReplyError(f"Unexpected stop_reason {response.stop_reason!r}: {response.stop_details}")
    if response.parsed_output is None:
        raise ModelReplyError("Claude returned no parsed output")
    return Reply(code=response.parsed_output.code, output_tokens=response.usage.output_tokens)


async def call_openrouter(http: httpx.AsyncClient, model: ModelInfo, effort: str, user_message: str) -> Reply:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ModelReplyError("OPENROUTER_API_KEY is not set in the server's environment")
    body: dict[str, object] = {
        "model": model.id.removeprefix(OPENROUTER_PREFIX),
        "max_tokens": model.max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + OPENROUTER_OUTPUT_FORMAT},
            {"role": "user", "content": user_message},
        ],
    }
    if model.supports_effort:
        # OpenRouter's reasoning effort tops out at "high"
        body["reasoning"] = {"effort": effort if effort in ("low", "medium", "high") else "high"}
    response = await http.post(
        OPENROUTER_CHAT_URL,
        json=body,
        headers={"Authorization": f"Bearer {api_key}", "X-Title": "pastrybot"},
        timeout=120.0,
    )
    data = response.json()
    if response.status_code != 200 or "error" in data:
        raise ModelReplyError(f"OpenRouter error (HTTP {response.status_code}): {data.get('error', data)}")
    choice = data["choices"][0]
    content: str = choice["message"]["content"] or ""
    if choice["finish_reason"] not in ("stop", "end_turn"):
        raise ModelReplyError(f"Unexpected finish_reason {choice['finish_reason']!r}. Reply:\n{content}")
    blocks = FENCED_CODE.findall(content)
    if len(blocks) != 1:
        raise ModelReplyError(f"Expected exactly one fenced code block, found {len(blocks)}. Reply:\n{content}")
    return Reply(code=blocks[0].strip(), output_tokens=data["usage"]["completion_tokens"])


def build_user_message(
    live: Version | None, previous: Version | None, target_turn: int, article: Article | None, upcoming: Article | None
) -> str:
    parts: list[str] = [local_time_phrase(target_turn * TURN_SECONDS)]
    if article is not None:
        parts.append(
            f"Here is a random Wikipedia article for inspiration:\n\n# {article.title}\n\n{article.extract}"
        )
    if upcoming is not None:
        turns_away = next_inspiration_turn_after(target_turn) - target_turn
        when = "next turn" if turns_away == 1 else f"in {turns_away} turns"
        parts.append(
            f"Preview: this is the next random Wikipedia article for inspiration. It arrives {when}, "
            "when you'll see it again. You may start steering the music toward it now, if you want "
            f"the transition to feel gradual:\n\n# {upcoming.title}\n\n{upcoming.extract}"
        )
    if live is None or live.code.strip() == "":
        parts.append("The editor is empty. You are opening the set.")
    else:
        parts.append(f"Current code in the editor:\n\n```js\n{live.code}\n```")
        if live.eval_error is not None:
            parts.append(
                "This code FAILED to evaluate, so listeners are still hearing the previous "
                f"version (which you can't see). Error:\n{live.eval_error}"
            )
        if live.runtime_errors:
            joined = "\n".join(f"- {e}" for e in live.runtime_errors)
            parts.append(f"Errors/warnings logged in the first seconds of this code playing:\n{joined}")
    if previous is not None:
        earlier = [e for e in previous.runtime_errors if live is None or e not in live.runtime_errors]
        if earlier:
            joined = "\n".join(f"- {e}" for e in earlier)
            parts.append(
                "Errors/warnings logged while the PREVIOUS version played. The current code may already "
                f"have fixed them; check whether the sound or pattern responsible is still there:\n{joined}"
            )
    parts.append("What do you want to play next?")
    return "\n\n".join(parts)


class Station:
    """The single shared performance: versions of the code, listeners, and the model calls."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: ModelInfo, effort: str) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.http = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=10.0)
        self.openrouter = httpx.AsyncClient(timeout=30.0)
        self.versions: list[Version] = self.load()
        self.listeners: dict[str, float] = {}  # client id -> last time it said it was listening
        self.writing = False
        self.error: str | None = None
        # articles keyed by the inspiration turn they belong to; fetches are shared while in flight
        self.articles: dict[int, asyncio.Task[Article]] = {}

    # ---- persistence: the music survives restarts
    def load(self) -> list[Version]:
        if not STATE_FILE.exists():
            return []
        return [Version.model_validate(v) for v in json.loads(STATE_FILE.read_text())["versions"]]

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps({"versions": [v.model_dump() for v in self.versions]}, indent=1))

    # ---- versions
    def live_version(self, turn: int) -> Version | None:
        started = [v for v in self.versions if v.start_turn <= turn]
        return started[-1] if started else None

    def scheduled_after(self, turn: int) -> Version | None:
        upcoming = [v for v in self.versions if v.start_turn > turn]
        return upcoming[0] if upcoming else None

    def add_version(self, version: Version) -> None:
        self.versions.append(version)
        # keep the live one, anything scheduled, and one before the live one (for its errors)
        live = self.live_version(int(time.time() // TURN_SECONDS))
        if live is not None:
            keep_from = max(0, self.versions.index(live) - 1)
            self.versions = self.versions[keep_from:]
        self.save()

    # ---- articles
    async def article_for(self, inspiration_turn: int) -> Article:
        task = self.articles.get(inspiration_turn)
        if task is None:
            task = self.articles[inspiration_turn] = asyncio.create_task(fetch_random_article(self.http))
            for stale in [t for t in self.articles if t < inspiration_turn - INSPIRATION_EVERY_N_TURNS]:
                del self.articles[stale]
        try:
            return await task
        except Exception:
            if self.articles.get(inspiration_turn) is task:
                del self.articles[inspiration_turn]  # let the next request fetch again
            raise

    def peek_article(self, inspiration_turn: int) -> Article | None:
        """For the page: the article if it's ready; otherwise start fetching it and show nothing yet."""
        task = self.articles.get(inspiration_turn)
        if task is None:
            asyncio.create_task(self.article_for(inspiration_turn))
            return None
        if task.done() and task.exception() is None:
            return task.result()
        return None

    # ---- listeners and reports
    def sync(self, req: SyncRequest) -> SyncResponse:
        now = time.time()
        if req.listening:
            self.listeners[req.client_id] = now
        for stale in [c for c, t in self.listeners.items() if now - t > LISTENER_TTL_SECONDS]:
            del self.listeners[stale]
        for report in req.reports[:VERSIONS_SENT]:
            reported = next((v for v in self.versions if v.id == report.version_id), None)
            if reported is None:
                continue
            if report.eval_error is not None and reported.eval_error is None:
                reported.eval_error = clean_report(report.eval_error)
            for e in report.runtime_errors[:MAX_REPORTED_ERRORS]:
                e = clean_report(e)
                if e and e not in reported.runtime_errors and len(reported.runtime_errors) < MAX_REPORTED_ERRORS:
                    reported.runtime_errors.append(e)

        turn = int(now // TURN_SECONDS)
        live = self.live_version(turn)
        start = max(0, self.versions.index(live) - 1) if live is not None else 0
        latest = self.versions[-1] if self.versions else None
        next_inspiration = next_inspiration_turn_after(turn)
        return SyncResponse(
            server_time=now,
            turn_seconds=TURN_SECONDS,
            versions=self.versions[start:][-VERSIONS_SENT:],
            writing=Writing(model=self.model.id, active=True, output_tokens=None) if self.writing
            else Writing(model=latest.model, active=False, output_tokens=latest.output_tokens) if latest
            else None,
            article=self.peek_article(inspiration_turn_for(turn)),
            upcoming=self.peek_article(next_inspiration) if next_inspiration - turn <= PREVIEW_TURNS else None,
            error=self.error,
        )

    def someone_listening_since(self, since: float) -> bool:
        return any(t >= since for t in self.listeners.values())

    # ---- the conductor: at most one model call per turn, and only if someone is listening
    async def conduct(self) -> None:
        called_in_turn: int | None = None
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            turn = int(now // TURN_SECONDS)
            into_turn = now - turn * TURN_SECONDS
            if (
                called_in_turn != turn
                and into_turn >= REQUEST_DELAY_SECONDS
                and not self.writing
                and self.scheduled_after(turn) is None  # the next version isn't already written
                and self.someone_listening_since(turn * TURN_SECONDS)
            ):
                called_in_turn = turn
                asyncio.create_task(self.write_next_version(turn))

    async def write_next_version(self, turn: int) -> None:
        self.writing = True
        try:
            target_turn = turn + 1
            live = self.live_version(turn)
            previous = self.versions[self.versions.index(live) - 1] if live and self.versions.index(live) > 0 else None
            article = await self.article_for(target_turn) if is_inspiration_turn(target_turn) else None
            next_inspiration = next_inspiration_turn_after(target_turn)
            upcoming = (
                await self.article_for(next_inspiration) if next_inspiration - target_turn <= PREVIEW_TURNS else None
            )
            user_message = build_user_message(live, previous, target_turn, article, upcoming)
            print(f"\n=== writing for {datetime.fromtimestamp(target_turn * TURN_SECONDS):%H:%M:%S} ===\n{user_message}\n")
            if self.model.provider == "anthropic":
                reply = await call_anthropic(self.client, self.model, self.effort, user_message)
            else:
                reply = await call_openrouter(self.openrouter, self.model, self.effort, user_message)
            # start on the first mark far enough away for every listener's browser to have it
            start_turn = target_turn
            while start_turn * TURN_SECONDS - time.time() < SAFETY_SECONDS:
                start_turn += 1
            if start_turn != target_turn:
                print(f"(arrived late; starts a turn later than planned)")
            self.add_version(
                Version(
                    id=(self.versions[-1].id + 1) if self.versions else 1,
                    start_turn=start_turn,
                    code=reply.code,
                    model=self.model.id,
                    output_tokens=reply.output_tokens,
                )
            )
            self.error = None
            print(f"--- new code (starts {datetime.fromtimestamp(start_turn * TURN_SECONDS):%H:%M:%S}) ---\n{reply.code}\n")
        except Exception as exc:
            traceback.print_exception(exc)
            self.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            self.writing = False


def make_app(station: Station) -> FastAPI:
    app = FastAPI()

    @app.on_event("startup")
    async def start_conducting() -> None:
        asyncio.create_task(station.conduct())

    # Show the actual error text instead of a bare "Internal Server Error".
    @app.exception_handler(Exception)
    async def show_errors(_request: Request, exc: Exception) -> PlainTextResponse:
        traceback.print_exception(exc)
        return PlainTextResponse(f"{type(exc).__name__}: {exc}", status_code=500)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    @app.post("/api/sync")
    async def sync(req: SyncRequest) -> SyncResponse:
        return station.sync(req)

    return app


async def resolve_model(model_id: str) -> ModelInfo:
    if model_id in ANTHROPIC_MODELS:
        return ANTHROPIC_MODELS[model_id]
    if model_id.startswith(OPENROUTER_PREFIX):
        async with httpx.AsyncClient(timeout=30.0) as http:
            return await fetch_openrouter_model(http, model_id)
    raise SystemExit(f"Unknown model {model_id!r}: use an Anthropic ID ({', '.join(ANTHROPIC_MODELS)}) or openrouter:<id>")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="openrouter:openai/gpt-6-luna", help="Anthropic model ID or openrouter:<id>")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"], help="ignored for models without effort/reasoning")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    model = asyncio.run(resolve_model(args.model))
    station = Station(anthropic.AsyncAnthropic(), model, args.effort)
    print(f"pastrybot on http://{args.host}:{args.port}  (model={model.id}, effort={args.effort})")
    uvicorn.run(make_app(station), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
