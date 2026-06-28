import sys
import time
import traceback
import re

# Windows 콘솔 인코딩 문제 방지
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# 지점별 계정
ACCOUNTS = {
    "별내카페거리점": {"id": "kr9713", "pw": "9713kr"},
    "별내점":       {"id": "kr4522", "pw": "4522kr"},
}
BASE_URL = "https://manager.washnpay.com"


def extract_last4(phone_text: str) -> str:
    """마스킹된 전화번호에서 끝 4자리 추출. 예) '010-4**2-9713 🔗' → '9713'"""
    # 공백 기준 첫 토큰만 사용 (아이콘 제거)
    token = phone_text.strip().split()[0] if phone_text.strip() else ""
    # 마지막 '-' 뒤 4자리
    parts = token.split("-")
    if len(parts) >= 2:
        return parts[-1].strip()
    # '-' 없으면 끝 4자리
    digits = re.sub(r"[^\d]", "", token)
    return digits[-4:] if len(digits) >= 4 else digits


def find_target_row(page, phone_last4: str, name: str = None):
    """
    검색 결과 테이블에서 조건에 맞는 행(row)을 반환한다.

    매칭 우선순위:
    1순위 - 이름 제공 시: 뒤 4자리 필터 후 이름으로 최종 확정 (이름 미일치 → 오류)
    2순위 - 이름 미제공 + 1명 일치: 해당 행 반환
    3순위 - 이름 미제공 + 여러 명 일치: 이름 요구 오류
    4순위 - 일치 없음: 오류 메시지 출력
    """
    rows = page.locator("table tbody tr").all()
    if not rows:
        raise ValueError("검색 결과가 없습니다.")

    # 전화번호 뒤 4자리로 1차 필터
    phone_matched = []
    for row in rows:
        cells = row.locator("td").all()
        if len(cells) < 3:
            continue
        phone_text = cells[1].inner_text()
        last4 = extract_last4(phone_text)
        if last4 == phone_last4:
            cust_name = cells[2].inner_text().strip()
            phone_matched.append((cust_name, row))

    # 4순위: 전화번호 뒤 4자리 일치 없음
    if not phone_matched:
        raise ValueError(
            f"전화번호 뒤 4자리 '{phone_last4}'에 해당하는 고객이 없습니다."
        )

    names_found = [n for n, _ in phone_matched]

    # 1순위 + 3순위: 이름 제공 시 → 뒤 4자리 필터 결과에서 이름으로 최종 확정
    if name:
        if len(phone_matched) > 1:
            print(f"    → 뒤 4자리 '{phone_last4}' 일치 {len(phone_matched)}명: {', '.join(names_found)}")
        name_matched = [(n, r) for n, r in phone_matched if n == name]
        if not name_matched:
            raise ValueError(
                f"뒤 4자리 '{phone_last4}' 중 이름 '{name}'인 고객이 없습니다.\n"
                f"검색된 고객: {', '.join(names_found)}"
            )
        cust_name, row = name_matched[0]
        print(f"    → 고객 확인: {cust_name} (뒤 4자리 {phone_last4} + 이름 매칭)")
        return row

    # 2순위: 이름 미제공 + 1명만 일치 → 바로 반환
    if len(phone_matched) == 1:
        cust_name, row = phone_matched[0]
        print(f"    → 고객 확인: {cust_name} (뒤 4자리 {phone_last4})")
        return row

    # 3순위 예외: 이름 미제공 + 여러 명 일치 → 이름 요구
    raise ValueError(
        f"동일한 뒤 4자리를 가진 고객이 {len(phone_matched)}명입니다. "
        f"이름을 지정해 주세요.\n"
        f"검색된 고객: {', '.join(names_found)}"
    )


