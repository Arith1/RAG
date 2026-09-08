import inspect
import logging
import os
import shutil
from typing import Optional, List

from langchain_community.document_loaders import (
    UnstructuredWordDocumentLoader,
    TextLoader)
from langchain_core.documents import Document

# from rag个人知识库.demo.mineru_demo import minerU_files
from rag个人知识库.loader.parser.document_validation_exception import DocumentValidationErrorType, \
    DocumentValidationError
from rag个人知识库.loader.parser.mineru_parser import minerU_files
from rag个人知识库.loader.parser.word_parser import word_complicatedness, docx_has_images, COMPLEXITY_THRESHOLD

logger = logging.getLogger(__name__)

# 文件大小阈值（单位：字节），超过此值使用 lazy_load 避免内存溢出
# 默认 10MB
LARGE_FILE_THRESHOLD = int(os.getenv("LARGE_FILE_THRESHOLD", 5 * 1024 * 1024))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))


def smart_load(loader, file_path: str) -> List[Document]:
    """
    根据文件大小智能选择加载方式：
      - 文件 <= LARGE_FILE_THRESHOLD：使用 load()，一次性返回 List[Document]
      - 文件 > LARGE_FILE_THRESHOLD：使用 lazy_load()，返回 Iterator[Document]，逐条处理不占内存
    """
    file_size = os.path.getsize(file_path)
    if file_size > LARGE_FILE_THRESHOLD:
        logger.info("[smart_load] 文件较大(%.1fMB)，使用 lazy_load 逐条加载: %s", file_size / 1024 / 1024, file_path)
        documents = [document for document in loader.lazy_load()]
        return documents
    else:
        logger.info("[smart_load] 文件大小正常(%.1fKB)，使用 load 直接加载: %s", file_size / 1024, file_path)
        return loader.load()


def needs_mineru(file_path: str) -> bool:
    """判断文件是否需要走 MinerU 解析（PDF 始终需要，Word 按复杂度判断）。

    与 validate_file 的校验口径一致：旧版二进制 .doc 在此显式拒绝
    （本函数可能被绕过 validate_file 直接调用，不能静默当作简单文档处理）。
    """
    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return True
    if ext == "doc":
        raise ValueError(
            f"暂不支持旧版 .doc 格式：{file_path}；请用 WPS/Word 另存为 .docx 再入库"
        )
    # .docx 按复杂度判断；其他后缀（md/txt）走本地解析
    if ext == "docx":
        return word_complicatedness(file_path) >= COMPLEXITY_THRESHOLD
    return False


def _cleanup_mineru_output_dir(output_dir: Optional[str]) -> None:
    """best-effort 删除 MinerU 解析产物目录（H4：磁盘泄漏修复）。

    产物（full.md + images/ + content_list.json）只在加载时消费一次，
    读取后立即清理，避免 uploads/{uid}/mineru_results 随每次解析永久堆积。
    失败只记日志，绝不影响入库主流程。
    """
    if not output_dir:
        return
    try:
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
            logger.info("[load_file] 已清理 MinerU 解析产物目录：%s", output_dir)
    except Exception as e:
        logger.warning("[load_file] 清理 MinerU 产物目录失败（不影响入库）：%s（%s）", output_dir, e)


def load_mineru_md_from_result(
    file_path: str,
    result: Optional[dict],
    doc_type_label: str,
) -> Optional[List[Document]]:
    """读取 MinerU 已解析结果中的 Markdown，返回 Document 列表。

    H4：产物目录只在本函数消费一次，读取后（无论成败）在 finally 中
    best-effort 清理——全项目唯一消费点，批量（ingest_files_batched）与
    单文件（load_mineru_md）两条路径都经过这里，一处修复覆盖全部。
    """
    output_dir = (result or {}).get("output_dir")
    try:
        if result is None or result.get("status") != "success":
            error = (result or {}).get("error") or "未返回解析结果"
            logger.warning("%s 解析失败：%s，原因：%s", doc_type_label, file_path, error)
            return None

        # md_path 由 MinerU 批量结果直接给出，无需自己拼产物目录结构
        md_path = result.get("md_path")
        if not md_path or not os.path.exists(md_path):
            logger.warning("未找到解析结果：%s", md_path)
            return None
        logger.info("读取解析结果：%s", md_path)
        # 产物是 Markdown，按原文读入，切分交给切分层
        documents = Loader.load_md(md_path)
        if documents:
            for doc in documents:
                # source 指回原始文件便于溯源，解析产物路径另存 md_path
                doc.metadata["source"] = file_path
                doc.metadata["md_path"] = md_path
        logger.info("成功加载文档：%s", md_path)
        return documents
    finally:
        _cleanup_mineru_output_dir(output_dir)


