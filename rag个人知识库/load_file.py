import inspect
import os
from typing import Optional, List, Iterable

from langchain_community.document_loaders import (
    PyPDFLoader,
    UnstructuredExcelLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredMarkdownLoader,
    TextLoader)
from langchain_core.documents import Document

from 尚硅谷.RAG.rag个人知识库.mineru_pdf import upload_files, download_files
from 尚硅谷.RAG.rag个人知识库.parser.document_validation_exception import DocumentValidationErrorType, \
    DocumentValidationError
from 尚硅谷.RAG.rag个人知识库.parser.word_parser import load_word


class Loader:


    @staticmethod
    def load_doc(file_path):  # Word 文件加载器
        documents = load_word(file_path)
        return None

    @staticmethod
    def load_docx(file_path):  # Word 文件加载器
        documents = load_word(file_path)
        return None

    @staticmethod
    def load_md(file_path, mode : str = "elements", strategy : str= "fast")-> Optional[List[Document]]:
        # Markdown 文件加载器
        loader = UnstructuredMarkdownLoader(
            file_path=file_path,
            # 加载模式:
            #   single 返回单个Document对象
            #   elements 按标题等元素切分文档
            mode=mode,
            # 解析策略：
            #   "fast"（快速模式），它会以最快的速度提取文本，不进行复杂的版面分析
            #   "hi_res" 高分辨率模式
            strategy=strategy
        )
        documents = loader.load()
        return documents

    @staticmethod
    def load_pdf(file_path) -> Optional[List[Document]]:
        # PDF 文件加载器
        """上传 PDF 到 MineRU 解析，解析完成后读取 full.md 返回 Document"""
        batch_id = upload_files([file_path])
        print(f"上传文件成功，batch_id: {batch_id}")
        if not batch_id:
            return None

        parsed_files = download_files(batch_id)
        all_documents = []

        for file in parsed_files:
            # 查找解析后释放的 full.md 文件
            # file_name = os.path.splitext(os.path.basename(file_path))[0]
            # parsed_dir = f"parsed_files/{file_name}_{i}"
            md_path = file+"/full.md"

            if not os.path.exists(md_path):
                print(f"未找到解析结果：{md_path}")
                return None

            print(f"读取解析结果：{md_path}")

            # loader = UnstructuredMarkdownLoader(md_path, mode="single", strategy="fast")
            # documents = loader.load()
            #
            documents = Loader.load_md(md_path)

            print(f"成功加载文档：{md_path}")
            # total_len = sum(len(d.page_content) for d in documents)
            # print(f"文档信息：共 {len(documents)} 段，总字符数：{total_len}")
            ## TODO
            all_documents.append(documents)
        if len(all_documents) == 0:
            return None
        else :
            return all_documents[0]


    @staticmethod
    def load_txt(file_path)-> Optional[List[Document]]:
        # TXT 文件加载器
        try:
            loader = TextLoader(file_path, encoding="utf-8")
        except UnicodeDecodeError:
            loader = TextLoader(file_path, encoding="gbk")

        documents = loader.load()
        return documents




def load_documents(file_path_list) -> Optional[List[Document]]:
    # 获取所有方法名, 生成 {方法名: 绑定方法} 的映射,静态方法要用isfunction
    load_map = dict(inspect.getmembers(Loader, predicate=inspect.isfunction))
    # 获取所有方法名，生成支持的文件类型列表
    valid_file_types = [s.split("_")[-1] for s in list(load_map.keys())]
    load_files=[]
    for file_path in file_path_list:
        # 根据文件后缀获取加载方法
        load_func = "load_" + file_path.split(".")[-1].lower()
        # 校验文件是否存在，如果不存在则返回 None
        if not os.path.exists(file_path):
            print(f"{DocumentValidationErrorType.NOT_FOUND_FILE}")
            load_files.append(DocumentValidationError(file_path,DocumentValidationErrorType.NOT_FOUND_FILE))
            continue
        # 校验是否支持该文件类型，如果不存在则返回 None
        if load_func not in load_map:
            print(f"{DocumentValidationErrorType.UNSUPPORTED_FORMAT}")
            print(f"支持格式:{valid_file_types}")
            load_files.append(DocumentValidationError(file_path,DocumentValidationErrorType.UNSUPPORTED_FORMAT))
            continue
        try:
            # 调用加载方法
            print(f"调用加载方法：{load_func}")
            load_files.append(load_map[load_func](file_path))

            # if file_path.endswith('.pdf'):
            #     print("检测到 PDF 文件")
            #     return Loader.load_pdf(file_path)
            # elif file_path.endswith(('.docx', '.doc')):
            #     print("检测到 Word 文件")
            #     return load_word(file_path)
            # # elif file_path.endswith('.xlsx'):
            # #     print("检测到 Excel 文件")
            # #     loader = UnstructuredExcelLoader(file_path)
            # elif file_path.endswith('.md'):
            #     print("检测到 Markdown 文件")
            #     return Loader.load_md(file_path)
            # elif file_path.endswith('.txt'):
            #     print("检测到 TXT 文件")
            #     try:
            #         loader = TextLoader(file_path, encoding="utf-8")
            #     except UnicodeDecodeError:
            #         loader = TextLoader(file_path, encoding="gbk")
            # else:
            #     print(DocumentValidationErrorType.UNSUPPORTED_FORMAT)
            #     print(f"支持格式:{document_type}")
            #     return None

            # documents = loader.load()
            # print(f"成功加载文档：{file_path}")
            # total_len = sum(len(d.page_content) for d in documents)
            # print(f"文档信息：共 {len(documents)} 页/段，总字符数：{total_len}")
            # return documents

        except Exception as e:
            print(f"加载文档失败：{str(e)}")
            load_files.append(DocumentValidationError(file_path,DocumentValidationErrorType.LOAD_FAILED))
            continue

    return load_files


