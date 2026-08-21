"""
MinerU PDF 批量解析：调用 minerU_files(file_paths) 传入本地文件路径列表，全流程：
1. 申请上传链接（一个 batch 提交多个文件，拿到 batch_id 和 OSS 预签名上传 URL 列表）
2. 逐个上传本地文件到 OSS
3. 用 batch_id 轮询解析结果，直到所有文件状态变为 done/failed
4. 下载各文件结果 zip 并解压（产物含 full.md、content_list.json、images/ 等）

提交（submit_batch）与轮询下载（poll + download）已拆分，
调用方可先提交 batch，利用服务端解析的等待期并行处理其他本地文档。
"""
import logging
import os
import time
import zipfile
import io

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _build_header() -> dict:
    """加载 .env 并构造带鉴权的请求头"""
    load_dotenv()
    # token 为空时请求头会变成 "Bearer None" 导致 401，这里提前拦截
    token = os.getenv("MINERU_API_TOKEN")
    if not token:
        raise RuntimeError("MINERU_API_TOKEN 未配置，请在项目根目录的 .env 中填写有效 token")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"  # token 不带 Bearer 前缀，由这里统一拼接
    }


def poll_batch_result(batch_id, header, interval=5, timeout=600, expected_data_ids=None):
    """轮询批量解析结果，全部完成后返回 extract_result 列表。

    expected_data_ids: 非 None 时只等待这些 data_id 对应的文件，避免上传失败的文件阻塞整批。
    """
    result_url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
    logger.info("start polling batch:%s ...", batch_id)
    start = time.time()
    # 在 timeout 时限内每隔 interval 秒查一次，避免无限等待
    while time.time() - start < timeout:
        res = requests.get(result_url, headers=header, timeout=30)
        if res.status_code != 200:
            # 接口偶发非 200 不直接退出，休眠后继续重试
            logger.warning("poll failed. status:%s", res.status_code)
            time.sleep(interval)
            continue
        extract_results = res.json()["data"]["extract_result"]
        if expected_data_ids is not None:
            extract_results = [
                item for item in extract_results
                if item.get("data_id") in expected_data_ids
            ]
            if not extract_results:
                time.sleep(interval)
                continue
        # state 取值：waiting-file / pending / running / done / failed
        states = [item["state"] for item in extract_results]
        logger.info("current states:%s", states)
        # 所有文件都到达终态（done/failed）才结束轮询，部分失败不阻塞其他文件
        if all(s in ("done", "failed") for s in states):
            return extract_results
        time.sleep(interval)
    raise TimeoutError(f"batch {batch_id} 解析超时")


def download_zip(zip_url, retries=3):
    """下载结果 zip，失败时重试，最后一次尝试绕过系统代理"""
    for attempt in range(1, retries + 1):
        try:
            if attempt < retries:
                # 前几次正常请求（走系统代理），应对 CDN 偶发抖动
                resp = requests.get(zip_url, timeout=60)
            else:
                # 最后一次重试：绕过系统代理直连（本地代理常导致 SSL EOF）
                session = requests.Session()
                session.trust_env = False  # 忽略 HTTP_PROXY/HTTPS_PROXY 等环境变量
                resp = session.get(zip_url, timeout=60)
            # 非 2xx（如预签名 URL 过期 403）按失败处理并重试，
            # 避免把错误页字节当 zip 解压抛 BadZipFile 中断整批
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException as e:
            logger.warning("download attempt %d failed:%s", attempt, e)
            time.sleep(2)
    raise RuntimeError(f"zip 下载失败，已重试 {retries} 次: {zip_url}")


def download_and_extract(extract_results, output_root, file_paths) -> dict:
    """下载解析结果 zip 并解压到 output_root，返回以原始路径为 key 的结构化结果。

    每个文件的结果为 {"status", "output_dir", "md_path", "error"}，
    通过 data_id（提交时写入的下标）把服务端结果映射回 file_paths 中的原始路径，
    避免不同目录下同名文件用 file_name 做 key 时互相覆盖。
    """
    results = {}
    for item in extract_results:
        # data_id 存的是提交时的下标，据此回溯原始输入路径
        src_path = file_paths[int(item["data_id"])]
        # 解析失败的记录原因后跳过（单文件失败不阻塞整批）
        if item["state"] != "done":
            err_msg = item.get("err_msg") or "解析失败"
            logger.warning("%s 解析失败，原因:%s", item["file_name"], err_msg)
            results[src_path] = {"status": "failed", "output_dir": None, "md_path": None, "error": err_msg}
            continue
        # full_zip_url 有时效性，拿到后尽快下载。
        # 单文件下载/解压失败（URL 过期、zip 损坏等）只标记该文件失败，
        # 不阻塞整批——download_zip 内部已重试，这里兜底隔离
        try:
            zip_content = download_zip(item["full_zip_url"])
            # 用 data_id 作目录后缀，同名文件产物也不会互相覆盖
            output_dir = os.path.join(output_root, "{}_{}".format(item["file_name"], item["data_id"]))
            # output_dir = os.path.join(output_root, "{}".format(item["file_name"]))
            os.makedirs(output_dir, exist_ok=True)
            # 内存流直接解压，不落盘临时 zip 文件
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                zf.extractall(output_dir)
        except Exception as e:
            logger.warning("%s 结果下载/解压失败，原因:%s", item["file_name"], e)
            results[src_path] = {
                "status": "failed", "output_dir": None, "md_path": None,
                "error": f"结果下载/解压失败:{e}",
            }
            continue
        logger.info("%s 解析结果已解压到:%s", item["file_name"], output_dir)
        # md_path 直接给到 full.md，下游无需感知产物目录结构
        results[src_path] = {
            "status": "success",
            "output_dir": output_dir,
            "md_path": os.path.join(output_dir, "full.md"),
            "error": None
        }
    return results


