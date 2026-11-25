"""Azure smoke test (synthetic text only).

Checks: Entra ID auth (no keys), the pinned deployment, temperature 0 + seed accepted,
strict structured outputs, and whether 3 identical calls return identical output.

Run:  uv run --env-file .env python scripts/smoke_azure.py
"""

import asyncio
import os

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hemoglobin", "unit"],
    "properties": {
        "hemoglobin": {"type": ["string", "null"]},
        "unit": {"type": ["string", "null"]},
    },
}
SYNTHETIC_TEXT = "Synthetic lab report. Patient MRN 00123. Hemoglobin 13.4 g/dL."


async def main() -> None:
    credential = DefaultAzureCredential()
    client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        azure_ad_token_provider=get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        ),
    )
    outputs = []
    for _ in range(3):
        resp = await client.chat.completions.create(
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            temperature=0,
            seed=int(os.environ.get("MODEL_SEED", "7")),
            messages=[
                {"role": "system", "content": "Extract hemoglobin (digits only) and its unit."},
                {"role": "user", "content": SYNTHETIC_TEXT},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "smoke", "strict": True, "schema": SCHEMA},
            },
        )
        outputs.append(resp.choices[0].message.content)
        print(f"model={resp.model} fingerprint={resp.system_fingerprint} -> {outputs[-1]}")
    print("IDENTICAL" if len(set(outputs)) == 1 else "DIFFERENT (check model/settings)")
    await client.close()
    await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
