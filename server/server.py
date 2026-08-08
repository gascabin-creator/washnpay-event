"""
WashnPay 포인트 자동 적립 서버
- /register  : 웹폼에서 직접 호출 (랜딩 페이지 → 포인트 적립 → 화면 안내)
- /webhook   : MacroDroid SMS 트리거 (기존 호환)
"""
import os
import re
import smtplib
import ssl
import subprocess
import sys
import threading
import logging
import time
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, send_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
SCRIPT_PATH = "point_auto.py"

# 중복 처리 방지: (branch, phone) → 마지막 처리 시각
_recent: dict = {}
_recent_lock = threading.Lock()
DEDUP_SECONDS = 300  # 5분 이내 동일 번호+지점 재요청 무시

# [WNP]지점/가방여부/전화번호[/이름]
SMS_PATTERN = re.compile(
    r"\[WNP\]([^/]+)/(가방[OX])/(\d{2,3}-?\d{3,4}-?\d{4})(?:/(.+))?"
)

# 조건별 답장 문자 메시지
# ※ 한글 SMS는 70자 초과 시 분할(multi-part) 발송되어 수신 실패 위험이 큼.
#   → 이모지 제거 + 70자 이내로 압축하여 단일 SMS 전송 보장.
# 지점별 포인트 금액
AMOUNTS = {
    ("별내점",        True):  "60000",
    ("별내점",        False): "65000",
    ("별내카페거리점", True):  "60000",
    ("별내카페거리점", False): "65000",
}

# 가방 수령 안내 (화면 표시용)
BAG_INFO = {
    "별내점":        "가방은 테이블 옆 3단서랍장 가운데칸 (자물쇠 202)",
    "별내카페거리점": "세탁가방은 오투닭갈비에서 받으세요",
}

# SMS 답장 (MacroDroid /webhook 호환용)
REPLY_MESSAGES = {
    ("별내카페거리점", True):  "[워시앤페이] 5만원 입금확인, 포인트 6만원 충전완료. 세탁가방은 오투닭갈비에서 받으세요. 감사합니다.",
    ("별내카페거리점", False): "[워시앤페이] 5만원 입금확인, 가방 대신 포인트 65,000원 충전완료. 감사합니다.",
    ("별내점", True):         "[워시앤페이] 5만원 입금확인, 포인트 6만원 충전완료. 가방은 테이블 옆 3단서랍장 가운데칸(자물쇠 202). 감사합니다.",
    ("별내점", False):        "[워시앤페이] 5만원 입금확인, 가방 대신 포인트 65,000원 충전완료. 감사합니다.",
}


GMAIL_APP_PW  = os.environ.get("GMAIL_APP_PW", "")
NOTIFY_EMAIL  = "gascabin@gmail.com"


def _send_email(subject: str, body: str) -> None:
    if not GMAIL_APP_PW:
        log.warning("[EMAIL] GMAIL_APP_PW 미설정 — 이메일 발송 건너뜀")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = NOTIFY_EMAIL
        msg["To"]      = NOTIFY_EMAIL
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.login(NOTIFY_EMAIL, GMAIL_APP_PW)
            smtp.sendmail(NOTIFY_EMAIL, NOTIFY_EMAIL, msg.as_bytes())
        log.info(f"[EMAIL] 발송 성공: {subject!r}")
    except Exception as e:
        log.error(f"[EMAIL] 오류: {e}")


def _run_script(phone_last4: str, amount: str, name: str = None, branch: str = "별내카페거리점"):
    cmd = [sys.executable, SCRIPT_PATH, phone_last4, amount, name or "", branch]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=120
        )
        return result
    except subprocess.TimeoutExpired:
        return None


# ── CORS (GitHub Pages → Cloud Run 허용) ─────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/register", methods=["OPTIONS"])
@app.route("/webhook",  methods=["OPTIONS"])
def preflight():
    return "", 204


# ── 웹폼 직접 제출 (/register) ───────────────────────────────────────────────

