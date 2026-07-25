import io
import os
import time
import zipfile
import requests
from dotenv import load_dotenv
load_dotenv(override=True)
def upload_files(file_paths: list[str]) -> str:
    """批量上传文件"""
    url = "https://mineru.net/api/v4/file-urls/batch"
    api_token = os.getenv("MINERU_API_TOKEN")
    header = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
    }
    files_info = [
        {
            "name": os.path.basename(file_path),
            "is_ocr": True,
            "data_id": f"file_{i}",
        }
        for i, file_path in enumerate(file_paths)
    ]
    data = {
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
        "files": files_info,
    }
    try:
        response = requests.post(url, headers=header, json=data)
        if response.status_code == 200:
            result = response.json()
            print("response success. result:{}".format(result))
            if result["code"] == 0:
                batch_id = result["data"]["batch_id"]
                urls = result["data"]["file_urls"]
                print("batch_id:{}\nurls:{}".format(batch_id, urls))
                for i in range(0, len(urls)):
                    with open(file_paths[i], "rb") as f:
                        res_upload = requests.put(urls[i], data=f)
                        if res_upload.status_code == 200:
                            print(f"{urls[i]} upload success")
                        else:
                            print(f"{urls[i]} upload failed")
                            return None
                return batch_id
            else:
                print("apply upload url failed, reason:{}")
                return None
        else:
            print(
                "response not success. status:{} ,result:{}".format(
                    response.status_code, response.text
                )
            )
            return None
    except Exception as err:
        print(err)
        return None

def download_files(batch_id):
    """批量获取任务结果"""
    if not batch_id:
        print("batch_id为空，跳过下载")
        return
    os.makedirs("parsed_files", exist_ok=True)
    url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
    api_token = os.getenv("MINERU_API_TOKEN")
    header = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
    }
    failed_files = set()
    done_files = set()
    files = []
    while True:
        res = requests.get(url, headers=header)
        result_json = res.json()
        if res.status_code != 200 or result_json.get("code") != 0:
            print("get result failed:", result_json)
            break
        extract_results = result_json["data"]["extract_result"]
        for result in extract_results:
            data_id = result["data_id"]
            if result["state"] == "failed":
                failed_files.add(data_id)
            elif result["state"] == "done" and data_id not in done_files:
                files.append(f"parsed_files/{result['file_name']}_{data_id}")
                done_files.add(data_id)
                full_zip_url = result["full_zip_url"]
                res_download = requests.get(full_zip_url)
                # 在内存中解压，直接释放文件，不保留压缩包
                with zipfile.ZipFile(io.BytesIO(res_download.content)) as zf:
                    extract_dir = f"parsed_files/{result['file_name']}_{data_id}"
                    os.makedirs(extract_dir, exist_ok=True)
                    zf.extractall(extract_dir)
                print(f"解压完成：{extract_dir}")
        if len(failed_files) + len(done_files) == len(extract_results):
            break
        time.sleep(5)

    for i in failed_files:
        print("failed:", i)
    for i in done_files:
        print("done:", i)

    return files

