import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
from sentence_transformers import SentenceTransformer

class EmbeddingService:
    _instance = None
    _model = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EmbeddingService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._model is None:
            import logging
            logger = logging.getLogger(__name__)
            logger.info("Loading Embedding Model: shibing624/text2vec-base-chinese...")
            # Use CPU for general compatibility, or CUDA if available
            self._model = SentenceTransformer('shibing624/text2vec-base-chinese', device='cpu')
            logger.info("Embedding Model loaded successfully.")

    def get_embedding(self, text: str) -> list:
        if not text:
            return [0.0] * 768
        # The model encode returns a numpy array, convert to list
        vector = self._model.encode(text)
        return vector.tolist()
        
    def get_embeddings(self, texts: list) -> list:
        if not texts:
            return []
        vectors = self._model.encode(texts)
        return vectors.tolist()

# Global instance
embedding_service = EmbeddingService()

def get_embedding(text: str) -> list:
    return embedding_service.get_embedding(text)

def get_embeddings(texts: list) -> list:
    return embedding_service.get_embeddings(texts)
