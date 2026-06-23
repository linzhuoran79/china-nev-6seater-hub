#!/usr/bin/env python3
"""Collect mutual-fund application and launch records for E Fund and GF Fund.

Sources:
- NERIS CSRC administrative approval progress API.
- E Fund official website product and disclosure APIs.
- GF Fund official website fund list, WAS disclosure search, and fund detail pages.

The script is intentionally verbose in its outputs so the resulting workbook can
be checked back to official pages/PDFs.
"""

from __future__ import annotations

import io
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests
from pypdf import PdfReader


START = date(2025, 1, 1)
END = date(2026, 3, 31)
# Launch announcements can be published before the sale start, and products sold
# late in Q1 can be established after Q1. These windows keep the crawler
# complete for the requested period without scanning every historical product.
ANNOUNCEMENT_LOOKBACK_START = date(2024, 10, 1)
ESTABLISHMENT_LOOKAHEAD_END = date(2026, 6, 30)
OUT_DIR = Path("outputs/fund_records")
RAW_DIR = OUT_DIR / "raw"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
PDF_TEXT_CACHE: dict[str, tuple[str, str]] = {}

NERIS_URL = "https://neris.csrc.gov.cn/alappr-delare/home/approval-progress/v1/list"
EFUNDS_LIST_URL = "https://www.efunds.com.cn/Html5/lm/jjcp/"
EFUNDS_CONTENTS_URL = "https://api.efunds.com.cn/xcowch/front/contents"
GF_ALL_FUNDS_URL = "https://www.gffunds.com.cn/funds/cpmp/allFunds.js"
GF_WAS_URL = "https://www.gffunds.com.cn/was5/web/search"
GF_JSON_URL = "https://www.gffunds.com.cn/apistore/JsonService"


@dataclass
class LaunchDoc:
    manager: str
    product_name: str
    share_codes: set[str]
    short_names: set[str]
    fund_type: str
    custodian: str
    announcement_date: str
    sale_start: str
    sale_end: str
    source_title: str
    source_url: str
    detail_url: str
    source_system: str
    pdf_extract_status: str
    verification_channel: str


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: int = 30,
    retries: int = 4,
    **kwargs: Any,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(value[:10] if fmt != "%Y%m%d" else value[:8], fmt).date()
        except ValueError:
            pass
    return None


def in_range(value: str | None) -> bool:
    parsed = parse_date(value)
    return bool(parsed and START <= parsed <= END)


def in_candidate_establishment_window(value: str | None) -> bool:
    parsed = parse_date(value)
    return bool(parsed and START <= parsed <= ESTABLISHMENT_LOOKAHEAD_END)


def in_candidate_announcement_window(value: str | None) -> bool:
    parsed = parse_date(value)
    return bool(parsed and ANNOUNCEMENT_LOOKBACK_START <= parsed <= END)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def classify_product_type(name: str, fallback: str = "") -> str:
    source = f"{name} {fallback}"
    if "REIT" in source or "不动产投资信托" in source:
        return "REITs"
    if "FOF" in source or "基金中基金" in source:
        return "FOF"
    if "货币" in source:
        return "货币型"
    if "同业存单" in source:
        return "同业存单指数"
    if "纯债" in source:
        return "纯债/债券型"
    if "债券" in source or "债" in source:
        return "债券型/固收"
    if "指数" in source or "ETF" in source or "交易型开放式" in source or "联接" in source:
        return "指数型/ETF/联接"
    if "QDII" in source:
        return "QDII"
    if "股票" in source:
        return "股票型"
    if "混合" in source:
        return "混合型"
    return fallback or "未识别"


def extract_product_from_neris(title: str) -> tuple[str, str]:
    inner = ""
    m = re.search(r"《([^》]+)》", title)
    if m:
        inner = m.group(1)
    else:
        m = re.search(r"《?([^《》]+基金[^《》]*)》?", title)
        inner = m.group(1) if m else title
    if "-" in inner:
        app_type, product = inner.split("-", 1)
    else:
        app_type, product = "", inner
    return app_type.strip(), product.strip()


