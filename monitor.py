#!/usr/bin/env python3
"""
机票监控脚本（Python + Playwright）

航线：纽约 JFK → 北京（首都 PEK / 大兴 PKX 任意）
去程：2026-12-16
返程：2026-12-22 或 2026-12-23，北京任意机场 → JFK
类型：一定是【来回票】（只认 Google Flights 标注 "round trip total" 的价格）
目标：来回总价在 MIN_PRICE ~ MAX_PRICE 之间（默认 7000 ~ 8000）

两种模式（环境变量 MODE）：
  flights（默认）：直接在 Google Flights 搜索来回票并读取价格。
  buttons       ：打开 MONITOR_URL，判断“你的机票到了！”/“请继续等待”
                  两个按钮哪个真正可见（两个按钮都一直在 HTML 里）。

退出码：
  0 = 正常检查完成（有票已报警，或没票继续等待）
  1 = 页面上什么都没识别到（已保存截图到 screenshots/），或报警发送失败

用法：
  python monitor.py               # 正常检查一次
  python monitor.py --test-email  # 只发一封测试邮件
  python monitor.py --test-bark   # 只发一次 Bark 测试警报

所有密码 / key 都从环境变量读取（GitHub Actions 里由 Secrets 注入），代码里不写死：
  BARK_KEY            Bark App 里的设备 key（必填，用于手机警报）
  BARK_SERVER         可选，默认 https://api.day.app
  GMAIL_USER          发件 Gmail 地址
  GMAIL_APP_PASSWORD  Gmail 16 位应用专用密码
  MAIL_TO             收件邮箱（不填就发给 GMAIL_USER 自己）
"""

import argparse
import datetime as dt
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============ 可修改的设置（也可以用同名环境变量覆盖） ============
ORIGIN = os.getenv("ORIGIN", "JFK")
DESTINATION = os.getenv("DESTINATION", "Beijing")  # Beijing = PEK + PKX 两个机场
DEPART_DATE = os.getenv("DEPART_DATE", "2026-12-16")
RETURN_DATES = [d.strip() for d in os.getenv("RETURN_DATES", "2026-12-22,2026-12-23").split(",") if d.strip()]
MIN_PRICE = float(os.getenv("MIN_PRICE", "7000"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "8000"))
CURRENCY = os.getenv("CURRENCY", "CNY")  # 价格单位：CNY=人民币，USD=美元

IN_STOCK_TEXT = os.getenv("IN_STOCK_TEXT", "你的机票到了！")
OUT_OF_STOCK_TEXT = os.getenv("OUT_OF_STOCK_TEXT", "请继续等待")
MODE = os.getenv("MODE", "flights").strip().lower()
MONITOR_URL = os.getenv("MONITOR_URL", "").strip()

SCREENSHOT_DIR = Path("screenshots")
PAGE_TIMEOUT_MS = 60_000
# ==============================================================


