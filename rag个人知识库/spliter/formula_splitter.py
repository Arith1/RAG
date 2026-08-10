# splitter/formula_splitter.py

import re
from langchain_core.documents import Document


class FormulaSplitter():

    def split(self, document):

        text = document.page_content

        # 按编号切块
        pattern = r'(?=\n?\d+\.\s*)'

        sections = re.split(pattern, text)

        chunks = []

        for section in sections:

            section = section.strip()

            if not section:
                continue

            # 第一块一般是标题
            if not re.match(r'^\d+\.', section):
                continue

            lines = section.splitlines()

            title = re.sub(r'^\d+\.\s*', '', lines[0]).strip()

            content = "\n".join(lines[1:]).strip()

            chunks.append(
                Document(
                    page_content=content,
                    metadata={
                        "type": "formula",
                        "source": document.metadata.get("source"),
                        "title": title
                    }
                )
            )

        return chunks
