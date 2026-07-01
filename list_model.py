from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv(".env")

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

for model in client.models.list():
    print(model.id)
