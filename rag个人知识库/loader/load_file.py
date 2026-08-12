import inspect
import os
from typing import Optional, List

from langchain_community.document_loaders import (
    UnstructuredWordDocumentLoader,
    TextLoader)
from langchain_core.documents import Document

# from rag个人知识库.demo.mineru_demo import minerU_files
from rag个人知识库.loader.parser.document_validation_exception import DocumentValidationErrorType, \
    DocumentValidationError
from rag个人知识库.loader.parser.mineru_parser import minerU_files
from rag个人知识库.loader.parser.word_parser import word_complicatedness, COMPLEXITY_THRESHOLD

# 文件大小阈值（单位：字节），超过此值使用 lazy_load 避免内存溢出
# 默认 10MB
LARGE_FILE_THRESHOLD = 5 * 1024 * 1024
MAX_FILE_SIZE = 10 * 1024 * 1024


def smart_load(loader, file_path: str) -> List[Document]:
    """
    根据文件大小智能选择加载方式：
      - 文件 <= LARGE_FILE_THRESHOLD：使用 load()，一次性返回 List[Document]
      - 文件 > LARGE_FILE_THRESHOLD：使用 lazy_load()，返回 Iterator[Document]，逐条处理不占内存
    """
    file_size = os.path.getsize(file_path)
    if file_size > LARGE_FILE_THRESHOLD:
        print(f"[smart_load] 文件较大({file_size / 1024 / 1024:.1f}MB)，使用 lazy_load 逐条加载: {file_path}")
        documents = [document for document in loader.lazy_load()]
        return documents
    else:
        print(f"[smart_load] 文件大小正常({file_size / 1024:.1f}KB)，使用 load 直接加载: {file_path}")
        return loader.load()

def load_word(file_path: str) -> Optional[List[Document]]:
    """
    加载 Word 文档。

    流程：
      1. 调用 word_complicatedness 评估文档复杂度
      2. 简单文档 → UnstructuredWordDocumentLoader 直接解析（快、免费、离线）
      3. 复杂文档 → 上传 MinerU 解析（图片/公式/图表本地解析易丢失），读产物 full.md
    """
    print(f"正在分析 Word 文档复杂度：{file_path}")
    score = word_complicatedness(file_path)

    if score < COMPLEXITY_THRESHOLD:
        # ── 简单文档：docx 本身结构化，本地解析可靠且不消耗解析额度 ──
        print("复杂度较低，使用 UnstructuredWordDocumentLoader 直接解析")
        loader = UnstructuredWordDocumentLoader(
            file_path=file_path,
            mode="single",
        )
        documents = loader.load()
        print(f"成功加载文档：{file_path}")
        total_len = sum(len(d.page_content) for d in documents)
        print(f"文档信息：共 {len(documents)} 段，总字符数：{total_len}")
        return documents

    # ── 复杂文档：走 MinerU 解析 ──
    print("复杂度较高，使用 MinerU 进行解析")
    # minerU_files 接收路径列表，返回以原始路径为 key 的结构化结果
    results = minerU_files([file_path])
    result = results.get(file_path)
    if result is None or result["status"] != "success":
        error = result["error"] if result else "未返回解析结果"
        print(f"MinerU 解析失败：{file_path}，原因：{error}")
        return None
    md_path = result["md_path"]
    if not os.path.exists(md_path):
        print(f"未找到解析结果：{md_path}")
        return None
    print(f"读取解析结果：{md_path}")
    # 产物是 Markdown，按原文读入，切分交给切分层
    documents = Loader.load_md(md_path)
    if documents:
        for doc in documents:
            # source 指回原始 Word 文件便于溯源，解析产物路径另存 md_path
            doc.metadata["source"] = file_path
            doc.metadata["md_path"] = md_path
    print(f"成功加载文档：{md_path}")
    return documents


class Loader:


    @staticmethod
    def load_doc(file_path):  # Word 文件加载器
        documents = load_word(file_path)
        return documents

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
        # minerU_files 接收路径列表，单文件包成单元素列表传入
        results = minerU_files([file_path])
        # 返回值以原始路径为 key，直接取当前文件的结果
        result = results.get(file_path)
        if result is None or result["status"] != "success":
            error = result["error"] if result else "未返回解析结果"
            print(f"PDF 解析失败：{file_path}，原因：{error}")
            return None
        # md_path 由 minerU_files 直接给出，无需自己拼产物目录结构
        md_path = result["md_path"]
        if not os.path.exists(md_path):
            print(f"未找到解析结果：{md_path}")
            return None
        print(f"读取解析结果：{md_path}")
        # 解析产物是 Markdown，复用 load_md 按原文加载为 Document
        documents = Loader.load_md(md_path)
        if documents:
            for doc in documents:
                # source 指回原始 PDF 便于溯源，解析产物路径另存 md_path
                doc.metadata["source"] = file_path
                doc.metadata["md_path"] = md_path
        return documents


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


def validate_file(file_path: str) -> Optional[DocumentValidationError]:
    """基础校验：存在性 / 格式支持 / 大小限制。返回 None 表示校验通过。"""
    # 获取所有方法名, 生成 {方法名: 绑定方法} 的映射,静态方法要用isfunction
    load_map = dict(inspect.getmembers(Loader, predicate=inspect.isfunction))
    valid_file_types = [s.split("_")[-1] for s in list(load_map.keys())]
    # 根据文件后缀获取加载方法
    load_func = "load_" + file_path.split(".")[-1].lower()
    # 校验文件是否存在，如果不存在则返回
    if not os.path.exists(file_path):
        print(f"{DocumentValidationErrorType.NOT_FOUND_FILE}")
        return DocumentValidationError(file_path, DocumentValidationErrorType.NOT_FOUND_FILE)
    # 校验是否支持该文件类型，如果不存在则返回
    if load_func not in load_map:
        print(f"{DocumentValidationErrorType.UNSUPPORTED_FORMAT}")
        print(f"支持格式:{valid_file_types}")
        return DocumentValidationError(file_path, DocumentValidationErrorType.UNSUPPORTED_FORMAT)
    # 校验文件大小，如果超过 MAX_FILE_SIZE 则返回
    if os.path.getsize(file_path) > MAX_FILE_SIZE:
        print(f"{DocumentValidationErrorType.FILE_TOO_LARGE}")
        return DocumentValidationError(file_path, DocumentValidationErrorType.FILE_TOO_LARGE)
    return None


def load_single(file_path: str) -> Optional[List[Document]]:
    """按文件后缀分发到对应加载器（不做基础校验，由调用方先 validate_file）"""
    load_map = dict(inspect.getmembers(Loader, predicate=inspect.isfunction))
    load_func = "load_" + file_path.split(".")[-1].lower()
    print(f"调用加载方法：{load_func}")
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
            print(f"加载文档失败：{str(e)}")
            load_files.append(DocumentValidationError(file_path,DocumentValidationErrorType.LOAD_FAILED))
            continue

    return load_files