def run(phone_last4: str, amount: str, name: str = None, branch: str = "별내카페거리점"):
    account = ACCOUNTS.get(branch, ACCOUNTS["별내카페거리점"])

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            # ── 1. 로그인 ──────────────────────────────────────────────
            print(f"[1/5] 로그인 중... ({branch})")
            page.goto(BASE_URL)
            page.wait_for_load_state("networkidle")

            page.fill("input[placeholder='Username']", account["id"])
            page.fill("input[placeholder='Password']", account["pw"])

            # 체크박스 체크 (로그인 상태 유지 경고 모달 자동 닫기)
            for chk in page.locator("input[type='checkbox']").all():
                if not chk.is_checked():
                    chk.check()
                    try:
                        modal = page.locator(".modal.show, .modal.d-block")
                        if modal.is_visible(timeout=1000):
                            page.click("button:has-text('확인')")
                            time.sleep(0.3)
                    except Exception:
                        pass

            page.click("button:has-text('Login')")
            page.wait_for_load_state("networkidle")

            # ── 2. 고객관리 → 회원관리 ────────────────────────────────
            print("[2/5] 고객관리 메뉴 이동 중...")
            page.click("text=고객관리")
            time.sleep(0.8)
            page.click("text=회원관리")
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)

            # ── 3. 전화번호 검색 ───────────────────────────────────────
            label = f"{phone_last4}" + (f" / {name}" if name else "")
            print(f"[3/5] 검색 중... ({label})")
            page.fill("input[placeholder='번호 or 이름 입력']", phone_last4)
            page.click("button:has-text('검색')")
            page.wait_for_load_state("networkidle")
            time.sleep(0.8)

            # ── 4. 대상 고객 행 찾기 & 포인트 적립 버튼 클릭 ───────────
            print("[4/5] 대상 고객 검색 및 포인트 적립 버튼 클릭 중...")
            target_row = find_target_row(page, phone_last4, name)
            target_row.locator("button:has-text('포인트 적립')").click()
            time.sleep(0.8)

            # ── 5. 팝업에서 금액 / 이유 입력 후 저장 ───────────────────
            print(f"[5/5] 팝업 입력 중... (금액={amount}, 이유=현금충전 보너스)")
            modal = page.locator(".modal.show, .modal.d-block").first
            modal_inputs = modal.locator("input[type='text']")
            reason = (
                "이벤트 5만원 충전 (가방포함)"
                if amount == "60000"
                else "이벤트 5만원 충전 (가방미포함)"
            )
            # 적립 전 지점포인트 기록 (저장 검증용)
            target_cells = target_row.locator("td")
            before_pts = target_cells.nth(4).inner_text().strip()

            # 컨트롤드 input이라 fill()로는 onChange 미발생 → 실제 키 입력 사용
            modal_inputs.nth(1).click()
            modal_inputs.nth(1).press_sequentially(amount, delay=30)
            modal_inputs.nth(2).click()
            modal_inputs.nth(2).press_sequentially(reason, delay=30)
            time.sleep(0.3)

            modal.locator("button:has-text('저장')").click()
            page.wait_for_load_state("networkidle")
            time.sleep(1.0)

            # ── 저장 검증: 재검색하여 포인트 증가 확인 ─────────────────
            page.fill("input[placeholder='번호 or 이름 입력']", phone_last4)
            page.click("button:has-text('검색')")
            page.wait_for_load_state("networkidle")
            time.sleep(0.8)
            verify_row = find_target_row(page, phone_last4, name)
            after_pts = verify_row.locator("td").nth(4).inner_text().strip()

            def _to_int(s):
                return int(re.sub(r"[^\d]", "", s) or "0")

            diff = _to_int(after_pts) - _to_int(before_pts)
            if diff == int(amount):
                print(f"\n✅ 완료! {label} 고객 {before_pts} → {after_pts} (+{amount} 적립 확인)")
            else:
                raise ValueError(
                    f"적립 검증 실패: 기대 +{amount}, 실제 {before_pts}→{after_pts} (차이 {diff})"
                )

        except PlaywrightTimeoutError as e:
            print(f"\n❌ 타임아웃 오류: {e}", file=sys.stderr)
            page.screenshot(path="error_screenshot.png")
            print("   오류 스크린샷 저장: error_screenshot.png", file=sys.stderr)
            raise
        except ValueError as e:
            print(f"\n❌ {e}", file=sys.stderr)
            try:
                page.screenshot(path="error_screenshot.png")
                print("   오류 스크린샷 저장: error_screenshot.png", file=sys.stderr)
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"\n❌ 오류 발생: {e}", file=sys.stderr)
            try:
                page.screenshot(path="error_screenshot.png")
                print("   오류 스크린샷 저장: error_screenshot.png", file=sys.stderr)
            except Exception:
                pass
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    # 인수: phone_last4 amount [name] [branch]
    if len(sys.argv) < 3:
        print("사용법: python point_auto.py <전화번호뒤4자리> <포인트금액> [이름] [지점]")
        sys.exit(1)

    _, phone, amount = sys.argv[:3]
    cust_name = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
    branch_arg = sys.argv[4] if len(sys.argv) > 4 else "별내카페거리점"

    try:
        run(phone, amount, cust_name, branch_arg)
    except Exception as e:
        print(f"\n❌ 최종 오류: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
