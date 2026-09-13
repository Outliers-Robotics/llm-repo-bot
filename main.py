from collections import OrderedDict
import logging
import os
import re
from threading import Lock
from time import monotonic

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import positive_int_env
from llm.factory import get_llm
from prompts.system import SYSTEM_PROMPT
from slack_messages import split_markdown
from tools.github import search_repo, read_file
from tools.plots import PlotSession


class ThreadHistoryCache:
    """Thread-safe LRU cache storing active thread conversation turns in memory."""

    def __init__(self, max_threads: int = 200):
        self._cache: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        self._lock = Lock()
        self._max_threads = max_threads

    def get(self, thread_ts: str) -> list[dict[str, str]]:
        with self._lock:
            if thread_ts in self._cache:
                self._cache.move_to_end(thread_ts)
                return [dict(msg) for msg in self._cache[thread_ts]]
            return []

    def append(self, thread_ts: str, role: str, content: str):
        with self._lock:
            if thread_ts not in self._cache:
                if len(self._cache) >= self._max_threads:
                    self._cache.popitem(last=False)
                self._cache[thread_ts] = []
            self._cache[thread_ts].append({"role": role, "content": content})
            self._cache.move_to_end(thread_ts)

    def set(self, thread_ts: str, messages: list[dict[str, str]]):
        with self._lock:
            if len(self._cache) >= self._max_threads and thread_ts not in self._cache:
                self._cache.popitem(last=False)
            self._cache[thread_ts] = [dict(msg) for msg in messages]
            self._cache.move_to_end(thread_ts)

    def has(self, thread_ts: str) -> bool:
        with self._lock:
            return thread_ts in self._cache

    def clear(self):
        with self._lock:
            self._cache.clear()


THREAD_HISTORY_CACHE = ThreadHistoryCache()


class MessageDeduplicator:
    """Thread-safe LRU deduplicator for Slack event IDs or channel:ts."""

    def __init__(self, max_size: int = 1000):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = Lock()
        self._max_size = max_size

    def acquire(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            if len(self._seen) >= self._max_size:
                self._seen.popitem(last=False)
            self._seen[key] = monotonic()
            return True

    def clear(self):
        with self._lock:
            self._seen.clear()


DEDUPLICATOR = MessageDeduplicator()


def _ts_key(ts: str | float | None) -> float:
    try:
        return float(ts) if ts is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


def get_thread_history(
    client,
    channel: str,
    thread_ts: str,
    current_ts: str,
    bot_user_id: str | None = None,
    logger: logging.Logger | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, str]], str | None]:
    """Retrieve prior conversation messages from the Slack thread."""
    if limit is None:
        try:
            limit = positive_int_env("SLACK_THREAD_HISTORY_LIMIT", 20)
        except Exception:
            limit = 20

    error_code = None
    try:
        response = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=limit,
        )
        if not response:
            return [], None
        raw_messages = response.get("messages", []) if hasattr(response, "get") else []
        if not isinstance(raw_messages, list):
            return [], None
    except Exception as error:
        resp = getattr(error, "response", {})
        if isinstance(resp, dict) and resp.get("error") == "missing_scope":
            error_code = "missing_scope"
        elif hasattr(resp, "get") and resp.get("error") == "missing_scope":
            error_code = "missing_scope"
        else:
            error_code = "error"
        if logger:
            logger.warning(
                "Could not fetch Slack thread history for channel=%s thread_ts=%s (error=%s): %s",
                channel, thread_ts, error_code, error,
            )
        return [], error_code

    history: list[dict[str, str]] = []
    current_key = _ts_key(current_ts)
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        msg_ts = msg.get("ts")
        if msg_ts and _ts_key(msg_ts) >= current_key:
            continue
        text = (msg.get("markdown_text") or msg.get("text") or "").strip()
        if not text and isinstance(msg.get("blocks"), list):
            block_texts = []
            for b in msg["blocks"]:
                if isinstance(b, dict):
                    if b.get("type") == "section" and isinstance(b.get("text"), dict):
                        block_texts.append(b["text"].get("text", ""))
            text = "\n".join(t for t in block_texts if t).strip()

        if not text or text.startswith("I'm looking into that"):
            continue

        is_assistant = bool(
            msg.get("bot_id")
            or msg.get("subtype") == "bot_message"
            or (bot_user_id and msg.get("user") == bot_user_id)
        )
        if is_assistant:
            history.append({"role": "assistant", "content": text})
        else:
            cleaned = re.sub(r"<@[^>]+>", "", text).strip()
            if cleaned:
                history.append({"role": "user", "content": cleaned})

    return history, None


