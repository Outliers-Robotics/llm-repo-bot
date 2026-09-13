# llm-repo-bot

A Socket Mode Slack bot that answers questions about Team 5687's robot
repository using Gemini and read-only GitHub tools.

## Run

1. Install Python 3.10+ and `uv`, then run `uv sync --locked`.
2. Copy `.env.example` to `.env` and fill in the Slack, Gemini, and GitHub values.
3. Enable Socket Mode and subscribe to the `app_mention` bot event in your Slack
   app. The bot needs `app_mentions:read` and `chat:write`; the app-level token
   needs `connections:write`. Invite the bot to the relevant channel.
4. Start with `uv run --env-file .env main.py`.

Use a GitHub token that can search and read the configured repository.
Production can supply environment variables directly instead of using `.env`.

## Response behavior and limits

The bot immediately posts “I'm looking into that…” in the mention's thread,
then updates that message with the answer or an error. If the update fails,
it tries posting the final answer as another reply in the same thread. Bolt
acknowledges mention events before running the listener in its worker pool.

Answers use Slack's native `markdown_text` field for headings, bold labels,
lists, tables, clickable source links, and fenced C++ code. The same formatting
is used when updating the status and when posting fallback replies. Answers
over Slack's 12,000-character Markdown limit are split into thread replies at
paragraph or line boundaries, with fenced code continued in the next reply.

Independent repository lookups in one model response run concurrently, with
up to four workers per mention. Each answer allows up to four tool rounds and
twelve distinct tool calls. Identical lookups reuse their results within a
request; a round that only repeats previous lookups ends research early.
After a search finds files, the next research turn offers `read_file` with the
exact matching paths as permitted choices. Invented paths are rejected before
calling GitHub. Both repository tools become available again after a successful
read. The repository uses C++; code questions
need evidence from relevant headers, implementations, and referenced constants.
When research ends, one fresh model request receives the original question and
all executed lookup results as text, including errors and the last round's
results. That final request has no tool declarations or function-call history.
If it still requests a tool, returns no text, or fails, the bot asks for a more
specific class, file path, or method name. Intermediate text accompanying a
tool call is not used as the answer. Tools remain enabled during research;
no additional Slack or Gemini setting is needed to enable them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `gemini` | Model provider (currently only Gemini). |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model available to your Gemini API key. |
| `GEMINI_TIMEOUT_SECONDS` | `30` | Timeout per model HTTP attempt. |
| `GEMINI_MAX_TOOL_ROUNDS` | `4` | Maximum rounds of repository lookups. |

The two numeric settings must be positive integers. Gemini may retry a
retryable network or HTTP failure once, with a short backoff. GitHub requests
use a 5-second connection timeout and a 10-second read timeout. These are
network timeouts, not an overall answer deadline: a question requiring several
model rounds can still take longer. General questions can skip repository
tools; code-specific answers still require repository evidence.

## Diagnose delays and failures

Startup enables INFO logs. Each mention logs total elapsed time, each Gemini
round logs model/round/duration, and each repository tool logs its duration.
GitHub searches also log match counts and whether the results are incomplete,
without logging search text or file contents.
Failures include tracebacks. Compare these durations to see whether time is
being spent in Gemini, GitHub, or Slack delivery.

The warning about non-text `function_call` parts means a Gemini response
contains a tool request. It is not necessarily an API failure. Passing that
response's empty `.text` to Slack causes `say(None)` to raise `ValueError`.
The bot now executes tool requests and requires non-empty answer text before
sending the final Slack message.

When deploying a fix, rebuild/restart the running service with this checkout.
The previous `fd3c4d1` commit already rejected empty Gemini responses, so a
`NoneType` traceback from `say()` may indicate an older deployed revision.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

Tests use a mock HTTP transport through the real Gemini SDK and mocked GitHub
and Slack calls. They require no credentials and send no network requests.
They cover tool-only responses, the final answer after budget exhaustion,
parallel lookups, preserved tool-call history/signatures, API failures, and
Slack status/fallback behavior. Live latency still needs measurement in the
deployed environment.

An optional live Gemini connectivity check (uses your API key) is available:

```sh
uv run --env-file .env python -m llm.test_gemini
```
