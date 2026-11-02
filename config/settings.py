"""Central configuration for Jupiter's local, user-scoped runtime."""

import os
import re
from pathlib import Path

# =====================================
# Project Paths
# =====================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
if getattr(__import__("sys"), "frozen", False):
    # A packaged app's BASE_DIR is PyInstaller's temporary extraction folder (deleted on exit): keep user data in the
    # permanent folder the host set (build_app.ensure_persistent_backend_data). Source/server runs are unchanged.
    _frozen_data = os.getenv("JUPITER_DATA_DIR", "").strip() or os.path.join(os.getenv("LOCALAPPDATA", ""), "Jupiter", "data")
    DATA_DIR = Path(_frozen_data)
    LOG_DIR = DATA_DIR.parent / "logs"
USERS_DIR = DATA_DIR / "users"
# Marker 216 (25 Oct: the owner's chats were split between production and the WT copy, and test runs wrote test accounts
# into the real data/users): one setting moves every account's chats / memories / profile. The test suite always
# points it at a throwaway folder (conftest), and Jupiter WT points it at production's data/users, so both apps show the
# same chats. A packaged app keeps its own data folder (above).
_USERS_OVERRIDE = os.getenv("JUPITER_USERS_DIR", "").strip()
if _USERS_OVERRIDE and not getattr(__import__("sys"), "frozen", False):
    USERS_DIR = Path(_USERS_OVERRIDE)


def _safe_user_id(value: str) -> str:
    """Keep a user identifier inside ``data/users`` on every platform."""

    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("._")
    return cleaned or "default"


# A different local user can be selected with JUPITER_USER_ID=alice.
USER_ID = _safe_user_id(os.getenv("JUPITER_USER_ID", "default"))
USER_DATA_DIR = USERS_DIR / USER_ID
PROFILE_PATH = USER_DATA_DIR / "profile.json"

DATABASE_PATH = USER_DATA_DIR / "memories.db"
VECTOR_DB_PATH = USER_DATA_DIR / "vectors"

# Automatically create folders. User data stays isolated by directory.
for directory in (DATA_DIR, USERS_DIR, USER_DATA_DIR, LOG_DIR, VECTOR_DB_PATH):
    directory.mkdir(parents=True, exist_ok=True)

# =====================================
# Application
# =====================================

APP_NAME = "Jupiter"

VERSION = "0.9"



# =====================================
# AI Models and Tasks
# =====================================

TASK_MODELS = {
    "MAIN_CHAT": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    "EXECUTIVE": ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"],
    "EMOTION": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    "PERSONALITY": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    "CURIOSITY": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    "RELATIONSHIP": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    "MEMORY_ANALYZER": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    "DUPLICATE": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    "CONTRADICTION": ["gemini-3.5-flash", "gemini-3.5-flash-lite"],
    "MEMORY_RETRIEVAL": ["gemini-embedding-2"],
    "VALIDATOR": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    "REWRITER": ["gemini-3.5-flash-lite", "gemini-3.5-flash"],
    "VOICE_CLEANUP": ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
    "TTS": ["gemini-3.1-flash-tts-preview"],
    "IMAGE_PROMPT": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
}

# Embedding Model
# text-embedding-004 was shut down by Google on 2026-01-14 (every embed call failed, so no index was ever built).
# gemini-embedding-001 is the STABLE Gemini embedding model (verified on ai.google.dev 2026-09-26).
LEGACY_EMBEDDING_MODEL = "text-embedding-004"
if os.getenv("JUPITER_LLM_BACKEND", "llama.cpp") == "gemini":
    EMBEDDING_MODEL = os.getenv("JUPITER_EMBEDDING_MODEL", "gemini-embedding-001")
else:
    EMBEDDING_MODEL = os.getenv("JUPITER_EMBEDDING_MODEL", "Qwen3-14B-Q5_K_M.gguf")

# LLM Backend (ollama, openai, gemini, llama.cpp)
LLM_BACKEND = os.getenv("JUPITER_LLM_BACKEND", "llama.cpp")

# =====================================
# LLM Settings
# =====================================

TEMPERATURE = 0.6

MAX_CONTEXT_MESSAGES = 50

# =====================================
# Vector Database
# =====================================

VECTOR_COLLECTION = "jupiter_memory"


def vector_collection_for(model: str) -> str:
    """One Chroma collection per embedding model: vectors from different models are never interchangeable, so a
    model change starts a NEW versioned collection and never overwrites or mixes the previous one."""
    if not model or model == LEGACY_EMBEDDING_MODEL:
        return VECTOR_COLLECTION
    slug = re.sub(r"[^a-z0-9]+", "_", str(model).casefold()).strip("_")[:40] or "model"
    return f"{VECTOR_COLLECTION}__{slug}"

# =====================================
# Memory Retrieval
# =====================================

MAX_RELEVANT_MEMORIES = 15

SEMANTIC_SEARCH_LIMIT = 20

KEYWORD_SEARCH_LIMIT = 20

# =====================================
# Memory Reflection
# =====================================

REFLECTION_THRESHOLD = 50

# Expiring memory lifetimes
EPISODIC_MEMORY_DAYS = 90
SHORT_TERM_MEMORY_DAYS = 20

# =====================================
# Memory Reasoning
# =====================================

MIN_REASONING_CONFIDENCE = 0.60

# =====================================
# Self-Reflection
# =====================================

ENABLE_SELF_REFLECTION = os.getenv("JUPITER_ENABLE_REFLECTION", "true").lower() != "false"

# =====================================
# Memory Ranking
# =====================================

TOPIC_MATCH_SCORE = 100

KEYWORD_MATCH_SCORE = 20

CRITICAL_IMPORTANCE_SCORE = 50
HIGH_IMPORTANCE_SCORE = 30
MEDIUM_IMPORTANCE_SCORE = 15
LOW_IMPORTANCE_SCORE = 5

CONFIDENCE_MULTIPLIER = 20

USAGE_SCORE = 2

RECENCY_MAX_BONUS = 30