@app.route("/register", methods=["POST"])
def register():
    """
    랜딩 페이지에서 직접 호출.
    Body JSON: {"branch": "별내점", "bag": "가방O", "phone": "010-xxxx-xxxx", "name": "홍길동"}
    Response JSON: {"success": true, "amount": "60000", "bag_info": "..."} or {"success": false, "error": "..."}
    """
    data = request.get_json(silent=True) or {}
    branch    = (data.get("branch") or "").strip()
    bag       = (data.get("bag") or "").strip()        # "가방O" | "가방X"
    phone_raw = (data.get("phone") or "").strip()
    cust_name = (data.get("name") or "").strip() or None

    count = max(1, min(4, int(data.get("count") or 1)))

    if not branch or bag not in ("가방O", "가방X") or not phone_raw:
        return jsonify({"success": False, "error": "필수 항목이 누락되었습니다."}), 400

    full_phone = re.sub(r"\D", "", phone_raw)
    if len(full_phone) < 10:
        return jsonify({"success": False, "error": "전화번호를 확인해 주세요."}), 400

    bag_needed  = (bag == "가방O")
    base_amount = AMOUNTS.get((branch, bag_needed), "60000")
    amount      = str(int(base_amount) * count)
    phone_last4 = full_phone[-4:]

    # 중복 방지
    dedup_key = (branch, full_phone)
    now = time.time()
    with _recent_lock:
        if now - _recent.get(dedup_key, 0) < DEDUP_SECONDS:
            elapsed = int(now - _recent[dedup_key])
            log.warning(f"[DEDUP] {dedup_key} 중복 무시 ({elapsed}초 전)")
            return jsonify({"success": False, "error": f"이미 처리된 요청입니다. ({elapsed}초 전)"}), 409
        _recent[dedup_key] = now

    log.info(f"[REGISTER] {branch} {phone_last4} {amount}원 {count}회 (이름={cust_name})")
    result = _run_script(phone_last4, amount, cust_name, branch)

    if result is None:
        log.error("[REGISTER] 타임아웃")
        return jsonify({"success": False, "error": "처리 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."}), 504

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        log.error(f"[REGISTER] 실패: {err}")
        # 전화번호로 아예 검색되지 않음 = 앱에서 이 지점을 즐겨찾기(지점 등록) 안 한 경우가 대부분
        if "해당하는 고객이 없습니다" in err or "검색 결과가 없습니다" in err:
            return jsonify({
                "success": False,
                "error": "워시앤페이 앱에서 지점 등록(즐겨찾기) 후 다시 시도해 주세요.",
                "error_type": "not_registered",
            }), 400
        if "없습니다" in err:
            return jsonify({"success": False, "error": "워시앤페이 앱에 가입된 번호를 확인해 주세요."}), 400
        return jsonify({"success": False, "error": "포인트 적립 중 오류가 발생했습니다. 관리자에게 문의해 주세요."}), 500

    log.info(f"[REGISTER] 성공: {result.stdout.strip()}")

    amt_fmt  = f"{int(amount):,}"
    label    = cust_name or f"끝번호 {phone_last4}"
    subject  = f"[워시앤페이] 적립완료 — {branch} {amt_fmt}원"
    body     = (
        f"지점     : {branch}\n"
        f"고객     : {label}\n"
        f"전화번호 : ***-****-{phone_last4}\n"
        f"충전금액 : {amt_fmt}원\n"
        f"충전횟수 : {count}회\n"
        f"가방여부 : {'포함' if bag_needed else '미포함'}\n"
    )
    threading.Thread(target=_send_email, args=(subject, body), daemon=True).start()

    return jsonify({
        "success":  True,
        "amount":   amount,
        "bag_info": BAG_INFO.get(branch, "") if bag_needed else "",
    })


