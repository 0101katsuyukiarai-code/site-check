"""時間表の空き(空欄マス)を監視して、新しく出たら通知する。
環境変数: CAST_IDS, NTFY_TOPIC, GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO, DEBUG
"""
import hashlib, json, os, pathlib, re, smtplib
from email.message import EmailMessage
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

CAST_IDS = [c.strip() for c in os.getenv("CAST_IDS", "55665").split(",") if c.strip()]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
STATE = pathlib.Path("state.json")
DEBUG = os.getenv("DEBUG") == "1"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def h(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()[:12]


def dbg(*a):
    if DEBUG:
        print("[debug]", *a)


def get(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "utf-8"
    return r.text


def cast_name(html: str, cast_id: str) -> str:
    title = BeautifulSoup(html, "html.parser").title
    return re.split(r"[(（|｜]", title.get_text(strip=True))[0] if title else cast_id


def reserve_links(html: str, cast_id: str) -> dict[str, str]:
    """出勤日ごとの予約リンク {date: url}"""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        href = urljoin("https://namexspa.com/", a["href"])
        if "r.caskan.jp" in href and f"cast_id={cast_id}" in href:
            out[parse_qs(urlparse(href).query).get("date", [href])[0]] = href
    return out


def time_tables(soup):
    """見出し行が全部 'H:MM' のテーブルを返す"""
    res = []
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        heads = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if heads and all(TIME_RE.match(h) for h in heads):
            res.append((heads, [c.get_text(strip=True) for c in rows[1].find_all(["th", "td"])]))
    return res


def cast_order(soup):
    """セラピスト一覧の並び順(予約ボタンあり / TEL確認以外)のID"""
    seen, with_btn, non_tel = set(), [], []
    for a in soup.find_all("a", href=re.compile(r"/namex/cast/\d+")):
        cid = re.search(r"/namex/cast/(\d+)", a["href"]).group(1)
        if cid in seen:
            continue
        seen.add(cid)
        card = a.find_parent("li") or a.parent
        text = card.get_text(" ", strip=True) if card else ""
        if "TEL確認" in text:
            continue
        non_tel.append(cid)
        if "予約満了" not in text:
            with_btn.append(cid)
    return with_btn, non_tel


def to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def ranges(times: list[str]) -> list[str]:
    """連続する5分刻みの時刻を '21:00〜21:55' のようにまとめる"""
    out, start, prev = [], None, None
    for t in times:
        if start is None:
            start = prev = t
        elif (to_min(t) - to_min(prev)) % 1440 == 5:
            prev = t
        else:
            out.append(f"{start}〜{prev}")
            start = prev = t
    if start:
        out.append(f"{start}〜{prev}")
    return out


def find_slots(html: str, cast_id: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    tables = time_tables(soup)
    with_btn, non_tel = cast_order(soup)
    dbg(f"時間表={len(tables)}個 / 予約ボタン付き={len(with_btn)}人 / TEL確認以外={len(non_tel)}人")
    idx = 0 if len(tables) == 1 else None  # 表が1つだけなら、その人の表
    for order in ((with_btn, non_tel) if idx is None else ()):
        if cast_id in order and len(order) == len(tables):
            idx = order.index(cast_id)
            break
    if idx is None:
        print("警告: 時間表の対応が取れませんでした。スキップします。")
        return []
    heads, cells = tables[idx]
    dbg(f"cast {cast_id} -> 時間表 #{idx} 先頭={heads[0]} 末尾={heads[-1]} 空き={sum(1 for c in cells if c == '')} x={sum(1 for c in cells if c.lower() == 'x')}")
    free = [h for h, c in zip(heads, cells) if c == ""]
    return ranges(free)


def notify(lines: list[str]) -> None:
    body = "\n".join(lines)
    title = "予約に空きが出ました"
    if topic := os.getenv("NTFY_TOPIC"):
        requests.post("https://ntfy.sh/", timeout=30, json={
            "topic": topic, "title": title, "message": body,
            "priority": 5, "tags": ["tada"]})
    if (user := os.getenv("GMAIL_USER")) and (pw := os.getenv("GMAIL_APP_PASSWORD")):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, user, os.getenv("MAIL_TO", user)
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.send_message(msg)
    print(f"通知しました({len(lines)}件)")


def main() -> None:
    current: dict[str, str] = {}
    for cid in CAST_IDS:
        page = get(f"https://namexspa.com/therapist/{cid}")
        name = cast_name(page, cid)
        links = reserve_links(page, cid)
        dbg(f"{name}({cid}) 出勤日: {sorted(links)}")
        for date, url in sorted(links.items()):
            html = get(url)
            if DEBUG:
                pathlib.Path(f"debug_{cid}_{date}.html").write_text(html, encoding="utf-8")
            for rng in find_slots(html, cid):
                current[h(f"{cid}|{date}|{rng}")] = f"{name} {date} {rng}\n{url}"
    old = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    new = [v for k, v in current.items() if k not in old]
    if new:
        notify(new)
    else:
        print(f"新しい空きなし(現在の空き枠: {len(current)})")
    STATE.write_text(json.dumps(sorted(current), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
