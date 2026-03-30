from __future__ import annotations

from functools import lru_cache

from config.settings import settings


@lru_cache
def get_intelligence_store():
    from intelligence.repositories.neo4j_store import Neo4jIntelligenceStore

    return Neo4jIntelligenceStore(
        uri=settings.NEO4J_URI,
        username=settings.NEO4J_USERNAME,
        password=settings.NEO4J_PASSWORD,
        database=settings.NEO4J_DATABASE,
    )
