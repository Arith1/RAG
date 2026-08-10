from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag个人知识库.models.vector import VectorFile


def insert_vector_file():
    pass

def select_file_names(db: AsyncSession):
    result = db.execute(select(VectorFile))
    return result.scalars().all()

