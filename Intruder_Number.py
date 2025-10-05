#!/usr/bin/env python3

import re
import sys
import time
import threading
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from openpyxl import Workbook

# defaults
EXCEL_CELL_LIMIT = 32767
SNIPPET_LEN = 500
DEFAULT_WORKERS = 20
DEFAULT_RPS = 50
DEFAULT_MAX_RETRIES = 2
DEFAULT_POOLSIZE = 100

requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)


def read_request_file(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def split_request(raw):
    parts = re.split(r"\r\n\r\n|\n\n", raw, maxsplit=1)
    head = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    lines = re.split(r"\r\n|\n", head)
    request_line = lines[0].strip()
    header_lines = [ln for ln in lines[1:] if ln.strip() != ""]
    headers = {}
    for h in header_lines:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    return request_line, headers, body


def find_dollar_pairs(raw):
    return re.findall(r"\$(.*?)\$", raw, flags=re.S)


def format_number(n, min_d, max_d):
    s = str(n)
    if len(s) < min_d:
        s = s.zfill(min_d)
    if len(s) > max_d:
        return None
    return s


def build_url_from_request_line(req_line, target_host, headers):
    toks = req_line.split()
    if len(toks) < 2:
        raise ValueError("无法解析请求行: " + req_line)
    path_or_url = toks[1]
    parsed = urlparse(path_or_url)
    if parsed.scheme and parsed.netloc:
        new = parsed._replace(netloc=target_host)
        return urlunparse(new)
    else:
        scheme = "http"
        ref = headers.get("Referer", "")
        if ref.lower().startswith("https://"):
            scheme = "https"
        path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
        return f"{scheme}://{target_host}{path}"


class RateLimiter:
    def __init__(self, rps):
        self.rps = rps if rps and rps > 0 else 0
        self.interval = 1.0 / self.rps if self.rps else 0
        self.lock = threading.Lock()
        self.next_allowed = time.time()

    def wait(self):
        if not self.interval:
            return
        with self.lock:
            now = time.time()
            if now < self.next_allowed:
                time.sleep(self.next_allowed - now)
                self.next_allowed += self.interval
            else:
                self.next_allowed = now + self.interval


def make_session(max_retries, poolsize):
    s = requests.Session()
    retries = Retry(total=max_retries, backoff_factor=0.2,
                    status_forcelist=(500, 502, 503, 504),
                    allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]))
    adapter = HTTPAdapter(max_retries=retries, pool_connections=poolsize, pool_maxsize=poolsize)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_worker_sessions = threading.local()


def thread_init_session(max_retries, poolsize):
    _worker_sessions.session = make_session(max_retries, poolsize)
    # expose as attribute used by worker
    worker_request._session = _worker_sessions.session


def worker_request(item, rate_limiter, timeout, verify_ssl):
    rate_limiter.wait()
    session = worker_request._session
    payload_desc = item["payload_desc"]
    filled = item["filled"]
    try:
        req_line, headers, body = split_request(filled)
    except Exception as e:
        return {"payload_desc": payload_desc, "url": "", "status_code": "PARSE_ERROR", "length": 0, "snippet": str(e)[:SNIPPET_LEN], "full": ""}

    target = headers.get("Host", "")
    url = build_url_from_request_line(req_line, target, headers)
    method = req_line.split()[0].upper()
    data = body.encode("utf-8") if body else None

    try:
        resp = session.request(method, url, headers=headers, data=data, timeout=timeout, verify=verify_ssl)
        text = resp.text
        length = len(resp.content)
        snippet = text[:SNIPPET_LEN]
        truncated = text[:EXCEL_CELL_LIMIT]
        return {"payload_desc": payload_desc, "url": url, "status_code": resp.status_code, "length": length, "snippet": snippet, "full": truncated}
    except Exception as e:
        return {"payload_desc": payload_desc, "url": url, "status_code": "ERROR", "length": 0, "snippet": str(e)[:SNIPPET_LEN], "full": ""}


