"""Serve the Strudel page and give a memoryless model one turn at the code per request."""

import argparse
import asyncio
import os
import re
import traceback
from pathlib import Path
from typing import Literal

import anthropic
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path(__file__).parent
SYSTEM_PROMPT = (ROOT / "system_prompt.md").read_text()
ANTHROPIC_OUTPUT_FORMAT = "\n\n# Output format\n\nPut the complete code in the `code` field, with no markdown fences."
OPENROUTER_OUTPUT_FORMAT = (
    "\n\n# Output format\n\nReply with the complete code in exactly one ```js fenced code block. "
    "Put nothing else outside it: no explanation before or after."
)
INSPIRATION_EVERY_N_TURNS = 6  # turn 1, then every 3 minutes at 30s per turn
PREVIEW_TURNS = 2  # the upcoming article is previewed on this many turns before it arrives


class ModelInfo(BaseModel):
    id: str  # Anthropic model ID, or "openrouter:<OpenRouter model ID>"
    name: str
    provider: Literal["anthropic", "openrouter"]
    input_price: float  # $ per million tokens
    output_price: float
    supports_effort: bool  # Anthropic effort / OpenRouter reasoning effort
    max_tokens: int


# Claude models go direct to Anthropic. Haiku 4.5 rejects the effort parameter (400).
ANTHROPIC_MODELS: list[ModelInfo] = [
    ModelInfo(id=i, name=n, provider="anthropic", input_price=a, output_price=b, supports_effort=e, max_tokens=16000)
    for i, n, a, b, e in [
        ("claude-fable-5-1", "Claude Fable 5.1", 10, 50, True),
        ("claude-opus-5-5", "Claude Opus 5.5", 4, 20, True),
        ("claude-opus-5", "Claude Opus 5", 5, 25, True),
        ("claude-sonnet-5", "Claude Sonnet 5", 2, 10, True),
        ("claude-haiku-4-5", "Claude Haiku 4.5", 1, 5, False),
    ]
]
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PREFIX = "openrouter:"
FENCED_CODE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
WIKIPEDIA_RANDOM_URL = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
MAX_ARTICLE_ROLLS = 10
# Articles about a specific piece of music or a musical artist are rerolled (they'd hand the
# model the answer). Wikidata says: an instance of (a subclass of) musical work, an instance
# of (a subclass of) musical group, or someone whose occupation is (a subclass of) musician.
IS_MUSIC_QUERY = """ASK {{
  {{ wd:{q} wdt:P31/wdt:P279* wd:Q2188189 }}
  UNION {{ wd:{q} wdt:P31/wdt:P279* wd:Q215380 }}
  UNION {{ wd:{q} wdt:P106/wdt:P279* wd:Q639669 }}
}}"""


class TurnRequest(BaseModel):
    model: str
    code: str
    turn: int
    elapsed_seconds: float
    eval_error: str | None
    runtime_errors: list[str]
    previous_runtime_errors: list[str]


class Article(BaseModel):
    title: str
    extract: str
    extract_html: str  # the page shows this; the subject is marked <b> as it appears in the sentence
    url: str


class TurnOutput(BaseModel):
    code: str


class TurnResponse(BaseModel):
    code: str
    article: Article | None
    upcoming: Article | None
    input_tokens: int
    output_tokens: int


class Config(BaseModel):
    interval_seconds: float
    model: str
    models: list[ModelInfo]


class Reply(BaseModel):
    code: str
    input_tokens: int
    output_tokens: int


class ModelReplyError(Exception):
    """The model answered, but not with usable code."""


def format_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def is_inspiration_turn(turn: int) -> bool:
    return (turn - 1) % INSPIRATION_EVERY_N_TURNS == 0


def next_inspiration_turn_after(turn: int) -> int:
    return turn + INSPIRATION_EVERY_N_TURNS - (turn - 1) % INSPIRATION_EVERY_N_TURNS


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


