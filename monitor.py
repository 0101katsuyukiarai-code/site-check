"""出勤日の監視(新しい予約ボタンが出たら通知)。
環境変数: CAST_IDS, NOTIFY_FIRST, NTFY_TOPIC, GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO, DEBUG
"""
import datetime as dt
import hashlib, json, os, pathlib, re, smtplib
from email.message import EmailMessage
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

CAST_IDS = [c.strip() for c in os.getenv("CAST_IDS", "55665").split(",") if c.strip()]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
STATE = pathlib.Path("state_dates.json")
WD = "月火水木金土日"
DEBUG = os.getenv("DEBUG") == "1"


def h(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()[:12]


def get(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "utf-8"
    return r.text


def parse(html: str, cid: str):
    """(名前, {date: (時間帯, 予約URL)}) を返す。出勤情報が見当たらなければ None"""
    if "出勤情報" not in html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    t = soup.title.get_text(strip=True) if soup.title else cid
    name = re.split(r"[(（|｜]", t)[0] or cid
    days = {}
    for a in soup.find_all("a", href=True):
        href = urljoin("https://namexspa.com/", a["href"])
        if "r.caskan.jp" in href and f"cast_id={cid}" in href:
            date = parse_qs(urlparse(href).query).get("date", [""])[0]
            if not date:
                continue
            row = a.find_parent("tr")
            m = re.search(r"\d{1,2}:\d{2}\s*[〜~～-]\s*\d{1,2}:\d{2}", row.get_text(" ", strip=True)) if row else None
            days[date] = (m.group(0) if m else "", href)
    return name, days


def label(date: str) -> str:
    d = dt.date.fromisoformat(date)
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def notify(lines: list[str]) -> None:
    body = "\n".join(lines)
    title = "予約ボタンが出ました"
    if topic := os.getenv("NTFY_TOPIC"):
        requests.post("https://ntfy.sh/", timeout=30, json={
            "topic": topic, "title": title, "message": body, "priority": 5, "tags": ["tada"]})
    if (user := os.getenv("GMAIL_USER")) and (pw := os.getenv("GMAIL_APP_PASSWORD")):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, user, os.getenv("MAIL_TO", user)
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.send_message(msg)
    print(f"通知しました({len(lines)}件)")


def main() -> None:
    first_run = not STATE.exists()
    old = json.loads(STATE.read_text()) if not first_run else {}
    current, new = {}, []
    for cid in CAST_IDS:
        ck = h(cid)
        res = parse(get(f"https://namexspa.com/therapist/{cid}"), cid)
        if res is None:  # 取得失敗など。前回の記録を維持して誤通知を防ぐ
            print("警告: 出勤情報が読めませんでした。今回はスキップ")
            current[ck] = old.get(ck, [])
            continue
        name, days = res
        if DEBUG:
            print(f"[debug] {name}({cid}) {[label(d) for d in sorted(days)]}")
        keys = []
        for date, (hours, url) in sorted(days.items()):
            k = h(f"{cid}|{date}")
            keys.append(k)
            if k not in old.get(ck, []):
                new.append(f"{name} {label(date)} {hours}\n{url}")
        current[ck] = keys
        print(f"確認OK(出勤日 {len(days)}件)")
    if new and (not first_run or os.getenv("NOTIFY_FIRST") == "1"):
        notify(new)
    elif first_run:
        print("初回なので記録だけしました(通知なし)")
    else:
        print("新しい日なし")
    STATE.write_text(json.dumps(current, indent=1))


if __name__ == "__main__":
    main()
