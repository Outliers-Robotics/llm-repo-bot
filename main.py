import os
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from llm.factory import get_llm
from prompts.system import SYSTEM_PROMPT
from tools.github import search_repo, read_file

app = App(
    token=os.environ["SLACK_BOT_TOKEN"]
)

llm = get_llm()

TOOLS = [
    search_repo,
    read_file,
]


@app.event("app_mention")
def handle_mention(event, say):

    question = re.sub(
        r"<@[^>]+>",
        "",
        event["text"],
    ).strip()

    if not question:
        return

    try:
        answer = llm.answer(
            question=question,
            system_prompt=SYSTEM_PROMPT,
            tools=TOOLS,
        )

    except Exception as error:
        print(error)
        answer = (
            "I couldn't complete that request. "
            "Check the bot logs."
        )

    say(
        answer,
        thread_ts=event.get(
            "thread_ts",
            event["ts"],
        ),
    )


if __name__ == "__main__":
    SocketModeHandler(
        app,
        os.environ["SLACK_APP_TOKEN"],
    ).start()
