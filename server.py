"""
WashnPay 포인트 자동 적립 서버
MacroDroid 웹훅 수신 및 기존 /add-point 엔드포인트 모두 지원.

SMS 형식: [WNP]별내점/가방O/010-1234-5678
         [WNP]별내카페거리점/가방X/010-1234-5678/홍길동  ← 이름 선택
"""
import re
import subprocess
import sys
import socket
import threading
import logging
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
SCRIPT_PATH = "point_auto.py"

# [WNP]지점/가방여부/전화번호[/이름]
SMS_PATTERN = re.compile(
    r"\[WNP\]([^/]+)/(가방[OX])/(\d{2,3}-?\d{3,4}-?\d{4})(?:/(.+))?"
)

# 조건별 답장 문자 메시지
# ※ 한글 SMS는 70자 초과 시 분할(multi-part) 발송되어 수신 실패 위험이 큼.
#   → 이모지 제거 + 70자 이내로 압축하여 단일 SMS 전송 보장.
REPLY_MESSAGES = {
    ("별내카페거리점", True): (
        "[워시앤페이] 5만원 입금확인, 포인트 6만원 충전완료. "
        "세탁가방은 오투닭갈비에서 받으세요. 감사합니다."
    ),
    ("별내카페거리점", False): (
        "[워시앤페이] 5만원 입금확인, 가방 대신 포인트 65,000원 충전완료. 감사합니다."
    ),
    ("별내점", True): (
        "[워시앤페이] 5만원 입금확인, 포인트 6만원 충전완료. "
        "가방은 테이블 옆 3단서랍장 가운데칸(자물쇠 202). 감사합니다."
    ),
    ("별내점", False): (
        "[워시앤페이] 5만원 입금확인, 가방 대신 포인트 65,000원 충전완료. 감사합니다."
    ),
}


def _run_script(phone_last4: str, amount: str, name: str = None, branch: str = "별내카페거리점"):
    cmd = [sys.executable, SCRIPT_PATH, phone_last4, amount, name or "", branch]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=120
        )
        return result
    except subprocess.TimeoutExpired:
        return None


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

    # 답장 메시지 즉시 결정
    reply = REPLY_MESSAGES.get(
        (branch, bag_needed),
        "안녕하세요! 포인트가 적립되었습니다. 😊 워시앤페이를 이용해 주셔서 감사합니다!"
    )

    # Playwright 포인트 적립은 백그라운드에서 실행 (MacroDroid 응답 즉시 반환)
    def run_in_background():
        log.info(f"[PLAYWRIGHT] 시작: {phone_last4} {amount}원 {branch}")
        result = _run_script(phone_last4, amount, cust_name, branch)
        if result is None:
            log.error("[PLAYWRIGHT] 타임아웃")
        elif result.returncode == 0:
            log.info(f"[PLAYWRIGHT] 성공: {result.stdout.strip()}")
        else:
            log.error(f"[PLAYWRIGHT] 실패: {result.stderr.strip() or result.stdout.strip()}")

    threading.Thread(target=run_in_background, daemon=True).start()

    # MacroDroid가 응답 본문을 그대로 SMS로 발송할 수 있도록 plain text 반환
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
