from langchain_core.documents import Document

from rag个人知识库.load_file import load_documents
from rag个人知识库.parser.document_validation_exception import DocumentValidationError
from rag个人知识库.spliter.spliter import split_documents
from rag个人知识库.vector_store.milvus_store import save_chunks, search

file_path_list = [
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\01.simple_word.docx",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\02.complicated_word.docx",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\03.simple_pdf.pdf",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\04.complicated_pdf.pdf",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\05.sample_text.txt",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\06.langchain-utf-8.txt",
    # "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\07.sample.md",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\AI智能体开发框架LangChain.docx",
]

docs = load_documents(file_path_list)

for i, doc in enumerate(docs):
    if isinstance(doc, DocumentValidationError):
        print(f"第{i + 1}个文档{doc.file_path}加载失败,失败原因:{doc.error_msg}")
    else :
        print(f"第{i + 1}个文档{doc[0].metadata['source']}加载成功")
        for page in doc[0:5]:
            print(f"内容：{page.page_content}")
            print(f"元数据：{page.metadata}")
        print(f"文件{doc[0].metadata['source']}包含{len(doc)}页")
        print(f"文件类型为:{type(doc[0])}")
        chunks = split_documents(doc)
        for chunk in chunks:
            print(f"内容：{chunk.page_content[:200]}")
            print(f"元数据：{chunk.metadata}")
        # 切分完成后向量化入库：确定性 ID 保证重复运行不产生重复向量
        save_chunks(chunks)
    print(type(doc))

# 入库完成后做一次相似度检索，验证向量库可用
_query = "LangChain 是什么"
print(f"\n[检索验证] 查询：{_query}")
for rank, hit in enumerate(search(_query, k=3), start=1):
    print(f"Top{rank}: {hit.page_content[:120]}...")
    print(f"  来源：{hit.metadata.get('source')}")
