# llm-repo-bot

A Socket Mode Slack bot that answers questions about Team 5687's robot
repository using Gemini and read-only GitHub tools.

## Run

1. Install Python 3.10+ and `uv`, then run `uv sync --locked`.
2. Copy `.env.example` to `.env` and fill in the Slack, Gemini, and GitHub values.
3. Enable Socket Mode in your Slack app.
   - Bot Token Scopes: `app_mentions:read`, `chat:write`, `channels:history` (for public channels), and `files:write` (for lookup table graphs).
     *Note on channel types*: In Slack's API, public channels require `channels:history`, while private channels (lock icon 🔒) require `groups:history`. Direct messages (DMs) are governed by `im:history`.
   - Event Subscriptions: subscribe to `app_mention`. To allow users to reply directly in active bot threads without re-tagging `@bot`, also subscribe to `message.channels` (and `message.groups` if using private channels).
   - App-level token needs `connections:write`. Invite the bot to the relevant channel.
4. Start with `uv run --env-file .env main.py`.

Use a GitHub token that can search and read the configured repository.
Production can supply environment variables directly instead of using `.env`.

## Response behavior and limits

The bot immediately posts “I'm looking into that…” in the mention's thread,
then updates that message with the answer or an error. If the update fails,
it tries posting the final answer as another reply in the same thread. Bolt
acknowledges mention events before running the listener in its worker pool.

Answers use Slack's native `markdown_text` field for headings, bold labels,
lists, tables, clickable source links, and fenced C++ code. Raw LaTeX math syntax
is automatically sanitized into clean plaintext and Unicode (such as ≤, ≥, °, ², RPM)
for native Slack readability. The same formatting is used when updating the status
and when posting fallback replies. Answers over Slack's 12,000-character Markdown limit
are split into thread replies at paragraph or line boundaries, with fenced code continued
in the next reply.

## Message chains and thread follow-ups

When a user asks a question or replies in an existing thread, the bot only uses context
strictly from the specific thread it is in. Top-level channel questions start fresh with
no prior context.

The bot automatically:
- Strictly scopes conversation context to the thread it is in: top-level channel questions never inherit history, and threads never cross-pollinate.
- Queries `conversations.replies` for public channels via `channels:history`, keeping thread history persistent across bot restarts without needing group permissions.
- Automatically handles thread replies even if the user replies directly in the thread without re-tagging `@bot` (when `message.channels` is subscribed).
- Maintains a channel- and thread-scoped cache (`ThreadHistoryCache`) and event deduplicator (`MessageDeduplicator`) so multi-turn conversations work seamlessly without double-processing.
- Filters out status messages, current turn timestamps, and third-party bot messages.
- Cleans bot mentions and maps roles between user turns and assistant responses.
- Normalizes turns to ensure valid alternating conversation flows for underlying LLMs.

## Graphs from lookup tables

Ask, for example:

> @OutliersLLM-Bot Graph the flywheel RPM lookup table against distance.

> @OutliersLLM-Bot Compare the RPM lookup tables in these files on one graph: …

The bot reads the C++ source, renders a PNG with Matplotlib, and attaches it in
the same Slack thread. Graphs include labeled axes, a legend, and source paths;
the text answer includes source links. Multiple tables with matching units can
share one chart; different quantities use separate charts.

For an existing Slack installation, add **`files:write`** under **OAuth &
Permissions → Bot Token Scopes**, then **reinstall the app to the workspace**.
Rebuild/restart the bot to install the new Python dependency. A missing upload
scope produces an explanation in the reply while preserving the text answer.

The plotting tool selects named brace-initialized `{x, y}` tables from files
read during that request. Values are extracted locally, including basic
arithmetic and unambiguous preceding scalar definitions such as `rpmBumpLow`.
This is a limited static reader, not a C++ compiler: function calls, unit
wrappers, preprocessor-dependent definitions, runtime values, and complex
initializers are unsupported. It refuses ambiguous or incomplete data instead
of fabricating points. Source points are marked; connecting lines are visual
guides, not a claim about the robot's interpolation or runtime behavior.
Each request supports up to three graphs, six tables per graph, and 500 points
per table. Rendering runs locally, and source data is not sent to a chart service.

## Repository research

Independent repository lookups in one model response run concurrently, with
up to four workers per mention. Each answer allows up to four tool rounds and
twelve distinct tool calls. Identical lookups reuse their results within a
request; a round that only repeats previous lookups ends research early.
After a search finds files, the next research turn offers `read_file` with the
exact matching paths as permitted choices. Invented paths are rejected before
calling GitHub. All tools become available again after a successful
read. The repository uses C++; code questions
need evidence from relevant headers, implementations, and referenced constants.
When research ends, one fresh model request receives the original question and
all executed lookup results as text, including errors and the last round's
results. That final request has no tool declarations or function-call history.
If it still requests a tool, returns no text, or fails, the bot asks for a more
specific class, file path, or method name. Intermediate text accompanying a
tool call is not used as the answer. Tools remain enabled during research;
no additional Slack or Gemini setting is needed to enable them.
Graph requests that use all research rounds can take one additional turn with
only the plotting tool before the final text answer, without more GitHub lookups.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `gemini` | Model provider (currently only Gemini). |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model available to your Gemini API key. |
| `GEMINI_TIMEOUT_SECONDS` | `30` | Timeout per model HTTP attempt. |
| `GEMINI_MAX_TOOL_ROUNDS` | `4` | Maximum rounds of repository lookups. |
| `SLACK_THREAD_HISTORY_LIMIT` | `20` | Maximum messages retrieved for thread context. |

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
