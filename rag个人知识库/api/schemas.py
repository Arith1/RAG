"""FastAPI request/response schemas."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from rag个人知识库.api.helpers import _check_password_utf8_limit


class RegisterIn(BaseModel):
    """注册入参。长度上限对齐 users.username 列宽与 bcrypt 72 字节硬限制。"""

    username: str = Field(..., min_length=2, max_length=64, description="用户名（2~64 字符）")
    password: str = Field(..., min_length=6, max_length=64, description="密码（6~64 字符）")

    @field_validator("password")
    @classmethod
    def _password_utf8_limit(cls, v: str) -> str:
        return _check_password_utf8_limit(v)

class UserOut(BaseModel):
    id: int
    username: str
    role: str

class ProfileOut(BaseModel):
    """个人详情公开字段：自己与他人查看同一模型，is_self 区分是否本人。"""

    id: int
    username: str
    role: str
    created_at: Optional[datetime] = None
    is_self: bool = False

class ChangePasswordIn(BaseModel):
    old_password: str = Field(..., max_length=128)
    new_password: str = Field(..., min_length=6, max_length=64, description="新密码（6~64 字符）")

    @field_validator("new_password")
    @classmethod
    def _new_password_utf8_limit(cls, v: str) -> str:
        return _check_password_utf8_limit(v)

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str

class ChatIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000, description="用户输入")
    session_id: Optional[str] = Field(default=None, max_length=64)  # 为空则服务端生成
    # 会话检索范围（仅「新建/空白会话首问」生效，会话已有范围时以库中为准）
    retrieve_own_private: bool = True
    retrieve_own_public: bool = True
    retrieve_kb_public: bool = True
    retrieve_owner_ids: List[int] = Field(default_factory=list, max_length=50)

class ChatOut(BaseModel):
    answer: str
    intent: str
    query: Optional[str]
    sources: List[dict]
    session_id: str
    error: Optional[str] = None

class SearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    k: int = Field(default=3, ge=1, le=50)  # 限制召回/精排条数，防超限请求
    source: Optional[str] = Field(default=None, max_length=512)

class ChatSessionOut(BaseModel):
    session_id: str
    title: str
    message_count: int
    last_message_at: Optional[str] = None
    last_message_preview: str = ''
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    retrieve_own_private: bool = True
    retrieve_own_public: bool = True
    retrieve_kb_public: bool = True
    retrieve_owner_ids: List[int] = Field(default_factory=list)

class ChatMessageOut(BaseModel):
    role: str
    content: str
    sources: List[dict] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)
    intent: Optional[str] = None
    created_at: Optional[str] = None

class ChatSessionDetailOut(BaseModel):
    session_id: str
    title: str
    messages: List[ChatMessageOut]
    retrieve_own_private: bool = True
    retrieve_own_public: bool = True
    retrieve_kb_public: bool = True
    retrieve_owner_ids: List[int] = Field(default_factory=list)
    retrieve_owner_names: List[str] = Field(default_factory=list)

class ChatRenameIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=128)

class DocumentOut(BaseModel):
    id: int
    file_name: str
    version: str
    source: str
    chunk_count: int
    sync_status: str
    owner_id: Optional[int] = None
    is_public: bool = False
    # 下载量（非所有者下载 +1）与最近更新时间（知识库排序用；后端已提供，缺省兼容旧数据）
    download_count: int = 0
    updated_at: Optional[str] = None

class DocumentListOut(BaseModel):
    """文档列表分页响应：items 为当前页文档，total 为可见文档总数（供分页统计）。"""

    total: int
    items: List[DocumentOut]

class BillingBucketOut(BaseModel):
    """按类型 / 按模型的用量分布桶。"""

    key: str
    requests: int
    tokens: int
    cost: float

class BillingDailyOut(BaseModel):
    date: str
    requests: int
    tokens: int
    cost: float

class BillingSummaryOut(BaseModel):
    """当前用户用量汇总（请求/调用数、费用、tokens、分布与按天趋势）。"""

    range: str
    request_count: int
    total_requests: int
    total_cost: float
    avg_cost: float
    total_tokens: int
    input_tokens: int
    cached_tokens: int
    uncached_tokens: int
    output_tokens: int
    by_type: List[BillingBucketOut]
    by_model: List[BillingBucketOut]
    daily: List[BillingDailyOut]

class UsageRecordOut(BaseModel):
    """单条 LLM 调用计费记录。"""

    id: int
    session_id: Optional[str] = None
    request_id: str
    provider: str
    model: str
    type: str
    input_tokens: int
    cached_tokens: int
    uncached_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    latency_ms: int
    status: str
    created_at: Optional[datetime] = None

class UsageListOut(BaseModel):
    total: int
    items: List[UsageRecordOut]

class AdminTopUserOut(BaseModel):
    user_id: int
    username: Optional[str] = None
    requests: int
    tokens: int
    cost: float

class AdminBillingOverviewOut(BaseModel):
    """管理员：全站用量汇总 + 费用排行。"""

    range: str
    request_count: int
    total_requests: int
    total_cost: float
    total_tokens: int
    active_users: int
    by_type: List[BillingBucketOut]
    by_model: List[BillingBucketOut]
    top_users: List[AdminTopUserOut]

class AdminUserUsageOut(BaseModel):
    user_id: int
    username: Optional[str] = None
    total_requests: int
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost: float
    last_used_at: Optional[datetime] = None

class AdminUserUsageListOut(BaseModel):
    total: int
    items: List[AdminUserUsageOut]