def load_mineru_md(file_path: str, doc_type_label: str) -> Optional[List[Document]]:
    """
    调用 MinerU 解析文件，并读取解析产物 Markdown 为 Document 列表。

    流程：
      1. 调用 minerU_files 解析，失败或产物缺失直接返回 None
      2. 产物是 Markdown，按原文读入，切分交给切分层
      3. 给每个 Document 打上 source（原始文件）与 md_path（解析产物）元数据便于溯源

    doc_type_label 用于日志提示，如 "Word" / "PDF"。
    """
    # minerU_files 接收路径列表，返回以原始路径为 key 的结构化结果
    results = minerU_files([file_path])
    result = results.get(file_path)
    return load_mineru_md_from_result(file_path, result, doc_type_label)


def load_word(file_path: str) -> Optional[List[Document]]:
    """
    加载 Word 文档。

    流程：
      1. 调用 word_complicatedness 评估文档复杂度
      2. 简单文档 → UnstructuredWordDocumentLoader 直接解析（快、免费、离线）
      3. 复杂文档 → 上传 MinerU 解析（图片/公式/图表本地解析易丢失），读产物 full.md
    """
    logger.info("正在分析 Word 文档复杂度：%s", file_path)
    score = word_complicatedness(file_path)

    if score < COMPLEXITY_THRESHOLD and not docx_has_images(file_path):
        # ── 简单文档且无图片：docx 本身结构化，本地解析可靠且不消耗解析额度。
        #    M20：含图文档即使得分 <3 也走 MinerU（Unstructured 本地解析会静默丢图）
        logger.info("复杂度较低且无图片，使用 UnstructuredWordDocumentLoader 直接解析")
        loader = UnstructuredWordDocumentLoader(
            file_path=file_path,
            mode="single",
        )
        documents = loader.load()
        logger.info("成功加载文档：%s", file_path)
        total_len = sum(len(d.page_content) for d in documents)
        logger.info("文档信息：共 %d 段，总字符数：%d", len(documents), total_len)
        return documents

    # ── 复杂文档：走 MinerU 解析 ──
    logger.info("复杂度较高，使用 MinerU 进行解析")
    return load_mineru_md(file_path, "Word")


class Loader:


    # @staticmethod
    # def load_doc(file_path):  # Word 文件加载器
    #     documents = load_word(file_path)
    #     return documents

    @staticmethod
    def load_docx(file_path):  # Word 文件加载器
        documents = load_word(file_path)
        return documents

    @staticmethod
    def load_md(file_path)-> Optional[List[Document]]:
        # Markdown 文件加载器
        # 不用 UnstructuredMarkdownLoader：它会剥掉 # 等 Markdown 语法（single 模式也一样），
        # 导致切分层的 MarkdownHeaderTextSplitter 识别不到标题层级。
        # 直接按原文读入，保留完整 Markdown 结构，切分职责完全交给切分层
        try:
            loader = TextLoader(file_path, encoding="utf-8")
            documents = smart_load(loader, file_path)
        except UnicodeDecodeError:
            loader = TextLoader(file_path, encoding="gbk")
            documents = smart_load(loader, file_path)
        # 打上 doc_type 标记，切分入口 split_documents 据此分发结构感知切分策略
        for doc in documents:
            doc.metadata["doc_type"] = "markdown"
        return documents

    @staticmethod
    def load_pdf(file_path) -> Optional[List[Document]]:
        # PDF 文件加载器
        """上传 PDF 到 MinerU 解析，解析完成后读取 full.md 返回 Document 列表"""
        return load_mineru_md(file_path, "PDF")


    @staticmethod
    def load_txt(file_path)-> Optional[List[Document]]:
        # TXT 文件加载器：解码发生在 smart_load 的 load() 阶段，try 必须包住 smart_load
        try:
            loader = TextLoader(file_path, encoding="utf-8")
            documents = smart_load(loader, file_path)
        except UnicodeDecodeError:
            loader = TextLoader(file_path, encoding="gbk")
            documents = smart_load(loader, file_path)
        return documents


# ── M5：上传内容嗅探（防伪装扩展名 / 恶意内容进解析库）──
# 只读文件头做 magic-byte 校验，与扩展名白名单互相印证：
#   - pdf：前 1024 字节须含 %PDF（PDF 规范允许头部在文件前 1KB，容忍 BOM/前导空白）
#   - docx：须为 zip 且含 [Content_Types].xml；限制 zip 成员数 / 解压总大小（防解压炸弹）
#   - txt/md：拒绝含 NUL 字节的二进制伪装
_PDF_HEADER = b"%PDF"
_DOCX_ZIP_MARK = b"PK\x03\x04"
_DOCX_MAX_MEMBERS = int(os.getenv("DOCX_MAX_MEMBERS", "10000"))
_DOCX_MAX_UNCOMPRESSED = int(os.getenv("DOCX_MAX_UNCOMPRESSED_BYTES", str(500 * 1024 * 1024)))