def collect_neris_records(session: requests.Session) -> pd.DataFrame:
    companies = ["易方达基金管理有限公司", "广发基金管理有限公司"]
    rows: list[dict[str, Any]] = []

    for company in companies:
        page_size = 100
        first = request_with_retry(
            session,
            "GET",
            NERIS_URL,
            params={
                "appMatrCde": "",
                "queryCondition": company,
                "pageNum": 1,
                "pageSize": page_size,
            },
            headers={**HEADERS, "Referer": "https://neris.csrc.gov.cn/alappr-delare-front/"},
        ).json()
        total = int(first["data"]["total"])
        pages = math.ceil(total / page_size)
        all_records = list(first["data"]["records"])

        for page in range(2, pages + 1):
            data = request_with_retry(
                session,
                "GET",
                NERIS_URL,
                params={
                    "appMatrCde": "",
                    "queryCondition": company,
                    "pageNum": page,
                    "pageSize": page_size,
                },
                headers={**HEADERS, "Referer": "https://neris.csrc.gov.cn/alappr-delare-front/"},
            ).json()
            all_records.extend(data["data"]["records"])
            time.sleep(0.05)

        for rec in all_records:
            title = rec.get("showCntnt", "")
            if "募集申请注册" not in title:
                continue
            if "变更注册" in title:
                continue
            flows = rec.get("aprvSchdPubFlowViewResultList") or []
            flow_map = {f.get("taskName", ""): f.get("fnshDate", "") for f in flows}
            report_date = flow_map.get("接收材料") or rec.get("appDate")
            if not in_range(report_date):
                continue
            app_type, product = extract_product_from_neris(title)
            rows.append(
                {
                    "基金公司": company,
                    "产品名称": product,
                    "产品类型_规则识别": classify_product_type(product),
                    "申请事项": app_type,
                    "报会日期_接收材料": report_date,
                    "主列表日期": rec.get("appDate", ""),
                    "受理通知日期": flow_map.get("受理通知", ""),
                    "反馈意见日期": flow_map.get("书面反馈", ""),
                    "行政许可决定日期": flow_map.get("行政许可决定书", ""),
                    "全部进度节点": "; ".join(
                        f"{f.get('taskName','')}:{f.get('fnshDate','')}" for f in flows if f.get("taskName")
                    ),
                    "NERIS标题": title,
                    "NERIS查询URL": (
                        "https://neris.csrc.gov.cn/alappr-delare-front/#/home/toPubFlow"
                        f"?queryCondition={company}"
                    ),
                    "核验方式": "中国证监会行政许可网上办理-审批进度公示，按基金公司或产品名称检索。",
                }
            )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["基金公司", "报会日期_接收材料", "产品名称"]).reset_index(drop=True)
    return df


def extract_json_array_assignment(text: str, var_name: str) -> list[dict[str, Any]]:
    marker = f"var {var_name} = "
    start = text.find(marker)
    if start == -1:
        raise ValueError(f"Cannot find {var_name}")
    start = text.find("[", start)
    depth = 0
    in_str = False
    escape = False
    end = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end is None:
        raise ValueError(f"Cannot parse {var_name}")
    return json.loads(text[start:end])


def get_efunds_products(session: requests.Session) -> dict[str, dict[str, Any]]:
    response = request_with_retry(session, "GET", EFUNDS_LIST_URL, headers=HEADERS)
    text = response.content.decode("utf-8", "replace")
    data = extract_json_array_assignment(text, "__FUND_SUPER_MARKET_DATA__")
    grouped: dict[str, dict[str, Any]] = {}
    for item in data:
        full_name = clean_text(item.get("fundSourceName")) or clean_text(item.get("fundname"))
        if not full_name:
            continue
        entry = grouped.setdefault(
            full_name,
            {
                "product_name": full_name,
                "share_codes": set(),
                "short_names": set(),
                "fund_type": clean_text(item.get("fundType")),
                "setupdate": clean_text(item.get("setupdate")),
                "detail_url": "",
            },
        )
        code = clean_text(item.get("fundcode"))
        if code:
            entry["share_codes"].add(code)
            if not entry["detail_url"]:
                entry["detail_url"] = f"https://www.efunds.com.cn/Html5/fund/{code}.shtml"
        short_name = clean_text(item.get("fundname"))
        if short_name:
            entry["short_names"].add(short_name)
        if not entry.get("fund_type"):
            entry["fund_type"] = clean_text(item.get("fundType"))
    return grouped


