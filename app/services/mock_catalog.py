"""站内 mock 目录页，模拟可采集列表（无外网依赖）。"""

from html import escape

CATALOGS = {
    "/mock/catalog/alpha": [
        {"sku": "A-001", "title": "市政管线测绘记录表"},
        {"sku": "A-002", "title": "排水井巡查台账"},
        {"sku": "A-003", "title": "排水井巡查台账"},
    ],
    "/mock/catalog/beta": [
        {"sku": "B-101", "title": "泵站运行日报"},
        {"sku": "B-102", "title": "雨污分流抽检单"},
    ],
    # 空目录：开跑一条都取不到，用于演练急件“落空”（两种落空口径都以此验证）。
    "/mock/catalog/empty": [],
    # 来源不可用：页面 404，但站内仍登记了应收条目，用于演练“来源取不到 →
    # 急件落空、条目全进候着区”的路径。
    "/mock/catalog/flaky": [
        {"sku": "F-001", "title": "供水抢修工单"},
        {"sku": "F-002", "title": "管网压力异常单"},
    ],
}

UNAVAILABLE_CATALOGS = {"/mock/catalog/flaky"}


def render_catalog_page(path: str) -> str:
    items = CATALOGS.get(path)
    if items is None or path in UNAVAILABLE_CATALOGS:
        return _not_found(path)
    rows = "".join(
        f"<li data-sku='{escape(it['sku'])}'>{escape(it['title'])}</li>"
        for it in items
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Mock 目录 {escape(path)}</title></head>
<body>
<h1>采集演练目录</h1>
<p class="seed">{escape(path)}</p>
<ul id="items">{rows}</ul>
</body></html>"""


def _not_found(path: str) -> str:
    return f"<html><body><h1>404</h1><p>{escape(path)}</p></body></html>", 404


def parse_catalog_html(html: str, seed_path: str) -> list[dict]:
    """极简解析：从 &lt;li data-sku&gt; 抽条目（基线够用，后续题可换解析器）。"""
    import re

    pattern = re.compile(
        r"<li\s+data-sku='([^']+)'\s*>([^<]+)</li>",
        re.IGNORECASE,
    )
    out = []
    for sku, title in pattern.findall(html):
        out.append({"sku": sku, "title": title.strip(), "seed_path": seed_path})
    return out