def log(msg: str) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ---------------------------------------------------------------
# 报警：Bark（iPhone 关键警报）+ Gmail 邮件
# ---------------------------------------------------------------
def send_bark(title: str, body: str, url: str = "") -> None:
    key = os.getenv("BARK_KEY", "").strip()
    if not key:
        raise RuntimeError("没有设置 BARK_KEY（GitHub Secrets 里加上）")
    server = os.getenv("BARK_SERVER", "https://api.day.app").strip().rstrip("/")
    # 兼容用户把整条链接 https://api.day.app/xxxx/ 粘进来的情况
    m = re.match(r"^(https?://[^/]+)/([^/?#]+)", key)
    if m:
        server, key = m.group(1), m.group(2)

    params = {
        "level": "critical",  # 关键警报：静音、专注、睡眠模式下也会响
        "call": "1",          # 持续响铃（约 30 秒），像来电一样
        "volume": "10",       # 关键警报音量 0-10
        "group": "flight",
    }
    if url:
        params["url"] = url
    full = (
        f"{server}/{urllib.parse.quote(key)}/"
        f"{urllib.parse.quote(title, safe='')}/{urllib.parse.quote(body, safe='')}"
        f"?{urllib.parse.urlencode(params)}"
    )
    req = urllib.request.Request(full, headers={"User-Agent": "flight-monitor"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8", "ignore")
    if '"code":200' not in text.replace(" ", ""):
        raise RuntimeError(f"Bark 返回异常：{text}")
    log("Bark 关键警报已发送")


def send_email(subject: str, body: str) -> None:
    user = os.getenv("GMAIL_USER", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.getenv("MAIL_TO", "").strip() or user
    if not user or not password:
        raise RuntimeError("没有设置 GMAIL_USER 或 GMAIL_APP_PASSWORD（GitHub Secrets 里加上）")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, password)
        s.sendmail(user, [a.strip() for a in to.split(",") if a.strip()], msg.as_string())
    log(f"邮件已发送到 {to}")


def alert(title: str, body: str, url: str = "") -> bool:
    """两路报警都尝试发送；任何一路失败都返回 False。"""
    ok = True
    for name, fn in (("Bark", lambda: send_bark(title, body, url)),
                     ("邮件", lambda: send_email(title, body + (f"\n\n链接：{url}" if url else "")))):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok = False
            log(f"{name} 发送失败：{e}")
    return ok


# ---------------------------------------------------------------
# “真正可见”判断：不能只搜文字，要看元素是否真的显示在页面上
# ---------------------------------------------------------------
_VISIBLE_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (parseFloat(s.opacity) === 0) return false;
    if (n.hidden || n.getAttribute('aria-hidden') === 'true') return false;
  }
  return true;
}
"""


def is_text_really_visible(page, text: str) -> bool:
    """页面上只要有一个包含 text 的元素真正可见就返回 True。"""
    loc = page.get_by_text(text, exact=False)
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if el.is_visible() and el.evaluate(_VISIBLE_JS):
                return True
        except Exception:  # 元素在检查过程中消失等情况
            continue
    return False


def save_screenshot(page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log(f"已保存截图：{path}")
    except Exception as e:  # noqa: BLE001
        log(f"截图失败：{e}")


def new_page(browser):
    context = browser.new_context(
        locale="en-US",
        timezone_id="America/New_York",
        viewport={"width": 1366, "height": 2000},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    return page


def dismiss_consent(page) -> None:
    """Google 有时会先弹 Cookie 同意页，自动点掉。"""
    for label in ("Reject all", "Accept all", "全部拒绝", "全部接受"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_load_state("domcontentloaded")
                return
        except Exception:
            pass


# ---------------------------------------------------------------
# 模式一：按钮页面（两个按钮一直同时存在于 HTML）
# ---------------------------------------------------------------
def run_buttons_mode(browser) -> int:
    if not MONITOR_URL:
        log("MODE=buttons 但没有设置 MONITOR_URL")
        return 1
    page = new_page(browser)
    page.goto(MONITOR_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass

    in_stock = is_text_really_visible(page, IN_STOCK_TEXT)
    out_stock = is_text_really_visible(page, OUT_OF_STOCK_TEXT)
    log(f"按钮可见性：【{IN_STOCK_TEXT}】={in_stock}  【{OUT_OF_STOCK_TEXT}】={out_stock}")

    if in_stock:  # 有货按钮可见就算有货
        body = f"监控页面显示【{IN_STOCK_TEXT}】\n{MONITOR_URL}"
        return 0 if alert(IN_STOCK_TEXT, body, MONITOR_URL) else 1
    if out_stock:
        log(f"【{OUT_OF_STOCK_TEXT}】")
        return 0
    log("两个按钮都看不到，页面可能变了或没加载出来")
    save_screenshot(page, "no_button_visible")
    return 1


# ---------------------------------------------------------------
# 模式二（默认）：Google Flights 来回票价格
# ---------------------------------------------------------------
def flights_url(ret: str) -> str:
    q = f"Flights to {DESTINATION} from {ORIGIN} on {DEPART_DATE} through {ret}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(
        {"q": q, "curr": CURRENCY, "hl": "en", "gl": "US"}
    )


# 例：aria-label="From 7512 Chinese yuan round trip total. 1 stop flight with ..."
_PRICE_RE = re.compile(r"From\s+([\d,]+(?:\.\d+)?)\s+[A-Za-z .]+?\s+round trip total", re.I)


def read_round_trip_prices(page) -> list[tuple[float, str]]:
    """返回 [(价格, 航班描述)]，只收真正可见、且明确是来回总价的结果。"""
    results = []
    cards = page.locator("[aria-label*='round trip total' i]")
    for i in range(cards.count()):
        el = cards.nth(i)
        try:
            if not (el.is_visible() and el.evaluate(_VISIBLE_JS)):
                continue
            label = el.get_attribute("aria-label") or ""
        except Exception:
            continue
        m = _PRICE_RE.search(label)
        if m:
            price = float(m.group(1).replace(",", ""))
            results.append((price, re.sub(r"\s+", " ", label)[:300]))
    # 去重（同一航班可能有多个元素带同样的标签）
    return sorted(set(results))


def run_flights_mode(browser) -> int:
    today = dt.date.today()
    if today > dt.date.fromisoformat(DEPART_DATE):
        log("出发日期已过，监控结束")
        return 0

    any_result = False
    hits = []
    for ret in RETURN_DATES:
        url = flights_url(ret)
        log(f"检查 {ORIGIN}→{DESTINATION} {DEPART_DATE} 去 / {ret} 回")
        page = new_page(browser)
        try:
            page.goto(url, wait_until="domcontentloaded")
            dismiss_consent(page)
            try:
                page.wait_for_selector("[aria-label*='round trip total' i]", state="visible", timeout=45_000)
                page.wait_for_timeout(3000)  # 等价格全部渲染
            except PWTimeout:
                pass

            prices = read_round_trip_prices(page)
            if not prices:
                log(f"  {ret} 返程：没识别到任何来回票价格")
                save_screenshot(page, f"no_result_{ret}")
                continue

            any_result = True
            cheapest = prices[0][0]
            log(f"  {ret} 返程：共 {len(prices)} 个来回票价格，最低 {cheapest:,.0f} {CURRENCY}")
            in_range = [(p, d) for p, d in prices if MIN_PRICE <= p <= MAX_PRICE]
            for p, d in in_range:
                hits.append((ret, p, d, url))
        finally:
            page.context.close()

    if hits:
        hits.sort(key=lambda h: h[1])
        lines = [
            f"{ORIGIN} → 北京（PEK/PKX）{DEPART_DATE} 出发，来回票",
            f"目标价格 {MIN_PRICE:,.0f} - {MAX_PRICE:,.0f} {CURRENCY}，找到 {len(hits)} 个：",
            "",
        ]
        for ret, p, d, url in hits[:10]:
            lines += [f"【{ret} 返程】{p:,.0f} {CURRENCY}", f"  {d}", f"  {url}", ""]
        body = "\n".join(lines)
        log(f"【{IN_STOCK_TEXT}】\n{body}")
        best = hits[0]
        short = f"{best[0]} 返程 {best[1]:,.0f} {CURRENCY}，共 {len(hits)} 个符合"
        ok_bark = ok_mail = True
        try:
            send_bark(IN_STOCK_TEXT, short, best[3])
        except Exception as e:  # noqa: BLE001
            ok_bark = False
            log(f"Bark 发送失败：{e}")
        try:
            send_email(IN_STOCK_TEXT, body)
        except Exception as e:  # noqa: BLE001
            ok_mail = False
            log(f"邮件发送失败：{e}")
        return 0 if (ok_bark and ok_mail) else 1

    if not any_result:
        log("两个返程日期都没识别到任何价格（相当于两个按钮都看不到），截图已保存")
        return 1

    log(f"【{OUT_OF_STOCK_TEXT}】没有 {MIN_PRICE:,.0f}-{MAX_PRICE:,.0f} {CURRENCY} 的来回票")
    return 0


# ---------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="机票监控")
    ap.add_argument("--test-email", action="store_true", help="只发送一封测试邮件")
    ap.add_argument("--test-bark", action="store_true", help="只发送一次 Bark 测试警报")
    args = ap.parse_args()

    if args.test_email:
        try:
            send_email("【测试】机票监控邮件",
                       "这是一封测试邮件。能收到说明 Gmail 应用专用密码设置正确。\n"
                       f"有票时邮件标题会是：{IN_STOCK_TEXT}")
            return 0
        except Exception as e:  # noqa: BLE001
            log(f"测试邮件发送失败：{e}")
            return 1

    if args.test_bark:
        try:
            send_bark("【测试】机票监控", "能听到响铃说明 Bark 关键警报设置正确")
            return 0
        except Exception as e:  # noqa: BLE001
            log(f"Bark 测试失败：{e}")
            return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            if MODE == "buttons":
                return run_buttons_mode(browser)
            return run_flights_mode(browser)
        except Exception as e:  # noqa: BLE001
            log(f"运行出错：{e}")
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
