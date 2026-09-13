import os

from google import genai

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)

chat = client.chats.create(
    model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
)

response = chat.send_message("Respond with exactly: Gemini works")

print(response.text)
