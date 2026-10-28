"""Run after starting the mock and gateway using the README commands."""
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["GREENROUTE_GATEWAY_KEY"], base_url="http://127.0.0.1:8000/v1", max_retries=0)
response = client.chat.completions.with_raw_response.create(
    model="demo-model", messages=[{"role":"user", "content":"Summarize the benefits of flexible inference scheduling."}])
print(response.parse().choices[0].message.content)
print("Simulated region:", response.headers["x-greenroute-region"])
print("Request ID:", response.headers["x-greenroute-request-id"])
