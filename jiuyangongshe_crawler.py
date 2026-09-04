# -*- coding: utf-8 -*-
"""
韭研公社 A股/美股 舆情爬虫 v3 - 精简版
适配 GitHub Actions 运行环境

策略1: HTML直接解析（首页/研究优选/热门）
策略2: SSR数据提取（异动页面）
"""

import requests
import re
import json
import os
import time
import random
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger()

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://www.jiuyangongshe.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# A股关键词
KEYWORDS_A = [
    "A股", "上证", "深证", "创业板", "科创板", "北交所", "沪指", "深指",
    "涨停", "跌停", "大盘", "指数", "蓝筹", "龙头", "央行", "证监会",
    "IPO", "注册制", "新能源", "半导体", "医药", "白酒", "银行", "券商",
    "基金", "外资", "北向", "龙虎榜", "主力", "游资", "机构",
    "行情", "收盘", "开盘", "牛股", "板块", "题材", "概念",
    "政策", "利好", "利空", "回购", "并购", "重组", "增发",
    "算力", "AI", "芯片", "光模块", "光通信", "氢能", "光伏",
    "机器人", "低空经济", "碳纤维", "储能", "充电桩", "锂电",
    "华为", "小米", "比亚迪", "宁德时代", "中芯国际",
]

# 美股关键词
KEYWORDS_US = [
    "美股", "纳斯达克", "道琼斯", "标普", "S&P", "NYSE",
    "苹果", "微软", "谷歌", "亚马逊", "特斯拉", "Meta",
    "英伟达", "AMD", "Intel", "美联储", "Fed", "鲍威尔",
    "CPI", "非农", "加息", "降息", "华尔街", "中概股",
    "财报", "季报", "科技股", "FAANG", "ChatGPT", "马斯克",
    "通胀", "失业率", "GDP", "SpaceX", "OpenAI", "TSLA",
    "半导体", "芯片", "AI", "算力",
]

# 排除关键词
EXCLUDE_KW = ["广告", "推广", "合作", "代写"]

# 只保留最近 N 天的文章（首页/研究优选含多日前的置顶热帖，不过滤会混进旧闻）
MAX_AGE_DAYS = 2

_DATE_FULL_RE = re.compile(r'(\d{4})-(\d{1,2})-(\d{1,2})')
_DATE_REL_RE = re.compile(r'(\d+)\s*天前')