def handle_mention(
    event,
    say,
    client,
    logger,
    llm,
    bot_user_id: str | None = None,
    thread_cache: ThreadHistoryCache | None = None,
):
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    if bot_user_id is None:
        match = re.search(r"<@([A-Z0-9_]+)>", event.get("text", ""))
        if match:
            bot_user_id = match.group(1)

    question = re.sub(
        r"<@[^>]+>",
        "",
        event.get("text", ""),
    ).strip()

    if not question:
        return

    if thread_cache is None:
        thread_cache = THREAD_HISTORY_CACHE

    started = monotonic()
    thread_ts = event.get("thread_ts") or event["ts"]
    pending = None
    plots = PlotSession(read_file)
    try:
        pending = say(
            text="I'm looking into that…",
            thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("Could not post the Slack status message")

    history: list[dict[str, str]] = []
    missing_scope_notice = False
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        slack_history, history_err = get_thread_history(
            client=client,
            channel=event["channel"],
            thread_ts=event["thread_ts"],
            current_ts=event["ts"],
            bot_user_id=bot_user_id,
            logger=logger,
        )
        if slack_history:
            history = slack_history
            thread_cache.set(event["thread_ts"], slack_history)
        else:
            cached_history = thread_cache.get(event["thread_ts"])
            if cached_history:
                history = cached_history
            else:
                history = []
                if history_err == "missing_scope":
                    missing_scope_notice = True

    model_succeeded = False
    try:
        answer = llm.answer(
            question=question,
            system_prompt=SYSTEM_PROMPT,
            tools=[search_repo, plots.read_file, plots.plot_lookup_tables],
            history=history,
        )
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("The model did not return a non-empty answer")
        answer = answer.strip()
        model_succeeded = True

    except Exception:
        logger.exception("Could not answer Slack mention ts=%s", event["ts"])
        answer = (
            "I couldn't complete that request. "
            "Please try again or ask a more specific question."
        )

    # Store successful turns in the thread cache before appending error/scope notes
    if model_succeeded:
        thread_cache.append(thread_ts, "user", question)
        thread_cache.append(thread_ts, "assistant", answer)

    # Upload to the existing thread before composing the final delivery status.
    # Failure to attach a graph must still leave the user with the text answer.
    upload_error = None
    for chart in plots.artifacts:
        try:
            client.files_upload_v2(
                channel=event["channel"], thread_ts=thread_ts,
                file=chart.png, filename=chart.filename,
                title=chart.title, alt_txt=chart.alt_text,
            )
        except Exception as error:
            logger.exception("Could not upload graph for Slack mention ts=%s", event["ts"])
            response = getattr(error, "response", {})
            if response.get("error") == "missing_scope":
                upload_error = "missing_scope"
                break
            elif not upload_error:
                upload_error = "failed"

    if upload_error == "missing_scope":
        answer += (
            "\n\nI generated the graph, but Slack blocked the upload. "
            "Add the `files:write` bot scope under OAuth & Permissions "
            "in the Slack app settings, then reinstall the app to the workspace."
        )
    elif upload_error == "failed":
        answer += "\n\nI generated the graph, but its Slack upload failed. Please try again."

    if model_succeeded and missing_scope_notice and not history:
        answer += (
            "\n\n_Note: I couldn't load earlier messages in this thread because the Slack app "
            "is missing the `channels:history` (and `groups:history` for private channels) bot scope. "
            "Add these scopes under OAuth & Permissions in the Slack app settings and reinstall the app "
            "to enable persistent conversation history across bot restarts._"
        )

    try:
        messages = split_markdown(answer)
        if pending is not None:
            try:
                client.chat_update(
                    channel=event["channel"],
                    ts=pending["ts"],
                    markdown_text=messages[0],
                )
                messages = messages[1:]
            except Exception:
                logger.exception("Could not update Slack status; posting the answer separately")
        for message in messages:
            # The dictionary form avoids Say's default text="", which conflicts
            # with markdown_text. Use the same formatting for every delivery path.
            say({"markdown_text": message}, thread_ts=thread_ts)
    except Exception:
        logger.exception("Could not deliver the Slack answer ts=%s", event["ts"])
    finally:
        logger.info(
            "Slack mention ts=%s elapsed=%.2fs", event["ts"], monotonic() - started,
        )


def create_app(token_verification_enabled: bool = True):
    # Initialize network clients at startup, not when importing a listener.
    app = App(
        token=os.environ["SLACK_BOT_TOKEN"],
        token_verification_enabled=token_verification_enabled,
    )
    llm = get_llm()

    @app.event("app_mention")
    def mention_listener(event, say, client, logger, context=None):
        msg_id = f"{event.get('channel')}:{event.get('ts')}"
        if not DEDUPLICATOR.acquire(msg_id):
            return
        bot_user_id = None
        if context:
            bot_user_id = getattr(context, "bot_user_id", None) or (
                context.get("bot_user_id") if isinstance(context, dict) else None
            )
        handle_mention(event, say, client, logger, llm, bot_user_id=bot_user_id)

    @app.event("message")
    def message_listener(event, say, client, logger, context=None):
        # Ignore bot messages and non-standard message subtypes (e.g. edits, deletes, joins)
        if event.get("bot_id") or event.get("subtype"):
            return

        bot_user_id = None
        if context:
            bot_user_id = getattr(context, "bot_user_id", None) or (
                context.get("bot_user_id") if isinstance(context, dict) else None
            )
        if bot_user_id and event.get("user") == bot_user_id:
            return

        thread_ts = event.get("thread_ts")
        # Only handle thread replies in threads the bot is already participating in
        if not thread_ts or thread_ts == event.get("ts"):
            return
        if not THREAD_HISTORY_CACHE.has(thread_ts):
            return

        if not event.get("text", "").strip():
            return

        msg_id = f"{event.get('channel')}:{event.get('ts')}"
        if not DEDUPLICATOR.acquire(msg_id):
            return

        handle_mention(event, say, client, logger, llm, bot_user_id=bot_user_id)

    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    SocketModeHandler(
        create_app(),
        os.environ["SLACK_APP_TOKEN"],
    ).start()
