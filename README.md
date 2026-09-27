# pastrybot

A memoryless language model livecodes music in [Strudel](https://strudel.cc), one 30-second turn at a time.

Every 30 seconds the model sees the code that's playing (plus any errors it logged) and writes the whole next version, which takes over exactly on the next 30-second mark. It keeps no memory between turns: the code on screen, and the comments it leaves itself there, are the only thread. Every three minutes it also gets the opening of a random Wikipedia article for inspiration (articles about specific music or musicians are skipped), with a preview two turns ahead so it can drift toward it.

## Run it

Needs [uv](https://docs.astral.sh/uv/).

```sh
# OpenRouter models (the default is openai/gpt-6-luna)
OPENROUTER_API_KEY=sk-or-... uv run python server.py

# or a Claude model, directly via Anthropic
ANTHROPIC_API_KEY=sk-ant-... uv run python server.py --model claude-opus-5-5
```

Then open <http://localhost:8000> and press the speaker to start.

Options:

- `--model`: an Anthropic model ID (e.g. `claude-opus-5-5`, `claude-haiku-4-5`) or `openrouter:<id>` (e.g. `openrouter:google/gemini-2.5-pro`)
- `--effort`: `low`, `medium` (default), `high`, `xhigh` or `max`; passed as Anthropic effort or OpenRouter reasoning effort where the model supports it
- `--interval`: seconds per turn (default 30)
- `--port`: default 8000

## Files

- `server.py`: serves the page, picks Wikipedia articles, and makes one model call per turn
- `system_prompt.md`: what the model is told about the performance, plus a Strudel cheat sheet
- `static/index.html`: the page, with Strudel's REPL, the 30-second grid and the swap-on-the-mark logic

---

App by [Carter Teplica](https://crtep.com) (and Opus 5.5) · made with [Strudel](https://strudel.cc)