def _sniff_content(file_path: str, ext: str) -> Optional[str]:
    """按扩展名嗅探文件内容是否与宣称格式一致；返回错误文案，None 表示通过。"""
    try:
        with open(file_path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return "文件读取失败"

    if ext == "pdf":
        if _PDF_HEADER not in head:
            return DocumentValidationErrorType.MIME_TYPE_MISMATCH
    elif ext == "docx":
        if not head.startswith(_DOCX_ZIP_MARK):
            return DocumentValidationErrorType.MIME_TYPE_MISMATCH
        try:
            import zipfile
            with zipfile.ZipFile(file_path) as zf:
                infos = zf.infolist()
                if len(infos) > _DOCX_MAX_MEMBERS:
                    return "docx 文件包含过多条目"
                if sum(i.file_size for i in infos) > _DOCX_MAX_UNCOMPRESSED:
                    return "docx 文件解压后体积过大"
                if "[Content_Types].xml" not in zf.namelist():
                    return DocumentValidationErrorType.MIME_TYPE_MISMATCH
        except Exception:
            return DocumentValidationErrorType.CORRUPTED_FILE
    elif ext in ("txt", "md"):
        if b"\x00" in head:
            return DocumentValidationErrorType.MIME_TYPE_MISMATCH
    return None


def validate_file(file_path: str) -> Optional[DocumentValidationError]:
    """基础校验：存在性 / 格式支持 / 大小限制 / 内容嗅探。返回 None 表示校验通过。"""
    # 获取所有方法名, 生成 {方法名: 绑定方法} 的映射,静态方法要用isfunction
    load_map = dict(inspect.getmembers(Loader, predicate=inspect.isfunction))
    valid_file_types = [s.split("_")[-1] for s in list(load_map.keys())]
    # 根据文件后缀获取加载方法
    load_func = "load_" + file_path.split(".")[-1].lower()
    # 校验文件是否存在，如果不存在则返回
    if not os.path.exists(file_path):
        logger.warning("%s", DocumentValidationErrorType.NOT_FOUND_FILE)
        return DocumentValidationError(file_path, DocumentValidationErrorType.NOT_FOUND_FILE)
    # 校验 .doc 旧版二进制格式：python-docx/ZipFile 无法解析，
    # 在通用格式校验前拦截并给出明确转换提示（load_doc 仅兼容 .docx）
    if file_path.rsplit(".", 1)[-1].lower() == "doc":
        logger.warning("%s", DocumentValidationErrorType.DOC_NEEDS_CONVERSION)
        return DocumentValidationError(file_path, DocumentValidationErrorType.DOC_NEEDS_CONVERSION)
    # 校验是否支持该文件类型，如果不存在则返回
    if load_func not in load_map:
        logger.warning("%s", DocumentValidationErrorType.UNSUPPORTED_FORMAT)
        logger.warning("支持格式:%s", valid_file_types)
        return DocumentValidationError(file_path, DocumentValidationErrorType.UNSUPPORTED_FORMAT)
    # 校验文件大小，如果超过 MAX_FILE_SIZE 则返回
    if os.path.getsize(file_path) > MAX_FILE_SIZE:
        logger.warning("%s", DocumentValidationErrorType.FILE_TOO_LARGE)
        return DocumentValidationError(file_path, DocumentValidationErrorType.FILE_TOO_LARGE)
    # M5：内容嗅探——伪装扩展名（如可执行文件改名 .pdf）不允许进入解析库
    content_err = _sniff_content(file_path, file_path.rsplit(".", 1)[-1].lower())
    if content_err is not None:
        logger.warning("%s：%s", content_err, file_path)
        return DocumentValidationError(file_path, content_err)
    return None


def load_single(file_path: str) -> Optional[List[Document]]:
    """按文件后缀分发到对应加载器（不做基础校验，由调用方先 validate_file）"""
    load_map = dict(inspect.getmembers(Loader, predicate=inspect.isfunction))
    load_func = "load_" + file_path.split(".")[-1].lower()
    logger.info("调用加载方法：%s", load_func)
    return load_map[load_func](file_path)


def load_documents(file_path_list) -> List[Optional[List[Document]]]:
    """批量加载（保留旧入口）：先基础校验，再按后缀分发加载"""
    load_files=[]
    for file_path in file_path_list:
        error = validate_file(file_path)
        if error is not None:
            load_files.append(error)
            continue
        try:
            # 调用加载方法
            load_files.append(load_single(file_path))
        except Exception as e:
            logger.warning("加载文档失败：%s", e)
            load_files.append(DocumentValidationError(file_path,DocumentValidationErrorType.LOAD_FAILED))
            continue

    return load_files
