"""指纹工具单元测试：确定性 / 来源隔离 / 文件哈希。"""
import os
import shutil

from rag个人知识库.utils.hash_utils import (
    compute_chunk_fingerprint, compute_file_hash, compute_identity_hash,
)


class TestFingerprints:
    def test_identity_hash_deterministic(self):
        a = compute_identity_hash("a.md", "F:/x/a.md")
        b = compute_identity_hash("a.md", "F:/x/a.md")
        assert a == b
        assert len(a) == 64

    def test_identity_hash_differs_by_name_or_source(self):
        assert compute_identity_hash("a.md", "F:/x/a.md") != compute_identity_hash("b.md", "F:/x/a.md")
        assert compute_identity_hash("a.md", "F:/x/a.md") != compute_identity_hash("a.md", "F:/y/a.md")

    def test_chunk_fingerprint_includes_source(self):
        # 不同文档里相同的一句话，指纹必须不同（防跨文档冲突）
        fp1 = compute_chunk_fingerprint("相同的句子", "doc1.md")
        fp2 = compute_chunk_fingerprint("相同的句子", "doc2.md")
        assert fp1 != fp2
        assert len(fp1) == 64

    def test_chunk_fingerprint_deterministic(self):
        fp1 = compute_chunk_fingerprint("内容", "src")
        fp2 = compute_chunk_fingerprint("内容", "src")
        assert fp1 == fp2

    def test_file_hash_streaming(self):
        # 沙箱下 tempfile.mkdtemp 的目录 ACL 受限，改用 os.makedirs + 固定目录名
        tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_test_hash_tmp")
        os.makedirs(tmp, exist_ok=True)
        try:
            p = os.path.join(tmp, "f.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("hello")
            assert compute_file_hash(p) == compute_file_hash(p)
            p2 = os.path.join(tmp, "f2.txt")
            with open(p2, "w", encoding="utf-8") as f:
                f.write("hello!")
            assert compute_file_hash(p) != compute_file_hash(p2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