# ── MacroDroid 웹훅 ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    MacroDroid에서 호출.
    Body JSON: {"body": "[WNP]...", "sender": "010-xxxx-xxxx"}
    """
    raw = request.get_data(as_text=True)
    log.info(f"[WEBHOOK] raw={raw!r}")

    data = request.get_json(silent=True) or {}
    sender = data.get("sender", "")

    # JSON body 우선, 없으면 raw body 자체를 SMS 내용으로 처리
    sms_body = data.get("body", "") or raw.strip()
    log.info(f"[SMS_BODY] {sms_body!r}")

    m = SMS_PATTERN.search(sms_body)
    if not m:
        log.warning(f"[NO MATCH] sms_body={sms_body!r}")
        return f"WNP 형식 아님: {sms_body}", 200

    branch = m.group(1).strip()
    bag = m.group(2)          # "가방O" or "가방X"
    full_phone = re.sub(r"-", "", m.group(3))
    cust_name = (m.group(4) or "").strip() or None

    bag_needed = (bag == "가방O")
    amount = "60000" if bag_needed else "65000"
    phone_last4 = full_phone[-4:]

    # 중복 처리 방지: 5분 이내 동일 (지점+전화번호) 재요청 무시
    dedup_key = (branch, full_phone)
    now = time.time()
    with _recent_lock:
        last_ts = _recent.get(dedup_key, 0)
        if now - last_ts < DEDUP_SECONDS:
            elapsed = int(now - last_ts)
            log.warning(f"[DEDUP] {dedup_key} 중복 요청 무시 ({elapsed}초 전 처리됨)")
            return f"중복 요청 무시 ({elapsed}초 전 처리됨)", 200
        _recent[dedup_key] = now

    # 답장 메시지 결정
    reply = REPLY_MESSAGES.get(
        (branch, bag_needed),
        "안녕하세요! 포인트가 적립되었습니다. 워시앤페이를 이용해 주셔서 감사합니다!"
    )

    # Playwright 포인트 적립을 동기적으로 실행 (완료 후 SMS 응답 반환)
    # → Cloud Run이 요청 처리 중 컨테이너를 종료하지 않음
    log.info(f"[PLAYWRIGHT] 시작: {phone_last4} {amount}원 {branch}")
    result = _run_script(phone_last4, amount, cust_name, branch)
    if result is None:
        log.error("[PLAYWRIGHT] 타임아웃")
    elif result.returncode == 0:
        log.info(f"[PLAYWRIGHT] 성공: {result.stdout.strip()}")
    else:
        log.error(f"[PLAYWRIGHT] 실패: {result.stderr.strip() or result.stdout.strip()}")

    # MacroDroid가 응답 본문을 그대로 SMS로 발송
    return reply, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ── 기존 수동 호출 엔드포인트 (하위 호환) ────────────────────────────────────

def _get_params():
    json_data = request.get_json(silent=True) or {}
    def pick(key):
        return str(json_data.get(key) or request.args.get(key, "")).strip()
    return pick("phone_last4"), pick("amount"), pick("name")


@app.route("/add-point", methods=["GET", "POST"])
def add_point():
    phone_last4, amount, name = _get_params()

    missing = [f for f, v in [("phone_last4", phone_last4), ("amount", amount)] if not v]
    if missing:
        return jsonify({"success": False, "error": f"필수 항목 누락: {', '.join(missing)}"}), 400
    if not phone_last4.isdigit() or len(phone_last4) != 4:
        return jsonify({"success": False, "error": "phone_last4는 숫자 4자리"}), 400
    if not amount.isdigit():
        return jsonify({"success": False, "error": "amount는 숫자"}), 400

    result = _run_script(phone_last4, amount, name or None)
    if result is None:
        return jsonify({"success": False, "error": "타임아웃"}), 504

    label = f"{phone_last4}" + (f" / {name}" if name else "")
    if result.returncode == 0:
        return jsonify({"success": True, "message": f"{label} → {amount}포인트 적립 완료"})
    else:
        return jsonify({"success": False, "error": result.stderr.strip() or result.stdout.strip()}), 500


@app.route("/health")
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
