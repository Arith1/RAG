"""路径脱敏工具：对外隐藏服务器本地绝对路径，统一为 uploads/ 相对口径。

原实现位于 api/main.py，抽到 utils 供接口层与缓存预热共用，
避免 chat_history.build_session_detail 反向依赖 api 层造成循环导入。
"""
import os

from rag个人知识库.service.oss_archive import UPLOAD_DIR

from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sanitize_source(path: str) -> str:
    """对外隐藏本地绝对路径；相对路径（uploads/{user_id}/文件）直接展示，不暴露服务器盘符。

    相对 source 是服务器环境下与 OSS 归档/下载一致的标准口径，脱敏后原样返回；
    绝对路径（存量/CLI 手动入库）则归一为 uploads/ 前缀的相对形式。
    """
    if not path:
        return path
    if path.startswith("uploads/"):
        return path  # 已是相对路径，直接展示
    abs_path = os.path.abspath(path)
    abs_upload = os.path.abspath(UPLOAD_DIR)
    if abs_path.startswith(abs_upload + os.sep):
        return "uploads/" + os.path.relpath(abs_path, abs_upload).replace(os.sep, "/")
    base = os.path.abspath(BASE_DIR)
    if abs_path.startswith(base + os.sep):
        return os.path.relpath(abs_path, base).replace(os.sep, "/")
    return os.path.basename(abs_path)


def resolve_upload_file_path(user_dir: str, raw_file_name: str) -> Optional[str]:
    """把上传文件名解析为用户目录内的落盘路径；非法或逃逸出目录时返回 None。

    - 统一转正斜杠后取最后一段，挡住 "../" 与 Windows 反斜杠目录穿越；
    - 盘符相对路径（如 "C:evil.pdf"）不含斜杠，上面的截断挡不住，而
      ntpath.join 会把它当作带盘符的路径直接丢弃 user_dir，必须显式拒绝冒号；
    - 最后用 commonpath 兜底校验仍在 user_dir 内（Windows 跨盘符会抛
      ValueError，一并按非法处理）。
    """
    name = (raw_file_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or ":" in name:
        return None
    user_root = os.path.abspath(user_dir)
    path = os.path.abspath(os.path.join(user_dir, name))
    if path == user_root:
        return None
    try:
        inside = os.path.commonpath([user_root, path]) == user_root
    except ValueError:
        return None
    return path if inside else None


def sanitize_source_paths(items: list) -> None:
    """就地脱敏 items 中的本地路径字段（source / metadata.source / metadata.md_path），
    兼容 chat 来源列表与检索命中列表，避免向客户端泄露服务器文件路径。"""
    for item in items:
        if item.get("source"):
            item["source"] = sanitize_source(item["source"])
        meta = item.get("metadata")
        if isinstance(meta, dict):
            if meta.get("source"):
                meta["source"] = sanitize_source(meta["source"])
            if meta.get("md_path"):
                meta["md_path"] = sanitize_source(meta["md_path"])
