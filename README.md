# pastrybot

A memoryless language model livecodes music in [Strudel](https://strudel.cc), live at [crtep.com/pastrybot](https://crtep.com/pastrybot/).

It's one always-on station. The music is a single shared version of the code, on a 30-second grid aligned to clock time (every :00 and :30). Five seconds into a turn, if anyone is listening, the model is shown the playing code (plus any errors listeners' browsers reported) and writes the whole next version, which every listener's browser swaps in exactly on the next mark, on the downbeat, beat-synced across listeners. With nobody listening, no model calls are made and the code just waits. The model keeps no memory between turns: the code on screen, and the comments it leaves itself there, are the only thread.

Every three minutes (on the clock) it also gets the opening of a random Wikipedia article for inspiration, with a preview two turns ahead so it can drift toward it. Articles about specific music or musicians are skipped.

## Run it

Needs [uv](https://docs.astral.sh/uv/).

```sh
# OpenRouter models (the default is openai/gpt-6-luna)
OPENROUTER_API_KEY=sk-or-... uv run python server.py

# or a Claude model, directly via Anthropic
ANTHROPIC_API_KEY=sk-ant-... uv run python server.py --model claude-opus-5-5
```

Then open <http://localhost:8000> and press the speaker to start listening. The station's current code is kept in `state.json`, so it survives restarts.

Options:

- `--model`: an Anthropic model ID (e.g. `claude-opus-5-5`, `claude-haiku-4-5`) or `openrouter:<id>` (e.g. `openrouter:google/gemini-2.5-pro`)
- `--effort`: `low`, `medium` (default), `high`, `xhigh` or `max`; passed as Anthropic effort or OpenRouter reasoning effort where the model supports it
- `--host`, `--port`: default 127.0.0.1:8000

## Deploying

`deploy/` has a systemd unit (`pastrybot.service`) and a script that adds a `/pastrybot/` route to a Caddyfile, proxying to the service. The page uses relative URLs, so it works under any path prefix.

## Files

- `server.py`: the station: keeps the shared code, counts listeners, picks Wikipedia articles, and makes at most one model call per turn
- `system_prompt.md`: what the model is told about the performance, plus a Strudel cheat sheet
- `static/index.html`: the page: Strudel's REPL, clock sync with the server, joining on the shared beat, and the swap-on-the-mark logic
- `deploy/`: systemd unit and Caddy route for crtep.com

---

App by [Carter Teplica](https://crtep.com) (and Opus 5.5) · made with [Strudel](https://strudel.cc)