def _parse_pub_date(text: str) -> Optional[datetime]:
    """从卡片文本解析发布日期。支持 YYYY-MM-DD 与 'N天前'；解析不出返回 None（视为无法验证新鲜度）。"""
    if not text:
        return None
    m = _DATE_FULL_RE.search(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE_REL_RE.search(text)
    if m:
        return datetime.now() - timedelta(days=int(m.group(1)))
    if any(k in text for k in ("小时前", "分钟前", "刚刚")):
        return datetime.now()
    return None


class JiuyanCrawler:
    """韭研公社舆情爬虫"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.seen_urls: Set[str] = set()
        self.all_articles: List[Dict] = []

    def _delay(self):
        time.sleep(random.uniform(0.3, 0.8))

    def _classify(self, text: str) -> Optional[str]:
        """判断文本属于 A股/美股/None"""
        text_lower = text.lower()
        for kw in EXCLUDE_KW:
            if kw in text_lower:
                return None
        a_score = sum(1 for kw in KEYWORDS_A if kw in text)
        us_score = sum(1 for kw in KEYWORDS_US if kw in text)
        if a_score > 0 and a_score >= us_score:
            return "A股"
        elif us_score > 0:
            return "美股"
        return None

    def _add_article(self, title: str, url: str, source: str, content: str = "", pub_date: Optional[datetime] = None):
        """添加文章（自动去重、日期过滤和分类）"""
        if url in self.seen_urls:
            return
        if not title or len(title) < 8:
            return
        # 新鲜度过滤：解析不到日期或超过 MAX_AGE_DAYS 的旧帖一律丢弃（置顶/热帖常为数日前旧闻）
        if pub_date is None:
            return
        if (datetime.now() - pub_date) > timedelta(days=MAX_AGE_DAYS):
            return

        stock_type = self._classify(title + " " + content)
        if not stock_type:
            return

        self.seen_urls.add(url)
        self.all_articles.append({
            "title": title.strip(),
            "url": url,
            "type": stock_type,
            "source": source,
            "date": pub_date.strftime("%Y-%m-%d"),
            "keywords": [kw for kw in (KEYWORDS_A if stock_type == "A股" else KEYWORDS_US) if kw in title + content][:5],
        })

    def _extract_cards(self, html_text: str, source: str):
        """BS4 解析：把每个 /a/ 文章链接与所在卡片的发布日期（fs13-ash）关联后入库。"""
        if not BeautifulSoup:
            logger.warning("  BeautifulSoup 未安装，跳过 BS4 解析")
            return
        soup = BeautifulSoup(html_text, 'html.parser')
        added = dropped_old = dropped_nodate = 0
        for a in soup.find_all('a', href=re.compile(r'^/a/')):
            title = a.get_text(strip=True)
            node, pub, card_text = a, None, ""
            for _ in range(6):          # 向上最多6层找卡片容器（含 fs13-ash 日期）
                node = node.parent
                if node is None:
                    break
                d = node.find(class_='fs13-ash')
                if d:
                    pub = _parse_pub_date(d.get_text(' ', strip=True))
                    if pub:
                        card_text = node.get_text(' ', strip=True)[:300]
                        break
            if not title:
                title = card_text[:60]
            before = len(self.all_articles)
            self._add_article(title, BASE_URL + a['href'], source, content=card_text, pub_date=pub)
            if len(self.all_articles) > before:
                added += 1
            elif pub is None:
                dropped_nodate += 1
            elif (datetime.now() - pub) > timedelta(days=MAX_AGE_DAYS):
                dropped_old += 1
        logger.info(f"  [{source}] 入库 {added}，过滤旧帖(>{MAX_AGE_DAYS}天) {dropped_old}，无日期丢弃 {dropped_nodate}")

    # --------------------------------------------------------
    # 策略1: HTML正则提取（首页/研究优选/热门）
    # --------------------------------------------------------
    def crawl_html_pages(self):
        """从可直接解析的页面提取文章"""
        pages = [
            (BASE_URL, "首页"),
            (f"{BASE_URL}/study_publish", "研究优选"),
            (f"{BASE_URL}/hot", "热门"),
        ]
        for url, name in pages:
            logger.info(f"[策略1] 抓取 {name}: {url}")
            try:
                self._delay()
                resp = self.session.get(url, timeout=15)
                resp.raise_for_status()
                resp.encoding = 'utf-8'
                self._extract_cards(resp.text, name)
            except Exception as e:
                logger.error(f"  失败: {e}")

    # --------------------------------------------------------
    # 策略2: 异动页面SSR数据提取
    # --------------------------------------------------------
    def crawl_action_page(self):
        """从异动页面提取（SSR渲染）"""
        logger.info(f"[策略2] 抓取 异动页面")
        try:
            self._delay()
            resp = self.session.get(f"{BASE_URL}/action", timeout=15)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            self._extract_cards(resp.text, "异动")
        except Exception as e:
            logger.error(f"  异动页面失败: {e}")

    # --------------------------------------------------------
    # 主入口
    # --------------------------------------------------------
    def crawl_all(self) -> List[Dict]:
        """执行全部抓取策略"""
        logger.info("=" * 60)
        logger.info(f"韭研公社舆情抓取 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        self.crawl_html_pages()
        self.crawl_action_page()

        zh = [a for a in self.all_articles if a["type"] == "A股"]
        us = [a for a in self.all_articles if a["type"] == "美股"]

        logger.info(f"{'=' * 60}")
        logger.info(f"抓取完成！共 {len(self.all_articles)} 篇")
        logger.info(f"  A股: {len(zh)} 篇")
        logger.info(f"  美股: {len(us)} 篇")
        logger.info(f"{'=' * 60}")

        return self.all_articles