def submit_batch(file_paths: list, header: dict) -> tuple[str, dict[str, str]]:
    """申请上传链接并上传所有文件。

    返回 (batch_id, failed_uploads)，failed_uploads 以 data_id 为 key、失败原因为 value。
    单个文件上传失败不会中止整个 batch，由调用方决定后续策略。
    """
    url = "https://mineru.net/api/v4/file-urls/batch"
    data = {
        "files": [
            # name 决定结果目录名；data_id 用下标便于业务侧回溯到原始路径
            {"name": os.path.basename(p), "data_id": str(i)}
            for i, p in enumerate(file_paths)
        ],
        "model_version": "vlm"  # 使用 VLM 模型解析（支持公式/表格/多语言）
    }
    # 第 1 步：申请上传链接，成功后返回 batch_id 和预签名上传 URL 列表
    response = requests.post(url, headers=header, json=data)
    if response.status_code != 200:
        raise RuntimeError(f"申请上传链接失败 status:{response.status_code}")
    result = response.json()
    # code == 0 表示业务层成功（HTTP 200 不代表申请成功）
    if result["code"] != 0:
        raise RuntimeError('申请上传链接失败，原因:{}'.format(result["msg"]))
    batch_id = result["data"]["batch_id"]
    urls = result["data"]["file_urls"]
    if len(urls) != len(file_paths):
        raise RuntimeError(f"上传链接数量与文件数量不一致：{len(urls)} != {len(file_paths)}")
    logger.info("batch_id:%s", batch_id)
    # 第 2 步：把本地文件 PUT 到预签名 URL（直传 OSS，无需再带 token）
    # file_urls 与 data["files"] 顺序一一对应，按下标对齐上传
    failed_uploads = {}
    for i, upload_url in enumerate(urls):
        file_name = os.path.basename(file_paths[i])
        try:
            with open(file_paths[i], 'rb') as f:
                res_upload = requests.put(upload_url, data=f)
            if res_upload.ok:
                logger.info("%s upload success", file_name)
            else:
                logger.warning("%s upload failed. status:%s", file_name, res_upload.status_code)
                failed_uploads[str(i)] = f"上传失败 status:{res_upload.status_code}"
        except Exception as e:
            logger.warning("%s upload exception: %s", file_name, e)
            failed_uploads[str(i)] = f"上传异常:{e}"
    return batch_id, failed_uploads


def minerU_files(file_paths: list, output_root: str | None = None) -> dict:
    """批量上传 PDF 到 MinerU 解析，解析完成后下载并解压产物。

    :param file_paths: 本地 PDF 路径列表（单个 batch 上限 200 个文件）
    :param output_root: 产物根目录，默认为第一个文件所在目录下的 mineru_results
    :return: {原始文件路径: {"status": "success"/"failed"/"skipped",
                              "output_dir": 解压目录,
                              "md_path": full.md 路径,
                              "error": 失败原因}}
    """
    results = {}
    # 先过滤不存在的文件，避免上传阶段中途报错浪费整批额度
    valid_paths = []
    for p in file_paths:
        if os.path.isfile(p):
            valid_paths.append(p)
        else:
            logger.warning("文件不存在，跳过:%s", p)
            results[p] = {"status": "skipped", "output_dir": None, "md_path": None, "error": "文件不存在"}
    if not valid_paths:
        logger.warning("没有可上传的有效文件")
        return results
    # 默认产物目录跟随第一个文件所在目录
    if output_root is None:
        output_root = os.path.join(os.path.dirname(os.path.abspath(valid_paths[0])), "mineru_results")

    header = _build_header()
    # 提交 batch（申请链接 + 上传）→ 轮询直到全部终态 → 下载解压产物
    batch_id, failed_uploads = submit_batch(valid_paths, header)
    successful_ids = {
        str(i) for i in range(len(valid_paths)) if str(i) not in failed_uploads
    }
    if not successful_ids:
        for i, p in enumerate(valid_paths):
            results[p] = {
                "status": "failed",
                "output_dir": None,
                "md_path": None,
                "error": failed_uploads.get(str(i), "上传失败"),
            }
        return results

    extract_results = poll_batch_result(
        batch_id,
        header,
        expected_data_ids=successful_ids,
    )
    results.update(download_and_extract(extract_results, output_root, valid_paths))
    for i, p in enumerate(valid_paths):
        if str(i) in failed_uploads:
            results[p] = {
                "status": "failed",
                "output_dir": None,
                "md_path": None,
                "error": failed_uploads[str(i)],
            }
    return results


def minerU_files_ordered(file_paths: list, output_root: str | None = None) -> list[dict]:
    """批量解析并返回与输入顺序完全一致的结果列表。"""
    results = minerU_files(file_paths, output_root)
    return [
        results.get(
            p,
            {
                "status": "failed",
                "output_dir": None,
                "md_path": None,
                "error": "未返回解析结果",
            },
        )
        for p in file_paths
    ]
