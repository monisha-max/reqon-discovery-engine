import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # LLM
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # PostgreSQL
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://apple@localhost:5432/reqon")

    # Crawl
    MAX_PAGES: int = int(os.getenv("MAX_PAGES", "100"))
    MAX_DEPTH: int = int(os.getenv("MAX_DEPTH", "5"))
    CRAWL_TIMEOUT: int = int(os.getenv("CRAWL_TIMEOUT", "300"))

    # Output
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "output")

    # Performance Testing (Layer 3)
    PERF_TEST_LOAD_USERS: int = int(os.getenv("PERF_TEST_LOAD_USERS", "10"))
    PERF_TEST_LOAD_DURATION: int = int(os.getenv("PERF_TEST_LOAD_DURATION", "30"))
    PERF_TEST_STRESS_MAX_USERS: int = int(os.getenv("PERF_TEST_STRESS_MAX_USERS", "50"))
    PERF_TEST_STRESS_DURATION: int = int(os.getenv("PERF_TEST_STRESS_DURATION", "60"))
    PERF_TEST_SOAK_USERS: int = int(os.getenv("PERF_TEST_SOAK_USERS", "10"))
    PERF_TEST_SOAK_DURATION: int = int(os.getenv("PERF_TEST_SOAK_DURATION", "120"))


settings = Settings()
