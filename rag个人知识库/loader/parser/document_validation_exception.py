

class DocumentValidationErrorType:
    LOAD_FAILED = "加载失败"
    FILE_TOO_LARGE = "文件大小超限"  # 文件大小超限
    UNSUPPORTED_FORMAT = "不支持的格式"  # 不支持的格式
    MIME_TYPE_MISMATCH = "扩展名与实际类型不符"  # 扩展名与实际类型不符
    CORRUPTED_FILE = "文件损坏"  # 文件损坏
    EMPTY_FILE = "空文件"  # 空文件
    ENCODING_ERROR = "编码错误" # 编码错误
    NOT_FOUND_FILE = "文件不存在或路径错误"

class DocumentValidationError:
    file_path: str
    error_msg: str
    def __init__(self, file_path: str, error_msg: str):
        self.file_path = file_path
        self.error_msg = error_msg
    def __repr__(self):
        return f"{self.file_path}: {self.error_msg}"