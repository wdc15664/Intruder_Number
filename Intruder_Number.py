"""
依赖：
    pip install requests openpyxl
"""

import re
import sys
import requests
from urllib.parse import urlparse, urlunparse
from openpyxl import Workbook

REQUEST_FILE = "request.txt"
OUTPUT_XLSX = "results.xlsx"
EXCEL_CELL_LIMIT = 32767  # Excel 单元格字符上限近似
SNIPPET_LEN = 500
TIMEOUT = 30

requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)


def read_request_file(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    return raw


def split_request(raw):
    # split into head and body by first blank line (support \r\n\r\n 或 \n\n)
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
    # 找到非贪婪的 $...$ 对
    pairs = re.findall(r"\$(.*?)\$", raw, flags=re.S)
    return pairs


def prompt_int(prompt_text):
    while True:
        s = input(prompt_text).strip()
        try:
            return int(s)
        except ValueError:
            print("请输入整数。")


def format_number(n, min_d, max_d):
    s = str(n)
    if len(s) < min_d:
        s = s.zfill(min_d)
    if len(s) > max_d:
        return None
    return s


def build_url_from_request_line(req_line, target_host, headers):
    # req_line 示例: "GET /v.gif?id=123 HTTP/1.1" 或 "GET http://a/b HTTP/1.1"
    toks = req_line.split()
    if len(toks) < 2:
        raise ValueError("无法解析请求行: " + req_line)
    path_or_url = toks[1]
    parsed = urlparse(path_or_url)
    if parsed.scheme and parsed.netloc:
        # absolute URL; 替换 netloc 为 target_host
        new = parsed._replace(netloc=target_host)
        return urlunparse(new)
    else:
        # path-only -> 由 scheme + target_host + path 构造
        # 尝试以 Referer 判断 scheme（若 Referer header 有 https 开头则用 https）
        scheme = "http"
        ref = headers.get("Referer", "")
        if ref.lower().startswith("https://"):
            scheme = "https"
        path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
        return f"{scheme}://{target_host}{path}"


def main():
    try:
        raw_template = read_request_file(REQUEST_FILE)
    except FileNotFoundError:
        print(f"未找到 {REQUEST_FILE}，请确认文件存在并与脚本在同一目录。")
        sys.exit(1)

    # 查找 $ 对
    pairs = find_dollar_pairs(raw_template)
    num_pairs = len(pairs)
    if num_pairs == 0:
        print("request.txt 中未发现 $...$ 占位，至少需要一对。")
        sys.exit(1)
    if num_pairs > 2:
        print("爆破位置超过两个，已停止")
        sys.exit(1)

    print(f"在 request.txt 中发现 {num_pairs} 对 $...$ 占位（非贪婪匹配）。")

    target = input("请输入目标 Host（例如 example.com 或 example.com:8080）：").strip()
    if not target:
        print("必须输入目标 Host。")
        sys.exit(1)

    ranges = []
    for i in range(num_pairs):
        idx = i + 1
        print(f"*** 填写第 {idx} 个爆破占位的参数 ***")
        start = prompt_int(f"第{idx}个 起始数字 (整数): ")
        end = prompt_int(f"第{idx}个 终止数字 (整数): ")
        min_digits = prompt_int(f"第{idx}个 最小位数 (不足则前面补0): ")
        max_digits = prompt_int(f"第{idx}个 最大位数 (超过则跳过该数字): ")
        if end < start:
            print("终止数字小于起始数字，已交换。")
            start, end = end, start
        ranges.append((start, end, min_digits, max_digits))

    # Excel 初始化
    wb = Workbook()
    ws = wb.active
    ws.title = "results"
    ws.append(["payload_desc", "url", "status_code", "response_length_bytes", "response_snippet", "response_full_truncated"])

    session = requests.Session()
    total = 0
    skipped = 0

    # 两种情况：1 对或 2 对
    if num_pairs == 1:
        a_start, a_end, a_min, a_max = ranges[0]
        for a in range(a_start, a_end + 1):
            fa = format_number(a, a_min, a_max)
            if fa is None:
                print(f"跳过第1个数字 {a}（长度超过最大位数 {a_max}）")
                skipped += 1
                continue
            # 用 fa 替换第一对 $...$（非贪婪）
            filled = re.sub(r"\$(.*?)\$", fa, raw_template, count=1, flags=re.S)
            # 解析 filled 的请求行/headers/body（保证替换可发生在 path/header/body 任意处）
            req_line, headers, body = split_request(filled)
            # 使用用户 target 覆盖 Host header（或添加）
            headers["Host"] = target
            url = build_url_from_request_line(req_line, target, headers)
            method = req_line.split()[0]
            try:
                resp = session.request(method, url, headers=headers, data=body.encode("utf-8") if body else None, timeout=TIMEOUT, verify=False)
                content_text = resp.text
                length = len(resp.content)
                snippet = content_text[:SNIPPET_LEN]
                truncated = content_text[:EXCEL_CELL_LIMIT]
                ws.append([f"a={fa}", url, resp.status_code, length, snippet, truncated])
                total += 1
                print(f"[{total}] a={fa} -> {resp.status_code}, len={length}")
            except Exception as e:
                ws.append([f"a={fa}", url, "ERROR", 0, str(e)[:SNIPPET_LEN], ""])
                print("请求失败：", e)
                total += 1

    else:  # num_pairs == 2
        a_start, a_end, a_min, a_max = ranges[0]
        b_start, b_end, b_min, b_max = ranges[1]
        for a in range(a_start, a_end + 1):
            fa = format_number(a, a_min, a_max)
            if fa is None:
                print(f"跳过第1个数字 {a}（长度超过最大位数 {a_max}）")
                skipped += 1
                continue
            for b in range(b_start, b_end + 1):
                fb = format_number(b, b_min, b_max)
                if fb is None:
                    print(f"跳过第2个数字 {b}（长度超过最大位数 {b_max}）")
                    skipped += 1
                    continue
                # 依次替换两个占位（先替换第一个出现的，再替换下一个）
                filled = raw_template
                filled = re.sub(r"\$(.*?)\$", fa, filled, count=1, flags=re.S)
                filled = re.sub(r"\$(.*?)\$", fb, filled, count=1, flags=re.S)
                req_line, headers, body = split_request(filled)
                headers["Host"] = target
                url = build_url_from_request_line(req_line, target, headers)
                method = req_line.split()[0]
                payload_desc = f"a={fa};b={fb}"
                try:
                    resp = session.request(method, url, headers=headers, data=body.encode("utf-8") if body else None, timeout=TIMEOUT, verify=False)
                    content_text = resp.text
                    length = len(resp.content)
                    snippet = content_text[:SNIPPET_LEN]
                    truncated = content_text[:EXCEL_CELL_LIMIT]
                    ws.append([payload_desc, url, resp.status_code, length, snippet, truncated])
                    total += 1
                    print(f"[{total}] {payload_desc} -> {resp.status_code}, len={length}")
                except Exception as e:
                    ws.append([payload_desc, url, "ERROR", 0, str(e)[:SNIPPET_LEN], ""])
                    print("请求失败：", e)
                    total += 1

    # 保存
    try:
        wb.save(OUTPUT_XLSX)
        print(f"已保存结果到 {OUTPUT_XLSX}。总请求数: {total}; 跳过: {skipped}")
    except Exception as e:
        print("保存 Excel 失败:", e)


if __name__ == "__main__":
    main()