def efunds_search_sale_doc(session: requests.Session, code: str) -> dict[str, Any] | None:
    payload = {
        "siteID": "1",
        "catalogAlias": "xxplflwj,xxpldqgg,xxpllsgg",
        "title": "基金份额发售公告",
        "fundCode": code,
        "isIncludeTransFund": "Y",
        "platformID": "Html5",
        "prop1": f"{START.isoformat()},2026-06-23",
        "pageSize": 10,
        "pageIndex": 0,
    }
    response = request_with_retry(
        session,
        "POST",
        EFUNDS_CONTENTS_URL,
        json=payload,
        headers={**HEADERS, "Referer": f"https://www.efunds.com.cn/Html5/fund/{code}.shtml"},
    )
    data = response.json()
    docs = (data.get("data") or {}).get("data") or []
    for doc in docs:
        title = clean_text(doc.get("title"))
        if "基金份额发售公告" in title:
            return doc
    return None


def parse_efunds_detail(session: requests.Session, url: str) -> dict[str, str]:
    if not url:
        return {}
    response = request_with_retry(session, "GET", url, headers=HEADERS)
    text = response.content.decode("utf-8", "replace")
    result: dict[str, str] = {}
    patterns = {
        "custodian": r"基金托管人：\s*</td>\s*<td[^>]*>\s*([^<]+)",
        "fund_type": r"基金类型：\s*</td>\s*<td[^>]*>\s*([^<]+)",
        "setupdate": r"成立日期：\s*</td>\s*<td[^>]*>\s*([^<]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            result[key] = clean_text(match.group(1))
    return result


def parse_gf_all_funds(session: requests.Session) -> list[dict[str, str]]:
    response = request_with_retry(session, "GET", GF_ALL_FUNDS_URL, headers=HEADERS)
    text = response.content.decode("utf-8", "replace")
    pattern = re.compile(
        r"\{\s*fCode:\s*'(?P<code>[^']*)',\s*"
        r"fName:\s*'(?P<name>[^']*)',\s*"
        r"fType:\s*'(?P<type>[^']*)',\s*"
        r"fPY:\s*'(?P<py>[^']*)',\s*"
        r"fCreateDate:\s*'(?P<create>[^']*)',\s*"
        r"fTheme:\s*'(?P<theme>[^']*)',\s*"
        r"fManager:\s*'(?P<manager>[^']*)',\s*"
        r"url:\s*'(?P<url>[^']*)'\s*\}",
        re.S,
    )
    return [m.groupdict() for m in pattern.finditer(text)]


def parse_gf_was_records(script_text: str) -> list[dict[str, str]]:
    records = []
    pattern = re.compile(
        r'\{"DOCTITLE":"(?P<title>.*?)","DOCPUBURL":"(?P<url>.*?)"\.replace\(/\^http:/, ""\),"DOCRELTIME":"(?P<time>.*?)"\}',
        re.S,
    )
    for m in pattern.finditer(script_text):
        title = clean_text(m.group("title"))
        url = clean_text(m.group("url"))
        rel_time = clean_text(m.group("time"))
        if title and url:
            if url.startswith("http://"):
                url = "https://" + url[len("http://") :]
            records.append({"title": title, "url": url, "time": rel_time})
    return records


def gf_search_sale_docs(session: requests.Session, code: str) -> list[dict[str, str]]:
    response = request_with_retry(
        session,
        "POST",
        GF_WAS_URL,
        data={"channelid": 200445, "page": 1, "searchword": code},
        headers={**HEADERS, "Referer": "https://www.gffunds.com.cn/funds/"},
    )
    text = response.content.decode("utf-8", "replace")
    return [r for r in parse_gf_was_records(text) if "基金份额发售公告" in r["title"]]


def parse_gf_detail(session: requests.Session, url: str) -> dict[str, str]:
    if not url:
        return {}
    url = url.replace("http://", "https://")
    response = request_with_retry(session, "GET", url, headers=HEADERS)
    text = response.content.decode("utf-8", "replace")
    result: dict[str, str] = {}
    patterns = {
        "custodian": r"基金托管人\s*:\s*</td>\s*<td[^>]*>\s*([^<]+)",
        "setupdate": r"成立日期\s*:\s*</td>\s*<td[^>]*>\s*([^<]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            result[key] = clean_text(match.group(1))
    return result


def gf_base_info(session: requests.Session, code: str) -> dict[str, str]:
    response = request_with_retry(
        session,
        "GET",
        GF_JSON_URL,
        params={"service": "BaseInfo", "method": "Fund", "op": "queryFundByGFFundcode", "fundcode": code},
        headers={**HEADERS, "Referer": "https://www.gffunds.com.cn/funds/"},
    )
    data = response.json().get("data") or []
    if not data:
        return {}
    item = data[0]
    return {
        "product_name": clean_text(item.get("FUNDFULLNAME")) or clean_text(item.get("FUNDNAME")),
        "fund_type": clean_text(item.get("CATEGORYNAME")),
        "setupdate": clean_text(item.get("CREATEDATE")),
    }


def normalize_cn_date(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def extract_sale_period(pdf_text: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", pdf_text)
    date_pat = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    patterns = [
        rf"发售日期[:：]?\s*{date_pat}\s*(?:至|起至|-|—|~)\s*{date_pat}",
        rf"本基金(?:将)?自\s*{date_pat}\s*(?:至|起至|-|—|~)\s*{date_pat}\s*(?:公开)?发售",
        rf"募集期为\s*{date_pat}\s*(?:至|起至|-|—|~)\s*{date_pat}",
        rf"发售时间为\s*{date_pat}\s*(?:至|起至|-|—|~)\s*{date_pat}",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            groups = m.groups()
            return normalize_cn_date(*groups[:3]), normalize_cn_date(*groups[3:6])
    return "", ""


def extract_custodian_from_pdf(pdf_text: str) -> str:
    text = re.sub(r"\s+", " ", pdf_text)
    for pattern in [
        r"基金托管人[:：]\s*([^ ]+?(?:银行|证券|股份有限公司|有限责任公司))",
        r"托管人为\s*([^ ，。；;]+?(?:银行|证券|股份有限公司|有限责任公司))",
        r"基金托管人为\s*([^ ，。；;]+?(?:银行|证券|股份有限公司|有限责任公司))",
    ]:
        m = re.search(pattern, text)
        if m:
            return clean_text(m.group(1))
    return ""


def read_pdf_text(session: requests.Session, url: str) -> tuple[str, str]:
    if url in PDF_TEXT_CACHE:
        return PDF_TEXT_CACHE[url]
    try:
        response = request_with_retry(session, "GET", url, headers=HEADERS, timeout=15, retries=2)
        reader = PdfReader(io.BytesIO(response.content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        result = (text, "OK")
    except Exception as exc:  # noqa: BLE001
        result = ("", f"PDF解析失败: {exc}")
    PDF_TEXT_CACHE[url] = result
    return result


def collect_launch_records(session: requests.Session) -> pd.DataFrame:
    launches: dict[str, LaunchDoc] = {}

    # E Fund official website.
    efunds_products = get_efunds_products(session)
    efunds_candidates = [
        p for p in efunds_products.values() if in_candidate_establishment_window(p.get("setupdate", ""))
    ]
    print(f"E Fund launch candidates after establishment-date prefilter: {len(efunds_candidates)}", flush=True)
    for idx, product in enumerate(efunds_candidates, start=1):
        codes = sorted(product["share_codes"])
        if not codes:
            continue
        doc = efunds_search_sale_doc(session, codes[0])
        if not doc:
            continue
        announcement_date = clean_text(doc.get("prop1")) or clean_text(doc.get("publishDate"))[:10]
        if not in_candidate_announcement_window(announcement_date):
            continue
        pdf_url = clean_text(doc.get("path"))
        pdf_text, status = read_pdf_text(session, pdf_url)
        sale_start, sale_end = extract_sale_period(pdf_text)
        if not (in_range(sale_start) or in_range(announcement_date)):
            continue
        detail = parse_efunds_detail(session, product.get("detail_url", ""))
        custodian = detail.get("custodian") or extract_custodian_from_pdf(pdf_text)
        fund_type = detail.get("fund_type") or product.get("fund_type") or classify_product_type(product["product_name"])
        key = f"易方达基金管理有限公司|{product['product_name']}|{pdf_url}"
        launches[key] = LaunchDoc(
            manager="易方达基金管理有限公司",
            product_name=product["product_name"],
            share_codes=set(codes),
            short_names=set(product["short_names"]),
            fund_type=fund_type,
            custodian=custodian,
            announcement_date=announcement_date,
            sale_start=sale_start,
            sale_end=sale_end,
            source_title=clean_text(doc.get("title")),
            source_url=pdf_url,
            detail_url=product.get("detail_url", ""),
            source_system="易方达基金官网 disclosure API + PDF",
            pdf_extract_status=status,
            verification_channel="基金公司官网PDF；证监会基金电子披露网站按公告标题/基金名称复核。",
        )
        if idx % 10 == 0:
            print(f"  E Fund candidates processed: {idx}/{len(efunds_candidates)}", flush=True)
            time.sleep(0.2)

    # GF Fund official website.
    gf_funds = [
        item for item in parse_gf_all_funds(session) if in_candidate_establishment_window(item.get("create", ""))
    ]
    print(f"GF Fund launch candidates after establishment-date prefilter: {len(gf_funds)}", flush=True)
    for idx, item in enumerate(gf_funds, start=1):
        code = item["code"]
        docs = gf_search_sale_docs(session, code)
        if not docs:
            continue
        detail = parse_gf_detail(session, item.get("url", ""))
        base = gf_base_info(session, code)
        for doc in docs:
            announcement_date = doc["time"][:10].replace(".", "-")
            if not in_candidate_announcement_window(announcement_date):
                continue
            pdf_url = doc["url"]
            pdf_text, status = read_pdf_text(session, pdf_url)
            sale_start, sale_end = extract_sale_period(pdf_text)
            if not (in_range(sale_start) or in_range(announcement_date)):
                continue
            product_name = base.get("product_name") or re.sub(r"基金份额发售公告$", "", doc["title"])
            key = f"广发基金管理有限公司|{product_name}|{pdf_url}"
            existing = launches.get(key)
            if existing:
                existing.share_codes.add(code)
                existing.short_names.add(item["name"])
                continue
            custodian = detail.get("custodian") or extract_custodian_from_pdf(pdf_text)
            fund_type = base.get("fund_type") or item.get("type") or classify_product_type(product_name)
            launches[key] = LaunchDoc(
                manager="广发基金管理有限公司",
                product_name=product_name,
                share_codes={code},
                short_names={item["name"]},
                fund_type=fund_type,
                custodian=custodian,
                announcement_date=announcement_date,
                sale_start=sale_start,
                sale_end=sale_end,
                source_title=doc["title"],
                source_url=pdf_url,
                detail_url=item.get("url", "").replace("http://", "https://"),
                source_system="广发基金官网 WAS 法律文件检索 + PDF",
                pdf_extract_status=status,
                verification_channel="基金公司官网PDF；证监会基金电子披露网站按公告标题/基金名称复核。",
            )
        if idx % 10 == 0:
            print(f"  GF Fund candidates processed: {idx}/{len(gf_funds)}", flush=True)
            time.sleep(0.2)

    rows = []
    for launch in launches.values():
        rows.append(
            {
                "基金公司": launch.manager,
                "产品名称": launch.product_name,
                "份额代码": ", ".join(sorted(launch.share_codes)),
                "份额简称": "; ".join(sorted(launch.short_names)),
                "产品类型": launch.fund_type or classify_product_type(launch.product_name),
                "托管行/托管人": launch.custodian,
                "首发公告日期": launch.announcement_date,
                "首发起始日": launch.sale_start,
                "首发截止日": launch.sale_end,
                "首发区间": (
                    f"{launch.sale_start} 至 {launch.sale_end}" if launch.sale_start and launch.sale_end else ""
                ),
                "公告标题": launch.source_title,
                "公告PDF链接": launch.source_url,
                "基金详情页": launch.detail_url,
                "来源系统": launch.source_system,
                "PDF抽取状态": launch.pdf_extract_status,
                "核验渠道": launch.verification_channel,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["基金公司", "首发起始日", "产品名称"]).reset_index(drop=True)
    return df


def write_workbook(report_df: pd.DataFrame, launch_df: pd.DataFrame) -> Path:
    output_path = OUT_DIR / "易方达_广发_2025_2026Q1_报会与首发记录.xlsx"
    readme_rows = [
        {
            "项目": "统计期间",
            "说明": f"{START.isoformat()} 至 {END.isoformat()}（2025全年 + 2026Q1）",
        },
        {
            "项目": "报会记录口径",
            "说明": "NERIS 审批进度公示中，标题含“募集申请注册”且不含“变更注册”，按“接收材料”日期过滤。",
        },
        {
            "项目": "首发产品口径",
            "说明": "基金公司官网披露的《基金份额发售公告》，首发起始日或公告日期落入统计期间；A/C等份额按产品全称合并。",
        },
        {
            "项目": "官方核验",
            "说明": "报会：neris.csrc.gov.cn 审批进度公示；首发：基金公司官网PDF + eid.csrc.gov.cn/fund 按公告标题/基金名称查询。",
        },
        {
            "项目": "注意",
            "说明": "托管人为证券公司的ETF/联接基金保留“托管人”原文；字段名写作“托管行/托管人”。",
        },
    ]
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(readme_rows).to_excel(writer, index=False, sheet_name="README_口径与核验")
        report_df.to_excel(writer, index=False, sheet_name="报会记录_NERIS")
        launch_df.to_excel(writer, index=False, sheet_name="首发产品_官网PDF")

        # A simple cross-check sheet: product names that appear in both tables by normalized prefix.
        cross_rows = []
        for _, r in report_df.iterrows():
            report_name = r.get("产品名称", "")
            matches = launch_df[launch_df["产品名称"].astype(str).apply(lambda x: x in report_name or report_name in x)]
            for _, l in matches.iterrows():
                cross_rows.append(
                    {
                        "报会产品名称": report_name,
                        "报会日期": r.get("报会日期_接收材料", ""),
                        "首发产品名称": l.get("产品名称", ""),
                        "首发区间": l.get("首发区间", ""),
                        "公告PDF链接": l.get("公告PDF链接", ""),
                    }
                )
        pd.DataFrame(cross_rows).to_excel(writer, index=False, sheet_name="报会_首发名称交叉匹配")
    return output_path


def main() -> None:
    ensure_dirs()
    session = requests.Session()
    session.headers.update(HEADERS)

    print("Collecting NERIS application records...", flush=True)
    report_df = collect_neris_records(session)
    report_json = RAW_DIR / "neris_report_records.json"
    report_json.write_text(report_df.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
    print(f"NERIS records: {len(report_df)}", flush=True)

    print("Collecting launch records from fund company websites...", flush=True)
    launch_df = collect_launch_records(session)
    launch_json = RAW_DIR / "launch_records.json"
    launch_json.write_text(launch_df.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
    print(f"Launch records: {len(launch_df)}", flush=True)

    output_path = write_workbook(report_df, launch_df)
    print(f"Wrote {output_path}", flush=True)


if __name__ == "__main__":
    main()
