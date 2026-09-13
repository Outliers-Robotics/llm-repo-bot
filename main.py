import logging
import os
import re
from time import monotonic

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import positive_int_env
from llm.factory import get_llm
from prompts.system import SYSTEM_PROMPT
from slack_messages import split_markdown
from tools.github import search_repo, read_file
from tools.plots import PlotSession


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
) -> list[dict[str, str]]:
    """Retrieve prior conversation messages from the Slack thread."""
    if limit is None:
        try:
            limit = positive_int_env("SLACK_THREAD_HISTORY_LIMIT", 20)
        except Exception:
            limit = 20

    try:
        response = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=limit,
        )
        if not response:
            return []
        raw_messages = response.get("messages", []) if hasattr(response, "get") else []
        if not isinstance(raw_messages, list):
            return []
    except Exception as error:
        if logger:
            logger.warning(
                "Could not fetch Slack thread history for channel=%s thread_ts=%s: %s",
                channel, thread_ts, error,
            )
        return []

    history: list[dict[str, str]] = []
    current_key = _ts_key(current_ts)
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        msg_ts = msg.get("ts")
        if msg_ts and _ts_key(msg_ts) >= current_key:
            continue
        text = (msg.get("text") or "").strip()
        if not text or text == "I'm looking into that…":
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

    return history


def handle_mention(event, say, client, logger, llm, bot_user_id: str | None = None):
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    if bot_user_id is None:
        match = re.search(r"<@([A-Z0-9_]+)>", event.get("text", ""))
        if match:
            bot_user_id = match.group(1)

    question = re.sub(
        r"<@[^>]+>",
        "",
        event["text"],
    ).strip()

    if not question:
        return

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

    history = []
    if event.get("thread_ts") and event["thread_ts"] != event["ts"]:
        history = get_thread_history(
            client=client,
            channel=event["channel"],
            thread_ts=event["thread_ts"],
            current_ts=event["ts"],
            bot_user_id=bot_user_id,
            logger=logger,
        )

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

    except Exception:
        logger.exception("Could not answer Slack mention ts=%s", event["ts"])
        answer = (
            "I couldn't complete that request. "
            "Please try again or ask a more specific question."
        )

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


def create_app():
    # Initialize network clients at startup, not when importing a listener.
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    llm = get_llm()

    @app.event("app_mention")
    def mention_listener(event, say, client, logger, context=None):
        bot_user_id = None
        if context:
            bot_user_id = getattr(context, "bot_user_id", None) or (
                context.get("bot_user_id") if isinstance(context, dict) else None
            )
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
