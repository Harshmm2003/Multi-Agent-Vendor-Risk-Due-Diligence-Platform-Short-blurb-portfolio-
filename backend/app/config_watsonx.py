import os
from langchain_ibm import ChatWatsonx
from dotenv import load_dotenv

load_dotenv()

# ── Credentials ────────────────────────────────────────
IBM_API_KEY      = os.environ["IBM_API_KEY"]
WATSONX_PROJECT  = os.environ["WATSONX_PROJECT_ID"]
WATSONX_URL      = os.environ.get(
    "WATSONX_URL", "https://us-south.ml.cloud.ibm.com"
)

# ── Model selection ────────────────────────────────────
# granite-3-8b-instruct  → fast, efficient, great for structured JSON
# granite-13b-instruct-v2 → more capable, use for complex reasoning
# DO NOT use: llama-3-405b, mistral-large, mixtral-8x7b (out of scope)

PRIMARY_MODEL = "meta-llama/llama-3-3-70b-instruct"
REASONING_MODEL = "meta-llama/llama-3-3-70b-instruct"


def get_llm(model_id: str = PRIMARY_MODEL, temperature: float = 0.0) -> ChatWatsonx:
    """
    Returns a ChatWatsonx LLM instance.
    temperature=0.0 for deterministic agent outputs (recommended).
    """
    return ChatWatsonx(
        model_id=model_id,
        url=WATSONX_URL,
        apikey=IBM_API_KEY,
        project_id=WATSONX_PROJECT,
        params={
            "temperature": temperature,
            "max_new_tokens": 1000,
            "decoding_method": "greedy",   # deterministic = better for JSON
            "repetition_penalty": 1.1,
        }
    )