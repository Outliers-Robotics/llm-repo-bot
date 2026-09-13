import logging
import os
import re
from time import monotonic

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from llm.factory import get_llm
from prompts.system import SYSTEM_PROMPT
from slack_messages import split_markdown
from tools.github import search_repo, read_file

TOOLS = [
    search_repo,
    read_file,
]


def handle_mention(event, say, client, logger, llm):
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

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
    try:
        pending = say(
            text="I'm looking into that…",
            thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("Could not post the Slack status message")

    try:
        answer = llm.answer(
            question=question,
            system_prompt=SYSTEM_PROMPT,
            tools=TOOLS,
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
    def mention_listener(event, say, client, logger):
        handle_mention(event, say, client, logger, llm)

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
