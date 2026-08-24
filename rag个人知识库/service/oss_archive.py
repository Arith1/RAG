"""文档原件归档：本地 upload 作为临时中转，入库成功后归档到阿里云 OSS，成功后再删本地原件。

链路（与产品约定一致）：
  上传(按用户分目录 /uploads/{user_id}/) → 入库 → 归档 OSS → 删除本地 upload 原件

设计要点：
  - 删除时机锚定在「入库 + OSS 归档都成功」之后；任何中途失败本地原件仍保留，可重试。
  - source 统一为相对路径 `uploads/{user_id}/{file_name}`，且 OSS key 即该相对路径
    （下载 URL = 前缀 + 相对路径，无需额外映射表；metadata.source 也存相对地址）。
  - 未配置 OSS（或未安装 oss2）时自动降级为「归档视为成功但保留本地原件」，
    绝不误删用户文件。

只在 .env 配置了 OSS_ENABLE=true 且具备 Endpoint/Key/Bucket 时才真正走阿里云 OSS。
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── 上传根目录（与 api/main.py 一致，后续统一收敛到这里）──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "").rstrip("/\\") or os.path.join(
    BASE_DIR, "resources", "uploads"
)

# ── OSS 配置（全部走 .env，绝不下发前端）──
OSS_ENABLE = os.getenv("OSS_ENABLE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "")           # 如 https://oss-cn-hangzhou.aliyuncs.com
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_BUCKET = os.getenv("OSS_BUCKET", "")
# 下载 URL：私有桶时留空（用签名 URL）；公有读桶可填 https://{bucket}.{endpoint} 或 CDN 域名
OSS_PUBLIC_BASE = os.getenv("OSS_PUBLIC_BASE", "").rstrip("/")
# OSS 对象 key 前缀：上传到 bucket 下 rag-project/ 目录，避免与同 bucket 其他业务混淆
# OSS key = OSS_KEY_PREFIX + source（如 rag-project/uploads/1/a.md）
OSS_KEY_PREFIX = os.getenv("OSS_KEY_PREFIX", "rag-project/").strip("/")


def oss_key_from_source(source: str) -> str:
    """把相对 source 转换为 OSS 对象 key（加上前缀，如 rag-project/uploads/{uid}/file）。"""
    src = source.lstrip("/")
    if src.startswith("uploads/"):
        return f"{OSS_KEY_PREFIX}/{src}" if OSS_KEY_PREFIX else src
    return source


def is_oss_enabled() -> bool:
    """是否真正启用阿里云 OSS 归档（配置齐全才为 True）。"""
    if not OSS_ENABLE:
        return False
    if not all([OSS_ENDPOINT, OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET]):
        logger.warning("[oss_archive] OSS_ENABLE=true 但缺少 Endpoint/AK/SK/Bucket 配置，降级为本地保留")
        return False
    return True


def rel_source_from_local(file_path: str) -> str:
    """把 upload 目录下的本地绝对路径转换为相对 source：uploads/{user_id}/{file_name}。

    仅当路径位于 UPLOAD_DIR 内才转换（API 上传场景）；
    非 upload 目录（如 CLI 手动入库）保持原路径不变，不破坏既有指纹口径。
    """
    upload = os.path.abspath(UPLOAD_DIR)
    fp = os.path.abspath(file_path)
    if fp.startswith(upload + os.sep):
        return "uploads/" + os.path.relpath(fp, upload).replace(os.sep, "/")
    return file_path


def _bucket():
    """惰性构造 OSS Bucket（延迟 import oss2，避免未安装时整个模块 import 失败）。"""
    import oss2
    auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    return oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)


async def archive_local_file(local_path: str) -> bool:
    """入库成功后归档原件：上传到 OSS 并删除本地。返回 True 表示已妥善归档。

    返回 True 时上游可安全删除本地；返回 False 表示归档失败，本地原件保留（可重试）。
    """
    if not os.path.isfile(local_path):
        return True
    source = rel_source_from_local(local_path)
    key = oss_key_from_source(source)  # 带前缀：rag-project/uploads/{uid}/file
    if not is_oss_enabled():
        logger.info("[oss_archive] OSS 未启用（或未配置），保留本地原件：%s", local_path)
        return True  # 降级：不删本地，绝不丢原件
    try:
        def _upload():
            # 同步函数：asyncio.to_thread 在线程池中执行，返回真实结果
            _bucket().put_object_from_file(key, local_path)
        await asyncio.to_thread(_upload)
        os.remove(local_path)
        logger.info("[oss_archive] 已归档到 OSS（key=%s）并删除本地原件：%s", key, local_path)
        return True
    except Exception as e:
        logger.warning("[oss_archive] OSS 上传失败，保留本地原件：%s（%s）", local_path, e)
        return False


async def delete_source_artifact(source: str) -> bool:
    """删除文档时同步删除 OSS 中的原件（source 为相对路径，OSS key 带前缀）。

    返回 True 表示该 source 不需要删 OSS 或已成功删除；False 表示删除失败，
    调用方（如账户删除队列）应视为失败，不能继续删除 MySQL 用户。
    原有文档删除逻辑会忽略返回值，因此该改动向后兼容。
    """
    if not source or not source.startswith("uploads/"):
        return True
    if not is_oss_enabled():
        return True
    key = oss_key_from_source(source)
    try:
        def _del():
            # 同步函数：删除 OSS 对象
            _bucket().delete_object(key)
        await asyncio.to_thread(_del)
        logger.info("[oss_archive] 已从 OSS 删除对象：%s", key)
        return True
    except Exception as e:
        logger.warning("[oss_archive] OSS 删除失败（将重试）：%s（%s）", key, e)
        return False


async def build_download_url(source: str, ttl: int = 3600) -> str:
    """根据相对 source 生成下载 URL（OSS key 带前缀）。
    优先 OSS 签名 URL（私有桶）；否则用 OSS_PUBLIC_BASE 直接拼接；都没有返回空串。
    """
    if not source:
        return ""
    key = oss_key_from_source(source)
    if is_oss_enabled():
        try:
            def _sign():
                # 同步函数：生成 OSS 签名 URL
                return _bucket().sign_url("GET", key, ttl)
            return await asyncio.to_thread(_sign)
        except Exception as e:
            logger.warning("[oss_archive] 生成 OSS 签名 URL 失败：%s", e)
    if OSS_PUBLIC_BASE:
        from urllib.parse import quote
        return OSS_PUBLIC_BASE + "/" + quote(key, safe="/")
    return ""


def local_source_exists(source: str) -> str:
    """按相对 source 反查本地原件是否存在，存在返回本地绝对路径，否则空串。
    用于 OSS 未启用时仍能从本地提供下载。
    """
    if not source or not source.startswith("uploads/"):
        return ""
    rel = source[len("uploads/"):]
    local = os.path.join(UPLOAD_DIR, rel.replace("/", os.sep))
    return local if os.path.isfile(local) else ""
