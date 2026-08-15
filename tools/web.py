import requests
from baidusearch.baidusearch import search


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """百度直搜，国内直连"""
    try:
        results = search(query, num_results=max_results)
        if results:
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("abstract", "")[:500]
                }
                for r in results
            ]
    except Exception as e:
        return [{"error": str(e)}]
    return [{"error": "搜索无结果"}]


# ---------- 网页抓取（多后端回退）----------
def web_fetch(url: str) -> str:
    """
    网页抓取，按以下顺序尝试：
    1. 直连目标网站（最快，适合静态页）
    2. r.jina.ai（Jina Reader，markdown 质量最优）
    3. markdown.new（Cloudflare 专用）
    4. defuddle.md（备用方案）
    """

    # 回退链路定义
    backends = [
        # 1. 直连（带浏览器 UA，避免被拒）
        {
            "name": "direct",
            "url": url,
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        },
        # 2. Jina Reader
        {
            "name": "jina",
            "url": f"https://r.jina.ai/{url}",
            "headers": {"Accept": "text/plain"},
        },
        # 3. markdown.new
        {
            "name": "markdown.new",
            "url": f"https://markdown.new/{url}",
            "headers": {"Accept": "text/plain"},
        },
        # 4. defuddle.md
        {
            "name": "defuddle",
            "url": f"https://defuddle.md/{url}",
            "headers": {"Accept": "text/plain"},
        },
    ]

    for backend in backends:
        try:
            resp = requests.get(
                backend["url"],
                headers=backend["headers"],
                timeout=15,
                allow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.text.strip()) > 200:
                # 直连返回的是 HTML，需要简单清洗
                if backend["name"] == "direct":
                    import re
                    #  crude HTML → text
                    text = re.sub(r'<script[\s\S]*?</script>', '', resp.text)
                    text = re.sub(r'<style[\s\S]*?</style>', '', text)
                    text = re.sub(r'<[^>]+>', ' ', text)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if len(text) < 200:
                        continue  # 直连没抓到实质内容，试下一个
                    return f"[via direct] {text[:8000]}"
                return f"[via {backend['name']}] {resp.text[:8000]}"
        except Exception:
            # 超时/连接失败，自动尝试下一个后端
            continue

    return f"所有抓取后端均失败: {url}"
