# -*- coding: utf-8 -*-
"""Build a local read-only dashboard snapshot.
No external upload. Filters sensitive text before writing data/snapshot.json.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

FIN = Path(r"C:/Users/smile/Documents/Obsidian/포이에마/1_지금/재무통제실")
CHILL = Path(r"C:/Users/smile/Documents/Obsidian/포이에마/1_지금/칠링스페이스_OS")
CHILL_BLOG = Path(r"C:/Users/smile/Documents/Obsidian/포이에마/3_발행/01_칠링스페이스")
INA = Path(r"C:/Users/smile/Documents/이나월드")
GAPI_DIR = Path(r"C:/Users/smile/AppData/Local/hermes/skills/productivity/google-workspace/scripts")
INCOME_SOURCES_FILE = DATA / "income_sources.json"
AIRBNB_CALENDAR_FILE = DATA / "airbnb_calendar.json"

# 대시보드 표시용 수입 가정. 원장 Source of Truth는 수정하지 않는다.
# 사용자가 확인한 쿠팡 평균과 Aside 읽기 전용 확인값을 반영한다.
DEFAULT_INCOME_SOURCES = {
    "period": "2026-10-25~2026-11-24 (월 수입·지출 기준 기간)",
    "current_income": {"쿠팡 10/25 입금분": 9_500_000, "에어비앤비 11월 수령완료+예정": 1_106_190, "칠링스페이스 예약확정": 2_160_000},
    "next_cycle_income": {"쿠팡 월평균": 9_500_000, "칠링스페이스 다음기간 예약확정": 1_700_000, "에어비앤비 12/25 순수령예정": 272_935},
    "chill_reservations": [],
    "airbnb_earnings": {},
    "smartplace_earnings": {},
}

def load_income_sources():
    if INCOME_SOURCES_FILE.exists():
        try:
            data = json.loads(INCOME_SOURCES_FILE.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_INCOME_SOURCES)
            merged.update(data)
            return merged
        except Exception:
            return DEFAULT_INCOME_SOURCES
    return DEFAULT_INCOME_SOURCES

INCOME_SOURCES = load_income_sources()
DASHBOARD_INCOME_PERIOD = INCOME_SOURCES.get("period", DEFAULT_INCOME_SOURCES["period"])
DASHBOARD_INCOME_ASSUMPTIONS = INCOME_SOURCES.get("current_income", DEFAULT_INCOME_SOURCES["current_income"])
DASHBOARD_NEXT_CYCLE_ASSUMPTIONS = INCOME_SOURCES.get("next_cycle_income", DEFAULT_INCOME_SOURCES["next_cycle_income"])
DASHBOARD_CHILL_EXPECTED_RESERVATIONS = INCOME_SOURCES.get("chill_reservations", [])
DASHBOARD_AIRBNB_EARNINGS = INCOME_SOURCES.get("airbnb_earnings", {})
DASHBOARD_SMARTPLACE_EARNINGS = INCOME_SOURCES.get("smartplace_earnings", {})

SENSITIVE_PATTERNS = [
    r"어린이집", r"병원", r"진료", r"처방", r"열\b", r"감기", r"구토", r"설사", r"알러지", r"예방접종",
    r"하원", r"등원", r"상담", r"주소", r"아파트\s*\d+동", r"\d{2,4}-\d{3,4}-\d{4}",
]

def now_iso():
    return datetime.now(KST).isoformat(timespec="seconds")

def read_text(path: Path, limit_chars: int | None = None) -> str:
    try:
        txt = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        txt = path.read_text(encoding="utf-8-sig")
    return txt if limit_chars is None else txt[:limit_chars]

def file_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, KST).isoformat(timespec="seconds")
    except FileNotFoundError:
        return None

def source_status(name, path: Path | None, status="ok", error=None, extra=None):
    d = {"name": name, "status": status, "last_attempt_at": now_iso(), "last_success_at": now_iso() if status == "ok" else None, "error": error}
    if path:
        d.update({"path": str(path), "source_modified_at": file_mtime_iso(path), "exists": path.exists()})
    if extra:
        d.update(extra)
    return d

def money_to_int(s: str) -> int | None:
    m = re.search(r"([\d,]+)원", s or "")
    return int(m.group(1).replace(",", "")) if m else None

def parse_finance_dashboard():
    path = FIN / "00_재무통제실.md"
    txt = read_text(path)
    def row(label):
        m = re.search(rf"\|\s*{re.escape(label)}[^|]*\|\s*([^|]+)\|", txt)
        return m.group(1).strip() if m else None
    overview = {
        "기준일": re.search(r"기준일:\s*([^\n]+)", txt).group(1).strip() if re.search(r"기준일:\s*([^\n]+)", txt) else None,
        "이번 달 수입": row("이번 달 수입"),
        "이번 달 지출": row("이번 달 지출"),
        "이번 달 잉여금": row("이번 달 잉여금"),
        "현재 가용자금": row("현재 가용자금"),
        "총 대출잔액": row("총 대출잔액"),
        "총 할부잔액": row("총 할부잔액"),
        "다음 달 예정지출": row("다음 달 예정지출"),
    }
    warn = []
    check = re.search(r"## 7\. 확인 필요\n([\s\S]+?)(?:\n---|\Z)", txt)
    if check:
        warn = [re.sub(r"^-\s*", "", line).strip() for line in check.group(1).splitlines() if line.strip().startswith("-")][:8]
    loans = []
    loan_sec = re.search(r"## 4\. 대출 현황\n([\s\S]+?)\n## 5", txt)
    if loan_sec:
        for line in loan_sec.group(1).splitlines():
            if line.startswith("|") and "|" in line and not any(x in line for x in ["---", "대출명", "합계"]):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 8:
                    loans.append({"name": cells[0], "institution": cells[1], "balance": cells[2], "payment": cells[3], "cycle": cells[4], "due_day": cells[5], "maturity": cells[7]})
    installments = []
    inst_sec = re.search(r"## 5\. 할부 현황\n([\s\S]+?)\n## 6", txt)
    if inst_sec:
        for line in inst_sec.group(1).splitlines():
            if line.startswith("|") and "|" in line and not any(x in line for x in ["---", "항목", "합계"]):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 8:
                    installments.append({"name": cells[0], "monthly": cells[1], "remain_count": cells[3], "remain_amount": cells[4], "due_day": cells[5], "this_month": cells[7]})
    upcoming = []
    due_sec = re.search(r"## 6\. 예정 지출[^\n]*\n([\s\S]+?)\n## 7", txt)
    if due_sec:
        for line in due_sec.group(1).splitlines():
            if line.startswith("|") and "|" in line and not any(x in line for x in ["---", "납부일", "합계"]):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 4:
                    upcoming.append({"date": cells[0], "name": cells[1], "kind": cells[2], "amount": cells[3]})
    return overview, warn, loans[:10], installments[:12], upcoming, path

def dashboard_finance_warnings(warnings):
    """Keep only user-actionable finance warnings on the dashboard.
    Hide noisy low-priority ledger hygiene such as unknown original installment totals.
    """
    out = []
    for w in warnings or []:
        if "최초금액·총회차 미확인" in w:
            continue
        if "마지막 회차" in w:
            # Not actionable until the due date. Keep it out of the daily dashboard.
            continue
        if "지출 기록 필요" in w:
            out.append("카드 할부 실제 출금 확인 필요 — 빠졌으면 지출 원장에 카드할부 1줄 기록")
            continue
        out.append(re.sub(r"\*+", "", w))
    return out[:5]

def parse_installment_ledger():
    """Read 할부.md directly so 회차 표기가 dashboard 축약표보다 정확하다."""
    path = FIN / "할부.md"
    txt = read_text(path)
    sec = re.search(r"## 할부 목록\n([\s\S]+?)\n## 할부 납부 내역", txt)
    rows = []
    if not sec:
        return rows
    for line in sec.group(1).splitlines():
        if not line.startswith("|") or any(x in line for x in ["---", "항목 |", "→"]):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 13:
            continue
        name, orig, monthly, fee, total_n, done_n, left_n, left_amt, fixed_bal, due_day, pay, status, memo = cells[:13]
        if status == "완료":
            continue
        done_i = int(done_n) if str(done_n).strip().isdigit() else None
        left_i = int(left_n) if str(left_n).strip().isdigit() else None
        total_i = int(total_n) if str(total_n).strip().isdigit() else None
        display_total = total_i if total_i is not None else ((done_i or 0) + left_i if left_i is not None else None)
        rows.append({
            "name": name,
            "monthly": monthly + (f" + 수수료 {fee}" if fee else ""),
            "progress": f"{done_i if done_i is not None else '?'} / {display_total if display_total is not None else '?'}회 납부",
            "left": f"남은 {left_n or '?'}회 · {left_amt or '금액 미확인'}",
            "left_count": left_i if left_i is not None else 999,
            "due_day": due_day,
            "status": status,
            "memo_short": re.sub(r"\*+", "", memo).split("·")[0].strip(),
        })
    # 남은 할부가 적은 순서로 보여준다. 같으면 납부일 빠른 순서.
    def key(r):
        day = int(r["due_day"]) if str(r["due_day"]).isdigit() else 99
        return (r.get("left_count", 999), day, r["name"])
    return sorted(rows, key=key)

def build_dashboard_projection(forecast):
    """Dashboard-only projection using known income assumptions.
    Does not edit finance ledgers. 쿠팡 평균 + Aside로 확인한 칠링/Airbnb 예정수입을 첫 사이클에 반영.
    """
    if forecast.get("status") != "ok":
        return forecast
    recurring_coupang = DASHBOARD_NEXT_CYCLE_ASSUMPTIONS["쿠팡 월평균"]
    first_cycle_extra = DASHBOARD_NEXT_CYCLE_ASSUMPTIONS["칠링스페이스 다음기간 예약확정"] + DASHBOARD_NEXT_CYCLE_ASSUMPTIONS["에어비앤비 12/25 순수령예정"]
    out = dict(forecast)
    rows = []
    cumulative_extra = 0
    for i, r in enumerate(forecast.get("rows", [])):
        rr = dict(r)
        cycle_extra = recurring_coupang + (first_cycle_extra if i == 0 else 0)
        cumulative_extra += cycle_extra
        rr["income_confirmed"] = r.get("income_basic")
        rr["income_assumed"] = cycle_extra
        rr["income_display"] = (r.get("income_basic") or 0) + cycle_extra
        rr["end_balance_display"] = (r.get("end_balance_basic") or 0) + cumulative_extra
        rr["income_note"] = "6개월 흐름은 다음 25~24 사이클부터 쿠팡 평균을 반복 반영하고, 첫 사이클에 확인된 칠링·Airbnb 예정수입을 추가 반영."
        rows.append(rr)
    out["rows"] = rows
    out["dashboard_assumptions"] = {
        "기간": DASHBOARD_INCOME_PERIOD,
        **DASHBOARD_NEXT_CYCLE_ASSUMPTIONS,
        "주의": "대시보드 표시용 가정이며 원장 Source of Truth는 수정하지 않음",
    }
    return out

def format_event_time(e):
    def parse(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace('Z', '+00:00')).astimezone(KST)
        except Exception:
            try:
                return datetime.fromisoformat(str(v)).replace(tzinfo=KST)
            except Exception:
                return None
    if e.get("all_day"):
        return str(e.get("start"))[:10]
    st, en = parse(e.get("start")), parse(e.get("end"))
    if st and en:
        return f"{st.strftime('%m/%d')} {st.strftime('%H:%M')}~{en.strftime('%H:%M')}"
    return f"{e.get('start')}~{e.get('end')}"

def current_income_cycle(today=None):
    today = today or datetime.now(KST).date()
    if today.day >= 25:
        start = today.replace(day=25)
        y, m = (today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1)
        end = datetime(y, m, 24, tzinfo=KST).date()
    else:
        y, m = (today.year - (today.month == 1), 12 if today.month == 1 else today.month - 1)
        start = datetime(y, m, 25, tzinfo=KST).date()
        end = today.replace(day=24)
    return start, end

def amount_int(s):
    return int(re.sub(r"[^0-9]", "", str(s) or "") or 0)

def run_finance_forecast():
    try:
        p = subprocess.run([sys.executable, "_scripts/재무계산.py", "forecast", "--months", "6", "--json"], cwd=str(FIN), text=True, capture_output=True, timeout=60)
        if p.returncode != 0:
            return {"status": "failed", "error": p.stderr[-500:] or p.stdout[-500:]}
        data = json.loads(p.stdout)
        rows = []
        for c in data.get("cycles", [])[:6]:
            rows.append({
                "cycle": f"{c.get('cycle_start')}~{c.get('cycle_end')}",
                "income_basic": c.get("income", {}).get("기본"),
                "income_detail": c.get("income_detail", {}),
                "actual_income": c.get("actual_income", {}),
                "scheduled_income": c.get("scheduled_income", {}),
                "expenses": c.get("expenses"),
                "end_balance_basic": c.get("end_balance", {}).get("기본"),
                "expenses_by_kind": c.get("expenses_by_kind", {}),
                "dues": c.get("dues", []),
            })
        return {"status": "ok", "asof": data.get("asof"), "start_cycle": data.get("start_cycle"), "rows": rows}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

def run_cycle_outgoing(start, end, from_date=None):
    """Return outgoing dues in the active 25~24 cycle, filtered to unpaid/future dates."""
    from_date = from_date or start
    try:
        p = subprocess.run(
            [sys.executable, "_scripts/재무계산.py", "forecast", "--asof", start.isoformat(), "--months", "2", "--json"],
            cwd=str(FIN), text=True, capture_output=True, timeout=60
        )
        if p.returncode != 0:
            return {"status": "failed", "error": p.stderr[-500:] or p.stdout[-500:], "items": [], "total": 0, "by_kind": {}}
        data = json.loads(p.stdout)
        items = []
        for c in data.get("cycles", []):
            for d in c.get("dues", []):
                try:
                    dt = datetime.fromisoformat(d.get("date", "")).date()
                except Exception:
                    continue
                if max(start, from_date) <= dt <= end:
                    item = dict(d)
                    item["amount"] = f"{int(item.get('amt') or 0):,}원"
                    items.append(item)
        by_kind = {}
        for item in items:
            by_kind[item.get("kind", "기타")] = by_kind.get(item.get("kind", "기타"), 0) + int(item.get("amt") or 0)
        return {"status": "ok", "items": items, "total": sum(int(x.get("amt") or 0) for x in items), "by_kind": by_kind, "from_date": from_date.isoformat()}
    except Exception as e:
        return {"status": "failed", "error": str(e), "items": [], "total": 0, "by_kind": {}}

def classify_calendar_event(summary: str) -> str:
    s = (summary or "").strip()
    if "칠링" in s or "칠스" in s:
        return "chill"
    if "에어비앤비" in s or "airbnb" in s.lower() or "숙소" in s:
        return "airbnb"
    if s == "휴무" or re.fullmatch(r"\d{2,4}", s):
        return "coupang"
    return "personal"


def safe_calendar_title(summary: str, category: str) -> str:
    """Preserve the Google Calendar title exactly for display."""
    return (summary or "(제목 없음)").strip()


def month_bounds(today):
    first = today.replace(day=1)
    if first.month == 12:
        next_month = first.replace(year=first.year + 1, month=1)
    else:
        next_month = first.replace(month=first.month + 1)
    # Calendar grid needs spillover days at both ends.
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    end = next_month + timedelta(days=(6 - ((next_month - timedelta(days=1)).weekday() + 1) % 7))
    return start, end


def google_calendar_events():
    try:
        sys.path.insert(0, str(GAPI_DIR))
        import google_api
        svc = google_api.build_service('calendar', 'v3')
        today = datetime.now(KST).date()
        start_day, end_day = month_bounds(today)
        time_min = datetime.combine(start_day, datetime.min.time(), KST).isoformat()
        time_max = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), KST).isoformat()
        events = svc.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max, singleEvents=True, orderBy='startTime').execute().get('items', [])
        safe = []
        for e in events:
            start = e.get('start', {})
            end = e.get('end', {})
            raw_summary = e.get('summary', '(제목 없음)')
            category = classify_calendar_event(raw_summary)
            safe.append({
                "summary": safe_calendar_title(raw_summary, category),
                "category": category,
                "start": start.get('dateTime') or start.get('date'),
                "end": end.get('dateTime') or end.get('date'),
                "all_day": 'date' in start,
            })
        return {"status": "ok", "events": safe, "month_start": start_day.isoformat(), "month_end": end_day.isoformat(), "source": source_status("Google Calendar", None)}
    except Exception as e:
        return {"status": "failed", "events": [], "month_start": None, "month_end": None, "source": source_status("Google Calendar", None, "failed", str(e))}

def latest_files(path: Path, pattern="*.md", n=8):
    files = sorted(path.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "path": str(p), "modified_at": file_mtime_iso(p)} for p in files[:n]]

def safe_snippet(text: str, max_lines=8):
    out=[]; filtered=0
    for line in text.splitlines():
        if any(re.search(p, line, re.I) for p in SENSITIVE_PATTERNS):
            filtered += 1
            continue
        if line.strip():
            out.append(line.strip())
        if len(out)>=max_lines:
            break
    return out, filtered

def inaworld_snapshot():
    sources=[]
    files = latest_files(INA, "*.md", 8)
    improvement = INA / "개선로그.md"
    weekly = INA / "레퍼런스/주간_레퍼런스_보고서/2026-11-09_주간_레퍼런스_보고서.md"
    editpkg = INA / "편집패키지/20261104_200226/편집패키지.md"
    filtered=0
    candidates=[]
    if weekly.exists():
        txt=read_text(weekly)
        m=re.search(r"## 6\. 이나월드 적용 가설 최대 2개\n([\s\S]+?)\n## 7", txt)
        if m:
            candidates, f = safe_snippet(m.group(1), 10); filtered+=f
        sources.append(source_status("이나월드 주간 레퍼런스", weekly))
    edit=[]
    if editpkg.exists():
        txt=read_text(editpkg)
        m=re.search(r"## 1순위 쇼츠 후보\n([\s\S]+?)\n---", txt)
        edit, f = safe_snippet(m.group(1) if m else txt, 8); filtered+=f
        sources.append(source_status("이나월드 편집패키지", editpkg))
    recent_rules=[]
    if improvement.exists():
        txt=read_text(improvement)
        recent_rules, f=safe_snippet("\n".join(txt.splitlines()[24:38]), 8); filtered+=f
        sources.append(source_status("이나월드 개선로그", improvement))
    return {"status":"ok", "sources":sources, "latest_files":files, "shooting_candidates":candidates, "editing_priorities":edit, "content_recommendations":recent_rules, "filtered_sensitive_items": filtered}

def is_chilling_reservation_event(title: str) -> bool:
    """Only bracketed Chilling Space calendar entries are reservations.
    Plain '칠스/칠링스페이스' tasks are internal work, not bookings.
    """
    return bool(re.search(r"\[\s*칠링스페이스\s*\]", title or ""))


def chill_snapshot(events):
    sources=[source_status("칠링스페이스 상태", CHILL/"01_상태.md"), source_status("칠링스페이스 검증기록", CHILL/"02_검증기록.md")]
    reservations=[]
    work_items=[]
    for e in events:
        title=e.get("summary", "")
        if is_chilling_reservation_event(title):
            people_m = re.search(r"(\d+)명", title)
            reservations.append({"reservation_id": None, "time_range": format_event_time(e), "people": (people_m.group(1)+"명") if people_m else None, "title_safe": title})
        elif "칠링" in title or "칠스" in title:
            work_items.append({"time_range": format_event_time(e), "title": title})
    blog_files = latest_files(CHILL_BLOG, "*.md", 5) if CHILL_BLOG.exists() else []
    state = read_text(CHILL/"01_상태.md", 2000) if (CHILL/"01_상태.md").exists() else ""
    current_rows = DASHBOARD_CHILL_EXPECTED_RESERVATIONS
    next_rows = INCOME_SOURCES.get("chill_reservations_next_cycle", [])
    current_total = sum(int(r.get("amount") or 0) for r in current_rows)
    next_total = sum(int(r.get("amount") or 0) for r in next_rows)
    filter_note = DASHBOARD_SMARTPLACE_EARNINGS.get("filter_note") or "칠링스페이스 재무 수익은 예약 확정/입금 시점 기준으로 반영합니다. 이용일 기준 조회는 운영·매니저비 확인용입니다."
    return {"status":"ok", "sources":sources, "reservations":reservations, "work_items":work_items, "expected_period": DASHBOARD_INCOME_PERIOD, "expected_income": {"total": DASHBOARD_INCOME_ASSUMPTIONS["칠링스페이스 예약확정"], "reservations": current_rows, "current_cycle_total": current_total, "next_cycle_total": next_total, "next_cycle_reservations": next_rows, "filter_note": filter_note, "smartplace_earnings": DASHBOARD_SMARTPLACE_EARNINGS}, "cleaning_status":"미확인", "marketing_warnings":["재무 수익 기준: 예약 확정/입금 시점", "이용일 기준은 운영·매니저비 확인용"], "blog_drafts":blog_files, "state_summary": safe_snippet(state, 6)[0]}

def airbnb_calendar_snapshot():
    if not AIRBNB_CALENDAR_FILE.exists():
        return {"status":"missing", "reservations": [], "source": source_status("Airbnb calendar", AIRBNB_CALENDAR_FILE, "failed", "airbnb_calendar.json 없음")}
    try:
        data = json.loads(AIRBNB_CALENDAR_FILE.read_text(encoding="utf-8"))
        return {"status":"ok", "reservations": data.get("reservations", []), "updated_at": data.get("updated_at"), "source": source_status("Airbnb calendar", AIRBNB_CALENDAR_FILE)}
    except Exception as e:
        return {"status":"failed", "reservations": [], "source": source_status("Airbnb calendar", AIRBNB_CALENDAR_FILE, "failed", str(e))}


def add_airbnb_events(events, airbnb):
    out = list(events)
    for r in airbnb.get("reservations", []):
        ci, co = r.get("check_in"), r.get("check_out")
        guests = r.get("guests")
        if not ci or not co:
            continue
        suffix = f" {guests}명" if guests else ""
        out.append({"summary": f"에어비앤비 체크인{suffix}", "category": "airbnb", "start": ci, "end": ci, "all_day": True})
        out.append({"summary": "에어비앤비 체크아웃", "category": "airbnb", "start": co, "end": co, "all_day": True})
    return sorted(out, key=lambda e: str(e.get("start", "")))


def ai_news_snapshot():
    """Fetch a curated AI trend digest for the dashboard.
    Public web data only. Avoid raw low-signal HN lists; show Korean context.
    """
    import time
    topics = [
        ("AI agent", "AI 에이전트"),
        ("OpenAI", "OpenAI"),
        ("Anthropic", "Anthropic/Claude"),
        ("Google Gemini", "Google Gemini"),
        ("LLM", "LLM"),
    ]
    cutoff = int(time.time()) - 60 * 60 * 24 * 45
    items = []
    errors = []
    reject_words = ["chess", "home automation", "blog framework", "lettercamp", "zumungo"]

    def summarize(title, topic, points, comments):
        t = title.lower()
        if "agent" in t or "codex" in t or "claude" in t:
            why = "자동화·에이전트 운영 방식에 참고할 만합니다."
        elif "openai" in t or "anthropic" in t or "gemini" in t:
            why = "주요 모델/플랫폼 변화라 도구 선택에 영향이 있을 수 있습니다."
        elif "security" in t or "safety" in t:
            why = "AI 시스템을 외부에 연결할 때 보안 기준으로 참고할 만합니다."
        elif "llm" in t or "model" in t:
            why = "모델 성능·비용·활용 방식 판단에 참고할 만합니다."
        else:
            why = "AI 업무 자동화와 간접 관련이 있어 후보로 남겼습니다."
        return f"{topic} 관련 공개 이슈입니다. {why} 반응: {points}점 · 댓글 {comments}개."

    for query, topic in topics:
        try:
            params = {
                "query": query,
                "tags": "story",
                "hitsPerPage": 8,
                "numericFilters": f"created_at_i>{cutoff},points>20",
            }
            url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"User-Agent": "PersonalDashboard/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            for hit in data.get("hits", []):
                title = (hit.get("title") or hit.get("story_title") or "").strip()
                if not title:
                    continue
                low = title.lower()
                if any(word in low for word in reject_words):
                    continue
                points = int(hit.get("points") or 0)
                comments = int(hit.get("num_comments") or 0)
                key = low
                if any(existing.get("key") == key for existing in items):
                    continue
                link = hit.get("url") or ("https://news.ycombinator.com/item?id=" + str(hit.get("objectID")))
                items.append({
                    "key": key,
                    "title": title,
                    "title_ko": f"[{topic}] {title}",
                    "summary": summarize(title, topic, points, comments),
                    "source": "Hacker News",
                    "url": link,
                    "published_at": hit.get("created_at"),
                    "points": points,
                    "comments": comments,
                    "topic": topic,
                    "importance": "높음" if points >= 100 or comments >= 50 else "보통",
                })
                if len(items) >= 6:
                    break
            if len(items) >= 6:
                break
        except Exception as e:
            errors.append(f"{query}: {e}")
    items.sort(key=lambda x: (x.get("importance") == "높음", x.get("points", 0) + x.get("comments", 0) * 2), reverse=True)
    if items:
        for item in items:
            item.pop("key", None)
        return {"status":"ok", "last_success_at": now_iso(), "items": items[:6], "source_note":"HN 공개 피드 중 최근 45일·20점 이상·AI 관련 키워드만 선별. 시장 전체 대표값이 아니라 조기 신호입니다."}
    return {"status":"failed", "last_success_at": None, "items":[
        {"title":"AI 뉴스 수집 실패", "title_ko":"AI 뉴스 수집 실패", "summary":"공개 피드 연결 실패. 다음 갱신 때 재시도합니다.", "source":"대시보드 로컬"}
    ], "error":" | ".join(errors[-3:])}

def main():
    generated_at=now_iso()
    cal=google_calendar_events()
    airbnb=airbnb_calendar_snapshot()
    overview,warn,loans,inst,upcoming,fin_path=parse_finance_dashboard()
    inst_ledger=parse_installment_ledger()
    forecast=build_dashboard_projection(run_finance_forecast())
    events=add_airbnb_events(cal.get("events", []), airbnb)
    snap={
        "meta":{"demo":False,"generated_at":generated_at,"pc_last_seen_at":generated_at,"display_mode":"local_real_snapshot_no_external_upload"},
        "sources":{},
        "cards":{}
    }
    snap["sources"]["google_calendar"] = cal["source"]
    snap["sources"]["airbnb_calendar"] = airbnb["source"]
    snap["sources"]["finance_dashboard"] = source_status("재무통제실 대시보드", fin_path)
    snap["sources"]["finance_forecast"] = {"name":"재무 forecast", "status": forecast.get("status"), "last_attempt_at": generated_at, "last_success_at": generated_at if forecast.get("status")=="ok" else None, "error": forecast.get("error")}
    ina=inaworld_snapshot()
    chill=chill_snapshot(events)
    ai=ai_news_snapshot()
    for i,s in enumerate(ina.get("sources", [])): snap["sources"][f"inaworld_{i}"]=s
    for i,s in enumerate(chill.get("sources", [])): snap["sources"][f"chill_{i}"]=s
    today_date = datetime.now(KST).date()
    todays=[e for e in events if str(e.get('start','')).startswith(str(today_date))]
    tomorrow=[e for e in events if str(e.get('start','')).startswith(str(today_date+timedelta(days=1)))]
    counts = {"chill": 0, "airbnb": 0, "coupang": 0, "personal": 0}
    for e in events:
        cat = e.get("category", "personal")
        counts[cat] = counts.get(cat, 0) + 1
    snap["cards"]["calendar"]={"status":"ok" if cal["status"]=="ok" else "failed", "minis":[["오늘",f"{len(todays)}개"],["이번 달",f"{len(events)}개"]], "bullets":[e['summary'] for e in todays[:5]] or ["오늘 일정 없음"], "detail":{"events":events,"today":today_date.isoformat(),"month_start":cal.get("month_start"),"month_end":cal.get("month_end"),"counts":counts}}
    dashboard_warn = dashboard_finance_warnings(warn)
    cycle_start, cycle_end = current_income_cycle()
    cycle_outgoing_all = run_cycle_outgoing(cycle_start, cycle_end, cycle_start)
    cycle_outgoing_remaining = run_cycle_outgoing(cycle_start, cycle_end, today_date)
    cycle_outgoing = cycle_outgoing_all
    cycle_outgoing_items = cycle_outgoing_all.get("items", [])
    cycle_total = cycle_outgoing_all.get("total", 0)
    remaining_outgoing = cycle_outgoing_remaining.get("items", [])
    remaining_total = cycle_outgoing_remaining.get("total", 0)
    fin_bullets=[f"쿠팡 10/25 입금분 {DASHBOARD_INCOME_ASSUMPTIONS['쿠팡 10/25 입금분']:,}원", f"Airbnb 11월 수령 {DASHBOARD_INCOME_ASSUMPTIONS['에어비앤비 11월 수령완료+예정']:,}원", f"칠링 예약 {DASHBOARD_INCOME_ASSUMPTIONS['칠링스페이스 예약확정']:,}원"] + dashboard_warn[:2]
    # 재무는 사용자가 바로 판단할 수 있게 25~24 현금흐름 기준으로 표시한다.
    # 카드 전면·한눈에·상세 드릴다운은 모두 같은 25~24 기간을 써야 한다.
    snap["cards"]["finance"]={"status":"warning" if dashboard_warn else "ok", "minis":[["25~24 수입", f"{sum(DASHBOARD_INCOME_ASSUMPTIONS.values()):,}원"],["25~24 지출", f"{cycle_total:,}원"]], "bullets":fin_bullets, "detail":{"overview":overview,"income_assumptions":DASHBOARD_INCOME_ASSUMPTIONS,"income_period":DASHBOARD_INCOME_PERIOD,"cycle_start":cycle_start.isoformat(),"cycle_end":cycle_end.isoformat(),"cycle_outgoing":cycle_outgoing_items,"cycle_total":cycle_total,"remaining_outgoing":remaining_outgoing,"remaining_total":remaining_total,"remaining_from":cycle_outgoing_remaining.get("from_date", today_date.isoformat()),"remaining_by_kind":cycle_outgoing_remaining.get("by_kind", {}),"warnings":dashboard_warn,"raw_warnings_count":len(warn),"loans":loans,"installments":inst_ledger,"upcoming":upcoming,"forecast":forecast,"forecast_note":"현재 25~24 기간 수입은 쿠팡 10/25 입금분 + Airbnb 11월 수령완료/예정 + 칠링 예약수입을 반영했습니다."}}
    snap["cards"]["chill"]={"status":"warning", "minis":[["예약",f"{len(chill['reservations'])}건"],["청소",chill["cleaning_status"]]], "bullets":([f"예약 {len(chill['reservations'])}건"] if chill['reservations'] else ["오늘 캘린더 기준 칠링 예약 없음"]) + chill["marketing_warnings"][:2] + (["블로그 초안: "+chill['blog_drafts'][0]['name']] if chill['blog_drafts'] else []), "detail":chill}
    snap["cards"]["inaworld"]={"status":"ok", "minis":[["촬영 후보",f"{len(ina['shooting_candidates'])}개"],["민감 제외",f"{ina['filtered_sensitive_items']}개"]], "bullets":(ina['shooting_candidates'][:3] or ["촬영 후보 원본 확인 필요"]) + (ina['editing_priorities'][:2] if ina['editing_priorities'] else []), "detail":ina}
    inbox_files=latest_files(Path(r"C:/Users/smile/Documents/Obsidian/포이에마/2_지식/_raw/_수신함"), "*.md", 5) if Path(r"C:/Users/smile/Documents/Obsidian/포이에마/2_지식/_raw/_수신함").exists() else []
    snap["cards"]["inbox"]={"status":"ok", "minis":[["최근",f"{len(inbox_files)}개"],["등록", "요청 기반"]], "bullets":[f['name'] for f in inbox_files[:4]] or ["최근 인박스 항목 없음"], "detail":{"latest_files":inbox_files}}
    snap["cards"]["ai"]={"status":"ok" if ai.get("status")=="ok" else "warning", "minis":[["뉴스",f"{len(ai['items'])}개"],["상태","연결됨" if ai.get("status")=="ok" else "확인 필요"]], "bullets":[x['title'] for x in ai['items']], "detail":ai}
    (DATA/"snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status":"ok","path":str(DATA/"snapshot.json"),"cards":list(snap["cards"].keys())}, ensure_ascii=False))

if __name__ == "__main__":
    main()
