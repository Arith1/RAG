from langchain_core.documents import Document

from rag个人知识库.load_file import load_documents
from rag个人知识库.parser.document_validation_exception import DocumentValidationError

file_path_list = [
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\01.simple_word.docx",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\02.complicated_word.docx",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\03.simple_pdf.pdf",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\04.complicated_pdf.pdf",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\05.sample_text.txt",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\06.langchain-utf-8.txt",
    "F:\\PracticeProject\\RAG\\rag_project\\rag个人知识库\\resources\\07.sample.md",
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

    print(type(doc))