def build_tasks(raw_template, target, ranges):
    tasks = []
    skipped = 0
    pairs = find_dollar_pairs(raw_template)
    num_pairs = len(pairs)
    if num_pairs == 1:
        a_start, a_end, a_min, a_max = ranges[0]
        for a in range(a_start, a_end + 1):
            fa = format_number(a, a_min, a_max)
            if fa is None:
                skipped += 1
                continue
            filled = re.sub(r"\$(.*?)\$", fa, raw_template, count=1, flags=re.S)
            req_line, headers, body = split_request(filled)
            headers["Host"] = target
            header_text = "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
            filled_full = req_line + "\r\n" + header_text + "\r\n\r\n" + body
            tasks.append({"payload_desc": f"a={fa}", "filled": filled_full})
    else:
        a_start, a_end, a_min, a_max = ranges[0]
        b_start, b_end, b_min, b_max = ranges[1]
        for a in range(a_start, a_end + 1):
            fa = format_number(a, a_min, a_max)
            if fa is None:
                skipped += 1
                continue
            for b in range(b_start, b_end + 1):
                fb = format_number(b, b_min, b_max)
                if fb is None:
                    skipped += 1
                    continue
                filled = raw_template
                filled = re.sub(r"\$(.*?)\$", fa, filled, count=1, flags=re.S)
                filled = re.sub(r"\$(.*?)\$", fb, filled, count=1, flags=re.S)
                req_line, headers, body = split_request(filled)
                headers["Host"] = target
                header_text = "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
                filled_full = req_line + "\r\n" + header_text + "\r\n\r\n" + body
                tasks.append({"payload_desc": f"a={fa};b={fb}", "filled": filled_full})
    return tasks, skipped


def save_results(results, outpath):
    wb = Workbook()
    ws = wb.active
    ws.title = "results"
    ws.append(["payload_desc", "url", "status_code", "response_length_bytes", "response_snippet", "response_full_truncated"])
    for r in results:
        ws.append([r.get("payload_desc", ""), r.get("url", ""), r.get("status_code", ""), r.get("length", 0), r.get("snippet", ""), r.get("full", "")])
    wb.save(outpath)


def main():
    parser = argparse.ArgumentParser(description="并发爆破脚本（支持 --workers 和 --rps）")
    parser.add_argument("--request-file", "-r", default="request.txt")
    parser.add_argument("--target", "-t", required=True, help="目标 Host，例如 example.com 或 example.com:8080")
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS, help="全局 requests per second（0 表示不限制）")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output", "-o", default="results.xlsx")
    parser.add_argument("--verify", action="store_true", help="验证 TLS 证书（默认不验证）")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--poolsize", type=int, default=DEFAULT_POOLSIZE)
    args = parser.parse_args()

    try:
        raw_template = read_request_file(args.request_file)
    except FileNotFoundError:
        print("无法找到 request 文件:", args.request_file)
        sys.exit(1)

    pairs = find_dollar_pairs(raw_template)
    if len(pairs) == 0 or len(pairs) > 2:
        print("request.txt 中必须包含 1 或 2 对 $...$ 占位。")
        sys.exit(1)

    # 交互式获取范围参数（保留原有交互式范围输入）
    ranges = []
    for i in range(len(pairs)):
        idx = i + 1
        print(f"*** 填写第 {idx} 个爆破占位的参数 ***")
        start = int(input(f"第{idx}个 起始数字 (整数): ").strip())
        end = int(input(f"第{idx}个 终止数字 (整数): ").strip())
        min_digits = int(input(f"第{idx}个 最小位数 (不足则前面补0): ").strip())
        max_digits = int(input(f"第{idx}个 最大位数 (超过则跳过该数字): ").strip())
        if end < start:
            start, end = end, start
        ranges.append((start, end, min_digits, max_digits))

    tasks, skipped = build_tasks(raw_template, args.target, ranges)
    total_tasks = len(tasks)
    if total_tasks == 0:
        print("没有任务，退出。")
        sys.exit(0)
    print(f"任务: {total_tasks}, 跳过: {skipped}")

    rate_limiter = RateLimiter(args.rps if args.rps > 0 else 0)
    results = []
    results_lock = threading.Lock()

    # 使用 ThreadPoolExecutor，initializer 用来为每个线程创建 session
    print(f"开始执行 workers={args.workers}, rps={args.rps}")
    def init():
        thread_init_session(args.max_retries, args.poolsize)

    with ThreadPoolExecutor(max_workers=args.workers, initializer=init) as exe:
        futures = [exe.submit(worker_request, item, rate_limiter, args.timeout, args.verify) for item in tasks]
        completed = 0
        for fut in as_completed(futures):
            res = fut.result()
            with results_lock:
                results.append(res)
            completed += 1
            if completed % 50 == 0 or completed == total_tasks:
                print(f"已完成 {completed}/{total_tasks}")

    # 保存
    try:
        save_results(results, args.output)
        print(f"已保存 {len(results)} 条结果到 {args.output}")
    except Exception as e:
        print("保存失败:", e)


if __name__ == "__main__":
    main()
