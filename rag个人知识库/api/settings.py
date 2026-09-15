"""HTTP layer configuration shared by routers and lifecycle."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_UPLOAD_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
MAX_BATCH_UPLOAD = int(os.getenv("MAX_BATCH_UPLOAD", "10"))
ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}
CHAT_MAX_REQUESTS_PER_MINUTE = int(os.getenv("CHAT_MAX_REQUESTS_PER_MINUTE", "10"))
SEARCH_MAX_REQUESTS_PER_MINUTE = int(os.getenv("SEARCH_MAX_REQUESTS_PER_MINUTE", "30"))
REGISTRATION_ENABLED = os.getenv("REGISTRATION_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
REGISTER_MAX_REQUESTS_PER_MINUTE = int(os.getenv("REGISTER_MAX_REQUESTS_PER_MINUTE", "5"))
UPLOAD_MAX_REQUESTS_PER_HOUR = int(os.getenv("UPLOAD_MAX_REQUESTS_PER_HOUR", "30"))
SSE_HEARTBEAT_SECONDS = max(1.0, float(os.getenv("SSE_HEARTBEAT_SECONDS", "15")))
DELETE_GRACE_DAYS = int(os.getenv("DELETE_GRACE_DAYS", "7"))
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in ("1", "true", "yes", "on")
_ENV = os.getenv("APP_ENV", "development").strip().lower()
DOCS_ENABLED = not (_ENV in ("production", "prod") or os.getenv("DISABLE_DOCS", "").strip().lower() in ("1", "true", "yes", "on"))
CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
