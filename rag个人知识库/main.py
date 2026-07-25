from langchain_core.documents import Document

from 尚硅谷.RAG.rag个人知识库.load_file import load_documents
from 尚硅谷.RAG.rag个人知识库.parser.document_validation_exception import DocumentValidationError

file_path_list = ["resources/03-langchain-utf-8.txt","resources/02-langchain-utf-8.txt","resources/06.sample.md"]

docs = load_documents(file_path_list)

for i, doc in enumerate(docs):
    if isinstance(doc, DocumentValidationError):
        print(f"第{i + 1}个文档{doc.file_path}加载失败,失败原因:{doc.error_msg}")
    else :
        print(f"第{i + 1}个文档{doc[0].metadata['source']}加载成功")
        for page in doc[0:15]:
            print(f"内容：{page.page_content}")
            print(f"元数据：{page.metadata}")

    print(type(doc))
