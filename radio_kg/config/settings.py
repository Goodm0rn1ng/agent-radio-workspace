from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM
    llm_provider: Literal["anthropic", "openai", "deepseek", "mimo"] = "anthropic"
    llm_model: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.mimo-v2.com/v1"

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password"

    # MCP servers
    mcp_neo4j_command: str = "uvx"
    mcp_neo4j_args: str = "mcp-neo4j-cypher"
    mcp_chroma_command: str = "uvx"
    mcp_chroma_args: str = "chroma-mcp"

    # Storage
    chroma_path: str = "./data/chroma"
    vector_embedding_model: str = "intfloat/multilingual-e5-base"
    vector_collection: str = ""
    vector_batch_size: int = 16
    qa_answer_max_tokens: int = 8192
    qa_structured_answer: bool = True  # fact->source_id->citation + verify
    qa_vector_k: int = 32
    qa_top_n: int = 48
    qa_summary_k: int = 24
    qa_direct_k: int = 16
    qa_fallback_k: int = 32
    checkpoint_db: str = "./data/checkpoints.sqlite"
    pending_dir: str = "./data/pending"
    conversations_db: str = "./data/conversations.sqlite"

    # Source data
    radio_data_dir: str = "../Radio/data/recordings"
    program_name: str = "羊宮妃那のこもれびじかん"

    def abspath(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (ROOT / path).resolve()

    @property
    def default_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return {
            "anthropic": "claude-opus-4-7",
            "openai": "gpt-4o",
            "deepseek": "deepseek-chat",
            "mimo": "mimo-v2.5-pro",
        }[self.llm_provider]

    @property
    def effective_vector_collection(self) -> str:
        if self.vector_collection:
            return self.vector_collection
        model = self.vector_embedding_model
        if model == "default":
            return "radio_chunks"
        slug = (
            model.replace("/", "_")
            .replace("-", "_")
            .replace(".", "_")
            .lower()
        )
        return f"radio_chunks_{slug}"


settings = Settings()
