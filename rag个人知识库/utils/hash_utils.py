"""文件与分块指纹工具（MySQL 元数据 与 Milvus 主键共用同一口径）。

统一口径：
- file_content_hash：SHA256(文件原始字节)，判断源文件内容是否变化
- identity_hash：SHA256(file_name|source)，vector_files 的唯一身份键
- chunk_fingerprint：SHA256(source|content)，chunk_records 的唯一键，
  同时作为 Milvus 的主键 ID，保证同一 chunk 反复入库 ID 不变
"""
import hashlib


def compute_file_hash(file_path: str) -> str:
    """计算文件内容的 SHA256（流式读取，避免大文件一次性载入内存）"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(block)
    return sha256.hexdigest()


def compute_identity_hash(file_name: str, source: str) -> str:
    """同名同源文件的唯一身份指纹"""
    raw = f"{file_name}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_chunk_fingerprint(content: str, source: str) -> str:
    """chunk 内容指纹：source + 正文，正文未变的 chunk 指纹保持不变"""
    raw = f"{source}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()