from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv(".env")

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

response = client.responses.create(
    model="gpt-5-mini",
    input="""
Analyze this stock setup.

Ticker: NVDA
Price: 216.10
SMA20: 215.71
SMA50: 199.45

Give:
- Recommendation
- Confidence 1-10
- Risk
- Short explanation
"""
)

print(response.output_text)
