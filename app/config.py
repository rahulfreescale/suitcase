"""Central settings, loaded from environment / .env."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate os.environ from .env so provider SDKs that read the environment
# directly — litellm (OPENAI_API_KEY, ANTHROPIC_API_KEY) and Langfuse — pick up
# keys placed in .env, not just our Settings object. Does not override real env vars.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deploy_profile: str = "local"

    # Cognito auth (only enforced when deploy_profile == "aws")
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""

    # AWS / Bedrock
    aws_region: str = "us-east-1"

    # Models
    llm_model_chain: str = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0,gpt-4o-mini"
    llm_fast_model: str = "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
    embed_model: str = "bedrock/amazon.titan-embed-text-v2:0"
    embed_dim: int = 1024
    llm_num_retries: int = 1
    llm_timeout_s: int = 30      # per-call timeout; a hung call fails fast instead of freezing

    # OpenSearch
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_use_ssl: bool = False
    opensearch_use_aws_auth: bool = False
    opensearch_index: str = "suitcase-guides"
    opensearch_user: str = "admin"
    opensearch_password: str = "Admin123!"

    # DynamoDB
    dynamodb_table: str = "suitcase-app-state"
    dynamodb_endpoint: str | None = "http://localhost:8000"

    # Postgres (checkpointer)
    postgres_dsn: str = "postgresql://suitcase:suitcase@localhost:5432/suitcase"

    # Structured store
    structured_backend: str = "local"  # local | athena
    travel_guides_dir: str = "data/travel_guides"
    flights_csv: str = "data/travel_guides/flights.csv"
    stays_csv: str = "data/travel_guides/stays.csv"
    athena_database: str = "suitcase"
    athena_table: str = "stays"
    athena_workgroup: str = "primary"
    athena_output_s3: str = ""
    s3_data_lake: str = ""

    # Reranker
    rerank_backend: str = "cross_encoder"  # cross_encoder | none
    rerank_model: str = "BAAI/bge-reranker-large"

    # Retrieval knobs
    expansion_n: int = 5
    hybrid_candidates: int = 20
    rerank_top_k: int = 7
    min_relevance_score: float = 0.0   # grounding gate: refuse if best chunk scores below this. 0.0 = off
    semantic_weight: float = 0.7
    keyword_weight: float = 0.3
    sql_row_limit: int = 50
    sql_max_retries: int = 3
    max_research_loops: int = 4
    max_reflection_loops: int = 2

    # Observability
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "http://localhost:3000"

    # external travel-data tools (all optional; specialists degrade gracefully)
    ors_api_key: str | None = None          # OpenRouteService (routing). No key -> routing skipped.
    enable_weather_tool: bool = True        # Open-Meteo (no key needed)
    enable_airquality_tool: bool = True     # Open-Meteo air quality (no key needed)
    enable_verifier_agent: bool = True      # 3rd agent: tool-using fact-checker (vs plain auditor)
    enable_routing_tool: bool = True        # requires ors_api_key to actually run
    enable_places_tool: bool = True         # OpenStreetMap/Overpass accessible places (no key)
    enable_holidays_tool: bool = True       # Nager.Date public holidays / closures (no key)

    # Live-traffic evaluation (daily batch)
    live_eval_sample_size: int = 25
    live_eval_min_faithfulness: float = 0.7
    sim_base_url: str = "http://localhost:8080"

    # --- Memory (Redis-backed) ---
    redis_url: str = "redis://localhost:6379/0"
    memory_enabled: bool = True
    memory_window_turns: int = 6          # recent turns kept verbatim in short-term memory
    memory_summarize_after: int = 6       # summarize older turns once a session exceeds this
    memory_session_ttl_s: int = 60 * 60 * 24 * 7   # session memory expires after 7 days
    memory_max_user_facts: int = 20       # cap durable long-term facts per user
    memory_extract_facts: bool = True     # after each turn, extract durable user facts (long-term)
    memory_resolve_in_code: bool = True   # resolve references in code BEFORE clarify (correct place — clarify is for ambiguity, not resolution)

    # --- Semantic cache (OpenSearch k-NN vector search) ---
    cache_enabled: bool = True
    cache_index: str = "semantic_cache"        # separate OpenSearch index for cached Q&A
    cache_similarity_threshold: float = 0.87   # cosine >= this = a hit. Tuned from data: paraphrases scored 0.89-0.92, unrelated 0.29 — 0.87 catches paraphrases with a wide safety margin.
    cache_ttl_s: int = 60 * 60 * 6             # cached answers expire after 6h (data may change)

    @property
    def model_chain(self) -> list[str]:
        return [m.strip() for m in self.llm_model_chain.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
