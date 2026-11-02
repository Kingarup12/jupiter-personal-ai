from pathlib import Path

from config.settings import DATA_DIR, EMBEDDING_MODEL, USERS_DIR, _safe_user_id, vector_collection_for
from core.logger import Logger
from memory.embedding import EmbeddingGenerator


class VectorStore:

    def __init__(self, user_id="default", read_only=False, path=None):
        self.available = False
        self.client = None
        self.collection = None
        self.embedding = None
        vector_path = Path(path or (USERS_DIR / _safe_user_id(user_id) / "vectors"))

        # A read-only request may open an existing user-scoped collection, but
        # it must never create a directory or collection as a side effect.
        if read_only and not (vector_path / "chroma.sqlite3").is_file():
            return

        try:
            import chromadb

            self.client = chromadb.PersistentClient(
                path=str(vector_path)
            )
            # Versioned per embedding model (see vector_collection_for); legacy collections are left untouched.
            self.collection_name = vector_collection_for(EMBEDDING_MODEL)
            if read_only:
                try:
                    self.collection = self.client.get_collection(name=self.collection_name)
                except Exception:
                    return  # the versioned index has not been built yet; read-only never creates it
            else:
                self.collection = self.client.get_or_create_collection(
                    name=self.collection_name, metadata={"embedding_model": str(EMBEDDING_MODEL)})
            self.embedding = EmbeddingGenerator()
            self.available = True
        except Exception as error:
            # Chroma uses optional native dependencies.  If Windows blocks one
            # of them, regular chat and SQLite-backed memory must still work.
            Logger.warning(f"Semantic memory index unavailable; using SQLite memory only: {error}")

    # ----------------------------------
    # Add Memory
    # ----------------------------------

    def add(self, memory):

        if not self.available:
            return

        vector = self.embedding.generate(memory.summary)

        self.collection.add(

            ids=[memory.memory_id],

            embeddings=[vector],

            documents=[memory.summary],

            metadatas=[

                {
                    "category": memory.category,
                    "topic": memory.topic,
                    "domain": memory.domain,
                    "emotion": memory.emotion,
                    "importance": memory.importance,
                    "retention": memory.retention,
                    "visibility": memory.visibility
                }

            ]

        )

    # ----------------------------------
    # Update Memory
    # ----------------------------------

    def update(self, memory):

        if not self.available:
            return

        vector = self.embedding.generate(memory.summary)

        self.collection.update(

            ids=[memory.memory_id],

            embeddings=[vector],

            documents=[memory.summary],

            metadatas=[

                {
                    "category": memory.category,
                    "topic": memory.topic,
                    "domain": memory.domain,
                    "emotion": memory.emotion,
                    "importance": memory.importance,
                    "retention": memory.retention,
                    "visibility": memory.visibility
                }

            ]

        )

    # ----------------------------------
    # Exists
    # ----------------------------------

    def exists(self, memory_id):

        if not self.available:
            return False

        result = self.collection.get(
            ids=[memory_id]
        )

        return len(result["ids"]) > 0

    # ----------------------------------
    # Get Memory
    # ----------------------------------

    def get(self, memory_id):

        if not self.available:
            return {"ids": []}

        return self.collection.get(
            ids=[memory_id]
        )

    # ----------------------------------
    # Delete
    # ----------------------------------

    def delete(self, memory_id):

        if not self.available:
            return

        self.collection.delete(
            ids=[memory_id]
        )

    # ----------------------------------
    # Semantic Search
    # ----------------------------------

    def search(self, query, n_results=5):

        if not self.available:
            return {"ids": [[]]}

        vector = self.embedding.generate(query)

        return self.collection.query(

            query_embeddings=[vector],

            n_results=n_results

        )

    # ----------------------------------
    # Number of Memories
    # ----------------------------------

    def count(self):

        if not self.available:
            return 0

        return self.collection.count()

    # ----------------------------------
    # Delete Everything (Testing)
    # ----------------------------------

    def reset(self):

        if not self.available:
            return

        # Testing-only: resets the CURRENT model's versioned collection; other models' collections are untouched.
        self.client.delete_collection(
            self.collection_name
        )

        self.collection = self.client.get_or_create_collection(
            name=self.collection_name, metadata={"embedding_model": str(EMBEDDING_MODEL)}
        )
