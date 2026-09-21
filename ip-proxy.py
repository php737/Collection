# -*- coding: utf-8 -*-
# Project: py
# Author: Penton
# Email: cGhwNzEzM0BnbWFpbC5jb20=
# Date: 2026/9/11
# Copyright (c) 2026 Penton. All Rights Reserved.

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------- 配置区 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, 'proxy_data')
TXT_FILE = os.path.join(OUTPUT_DIR, 'proxies.txt')

SOURCE_URL = ('https://www.zdaye.com/free/'
              '?ip_adr=&checktime=4&sleep=3&cunhuo=1&dengji=&protocol=https&yys=&px=')

TEST_URL = 'https://ping0.cc/geo'   # 测试目标：返回代理出口 IP 的归属信息
TEST_TIMEOUT = 7                    # 单个代理测试超时（秒）
MAX_WORKERS = 10                    # 并发测试线程数

# 采集源站时使用的本地代理（如不需要可设为 None）
LOCAL_PROXY = 'socks5h://192.168.8.2:1081'

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
}


class ProxyPool:
    # 只保留这些协议（http 会被过滤掉）
    ALLOW_PROTOCOLS = {'https', 'socks4', 'socks5'}

    def __init__(self, local_proxy=LOCAL_PROXY):
        self.local_proxy = local_proxy
        self.proxies = ({'http': local_proxy, 'https': local_proxy}
                        if local_proxy else None)

    # ---------- 解析工具 ----------
    @staticmethod
    def _text(node):
        return node.get_text(strip=True) if node else ''

    # ---------- 1. 采集（单页） ----------
    def _parse_page(self, html):
        """解析页面 HTML，返回 list[dict]（已按协议过滤）"""
        soup = BeautifulSoup(html, 'html.parser')

        container = soup.find(class_='ip-table-container')
        body = container.find(class_='ip-table-body') if container else None
        rows = body.find_all(class_='ul-row') if body else []
        if not rows:
            logging.warning('未解析到代理行，页面结构可能已变化')
            return []

        result = []
        seen = set()
        for row in rows:
            ip = self._text(row.find(class_='proxy_ip'))
            port = self._text(row.find(class_='proxy_port')).replace('Port：', '').strip()
            protocol = self._text(row.find(class_='proxy_protocol')).lower()
            level = self._text(row.find(class_='proxy_level'))
            city_li = row.find('li', title=True)
            city = city_li.get('title', '').strip() if city_li else ''
            survival = self._text(row.find(class_='ul-cell mtd'))

            if not ip or not port:
                continue

            # 过滤掉 http 等不在白名单里的协议
            if protocol not in self.ALLOW_PROTOCOLS:
                continue

            key = f"{ip}:{port}:{protocol}"
            if key in seen:
                continue
            seen.add(key)

            result.append({
                'ip': ip,
                'port': port,
                'protocol': protocol,
                'level': level,
                'city': city,
                'survival': survival,
            })
        return result

    def fetch(self):
        """采集免费代理列表（只保留 https/socks4/socks5），返回 list[dict]"""
        logging.info(f'正在采集: {SOURCE_URL}')
        try:
            resp = requests.get(SOURCE_URL, proxies=self.proxies,
                                headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logging.error(f'采集失败: {e}')
            return []

        resp.encoding = resp.apparent_encoding or resp.encoding
        items = self._parse_page(resp.text)
        logging.info(f'采集完成，符合协议（{"/".join(sorted(self.ALLOW_PROTOCOLS))}）'
                     f'共 {len(items)} 条')
        return items

    # ---------- 2. 测试 ----------
    @staticmethod
    def _proxy_url(item):
        p = item['protocol']
        if p == 'socks5':
            return f"socks5h://{item['ip']}:{item['port']}"
        if p == 'socks4':
            return f"socks4://{item['ip']}:{item['port']}"
        # https 代理走 CONNECT 隧道
        return f"http://{item['ip']}:{item['port']}"

    def test_one(self, item):
        proxy_url = self._proxy_url(item)
        proxies = {'http': proxy_url, 'https': proxy_url}
        try:
            r = requests.get(TEST_URL, proxies=proxies,
                             headers=HEADERS, timeout=TEST_TIMEOUT)
            if r.status_code == 200:
                return item
        except requests.RequestException:
            pass
        return None

    def test_all(self, items):
        """并发测试，返回可用代理列表"""
        alive = []
        logging.info(f'开始测试 {len(items)} 个代理（并发 {MAX_WORKERS}，目标 {TEST_URL}）...')
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(self.test_one, it): it for it in items}
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    alive.append(result)
                    logging.info(
                        f"可用: {result['ip']}:{result['port']} "
                        f"[{result['protocol']}] {result['city']}"
                    )
        logging.info(f'测试完成，可用 {len(alive)}/{len(items)}')
        return alive

    # ---------- 3. 保存 / 读取 ----------
    @staticmethod
    def save(items):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(TXT_FILE, 'w', encoding='utf-8') as f:
            for it in items:
                proto = it['protocol'].lower()
                if proto == 'socks4':
                    line = f"socks4://{it['ip']}:{it['port']}"
                elif proto == 'socks5':
                    line = f"socks5://{it['ip']}:{it['port']}"
                else:
                    line = f"{it['ip']}:{it['port']}"
                f.write(line + '\n')
        logging.info(f'已保存: {TXT_FILE}')

    @staticmethod
    def load():
        """供其它脚本调用：返回可用代理列表（从 TXT 读取）"""
        if not os.path.exists(TXT_FILE):
            return []
        with open(TXT_FILE, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]

    # ---------- 4. 调度 ----------
    def run_once(self):
        logging.info('=' * 60)
        logging.info(f'开始执行采集任务: {datetime.now():%Y-%m-%d %H:%M:%S}')
        items = self.fetch()
        if not items:
            logging.warning('未采集到任何代理，跳过本次测试')
            return []
        alive = self.test_all(items)
        self.save(alive)
        return alive

    def run_daily(self, hour=2, minute=0):
        """每天固定时间执行一次（阻塞循环）"""
        while True:
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            logging.info(f'下次执行时间: {target:%Y-%m-%d %H:%M:%S} '
                         f'（等待 {wait / 3600:.2f} 小时）')
            time.sleep(wait)
            try:
                self.run_once()
            except Exception as e:
                logging.exception(f'执行异常: {e}')


if __name__ == '__main__':
    # 只输出到控制台，不写本地日志文件
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        handlers=[logging.StreamHandler()],
    )
    pool = ProxyPool()
    pool.run_once()                 # 启动先跑一次
    # pool.run_daily(hour=2)        # 之后每天 02:00 自动执行