async def fetch_openrouter_models(http: httpx.AsyncClient) -> list[ModelInfo]:
    response = await http.get(OPENROUTER_MODELS_URL)
    response.raise_for_status()
    models: list[ModelInfo] = []
    for m in response.json()["data"]:
        input_price = float(m["pricing"]["prompt"]) * 1e6
        output_price = float(m["pricing"]["completion"]) * 1e6
        # Skip routers with variable pricing (-1), batch-only variants, and Claude (used directly).
        if input_price < 0 or m["id"].endswith(":batch") or m["id"].startswith("anthropic/"):
            continue
        if "text" not in m["architecture"]["output_modalities"]:
            continue
        max_completion = m["top_provider"]["max_completion_tokens"]
        models.append(
            ModelInfo(
                id=OPENROUTER_PREFIX + m["id"],
                name=m["name"],
                provider="openrouter",
                input_price=input_price,
                output_price=output_price,
                supports_effort="reasoning" in m["supported_parameters"],
                max_tokens=min(16000, max_completion) if max_completion else 16000,
            )
        )
    return sorted(models, key=lambda m: m.name.lower())


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
    return Reply(
        code=response.parsed_output.code,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


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
    return Reply(
        code=blocks[0].strip(),
        input_tokens=data["usage"]["prompt_tokens"],
        output_tokens=data["usage"]["completion_tokens"],
    )


def build_user_message(req: TurnRequest, article: Article | None, upcoming: Article | None) -> str:
    parts: list[str] = [
        f"Set time: {format_elapsed(req.elapsed_seconds)} elapsed. This is turn {req.turn}.",
    ]
    if article is not None:
        parts.append(
            f"Here is a random Wikipedia article for inspiration:\n\n# {article.title}\n\n{article.extract}"
        )
    if upcoming is not None:
        turns_away = next_inspiration_turn_after(req.turn) - req.turn
        when = "next turn" if turns_away == 1 else f"in {turns_away} turns"
        parts.append(
            f"Preview: this is the next random Wikipedia article for inspiration. It arrives {when}, "
            "when you'll see it again. You may start steering the music toward it now, if you want "
            f"the transition to feel gradual:\n\n# {upcoming.title}\n\n{upcoming.extract}"
        )
    if req.code.strip() == "":
        parts.append("The editor is empty. You are opening the set.")
    else:
        parts.append(f"Current code in the editor:\n\n```js\n{req.code}\n```")
    if req.eval_error is not None:
        parts.append(
            "This code FAILED to evaluate, so the audience is still hearing the previous "
            f"version (which you can't see). Error:\n{req.eval_error}"
        )
    if req.runtime_errors:
        joined = "\n".join(f"- {e}" for e in req.runtime_errors)
        parts.append(f"Errors/warnings logged in the first seconds of this code playing:\n{joined}")
    if req.previous_runtime_errors:
        joined = "\n".join(f"- {e}" for e in req.previous_runtime_errors)
        parts.append(
            "Errors/warnings logged while the PREVIOUS version played. The current code may already "
            f"have fixed them; check whether the sound or pattern responsible is still there:\n{joined}"
        )
    parts.append("What do you want to play next?")
    return "\n\n".join(parts)


def make_app(client: anthropic.AsyncAnthropic, model: str, effort: str, interval: float) -> FastAPI:
    app = FastAPI()

    # Show the actual error text on the page instead of a bare "Internal Server Error".
    @app.exception_handler(Exception)
    async def show_errors(_request: Request, exc: Exception) -> PlainTextResponse:
        traceback.print_exception(exc)
        return PlainTextResponse(f"{type(exc).__name__}: {exc}", status_code=500)
    # Wikipedia asks API clients to identify themselves; the random endpoint redirects to the article.
    http = httpx.AsyncClient(
        headers={"User-Agent": "pastrybot/0.1 (+https://crtep.com)"},
        follow_redirects=True,
        timeout=10.0,
    )

    # Articles chosen ahead of time, keyed by the inspiration turn they belong to, so the
    # page on load, the preview turns and the inspiration turn (and any retry of it) all see
    # the same one. Fetches are cached while in flight, so concurrent requests share one.
    articles: dict[int, asyncio.Task[Article]] = {}
    openrouter = httpx.AsyncClient(timeout=30.0)
    models_by_id: dict[str, ModelInfo] = {m.id: m for m in ANTHROPIC_MODELS}
    openrouter_loaded = False

    async def all_models() -> list[ModelInfo]:
        nonlocal openrouter_loaded
        if not openrouter_loaded:
            models_by_id.update({m.id: m for m in await fetch_openrouter_models(openrouter)})
            openrouter_loaded = True
        return list(models_by_id.values())

    async def article_for(inspiration_turn: int) -> Article:
        task = articles.get(inspiration_turn)
        if task is None:
            task = articles[inspiration_turn] = asyncio.create_task(fetch_random_article(http))
        try:
            return await task
        except Exception:
            if articles.get(inspiration_turn) is task:
                del articles[inspiration_turn]  # let a retry fetch again
            raise

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/config")
    async def config() -> Config:
        return Config(interval_seconds=interval, model=model, models=await all_models())

    # A page load starts a new set. Wikipedia's random endpoint can take ~2 s, so one opening
    # article is always fetched in advance: hand it out and start fetching the next.
    spare_opening: list[asyncio.Task[Article]] = []

    @app.get("/api/opening-article")
    async def opening_article() -> Article:
        articles.clear()
        if not spare_opening:
            spare_opening.append(asyncio.create_task(fetch_random_article(http)))
        articles[1] = spare_opening.pop()
        spare_opening.append(asyncio.create_task(fetch_random_article(http)))
        return await article_for(1)

    @app.post("/api/turn")
    async def turn(req: TurnRequest) -> TurnResponse:
        for stale in [t for t in articles if t < req.turn]:
            del articles[stale]
        article = await article_for(req.turn) if is_inspiration_turn(req.turn) else None
        next_turn = next_inspiration_turn_after(req.turn)
        upcoming = await article_for(next_turn) if next_turn - req.turn <= PREVIEW_TURNS else None
        user_message = build_user_message(req, article, upcoming)
        print(f"\n=== turn {req.turn} ===\n{user_message}\n")
        await all_models()
        model_info = models_by_id[req.model]
        print(f"(model: {model_info.id})")
        if model_info.provider == "anthropic":
            reply = await call_anthropic(client, model_info, effort, user_message)
        else:
            reply = await call_openrouter(openrouter, model_info, effort, user_message)
        print(f"--- new code ---\n{reply.code}\n")
        return TurnResponse(
            code=reply.code,
            article=article,
            upcoming=upcoming,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="openrouter:openai/gpt-6-luna", help="initial model (Anthropic ID or openrouter:<id>); switchable on the page")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"], help="ignored for models without effort/reasoning")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between Claude turns")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = make_app(anthropic.AsyncAnthropic(), args.model, args.effort, args.interval)
    print(f"Open http://localhost:{args.port}  (model={args.model}, effort={args.effort}, every {args.interval}s)")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
