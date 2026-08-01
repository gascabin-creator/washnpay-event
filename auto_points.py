import asyncio
import os
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from dotenv import load_dotenv

load_dotenv()

ADMIN_URL = "https://manager.washnpay.com"
ADMIN_ID = os.getenv("WASHNPAY_ID", "")
ADMIN_PW = os.getenv("WASHNPAY_PW", "")
SESSION_FILE = "session.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("washnpay.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


async def _ensure_logged_in(page, context):
    await page.goto(f"{ADMIN_URL}/manage-users/manage-users", wait_until="networkidle")
    if "/manage-users" not in page.url:
        log.info("세션 만료 - 재로그인")
        await page.goto(f"{ADMIN_URL}/login")
        await page.fill('input[type="text"]', ADMIN_ID)
        await page.fill('input[type="password"]', ADMIN_PW)
        await page.click('button[type="submit"]')
        await page.wait_for_url("**/dashboard", timeout=15000)
        await context.storage_state(path=SESSION_FILE)
        await page.goto(f"{ADMIN_URL}/manage-users/manage-users", wait_until="networkidle")


async def credit_points(phone: str, bag_needed: bool, branch: str) -> dict:
    """
    전화번호로 회원 검색 후 포인트 적립.
      bag_needed=True  → 60,000점
      bag_needed=False → 65,000점
    """
    points = 60000 if bag_needed else 65000
    reason = f"5만원 이벤트 - {branch} {'가방포함' if bag_needed else '가방미포함'}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        ctx_kwargs = {}
        if os.path.exists(SESSION_FILE):
            ctx_kwargs["storage_state"] = SESSION_FILE
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        try:
            await _ensure_logged_in(page, context)

            # 전화번호 검색
            await page.locator('input[placeholder*="번호"]').fill(phone)
            await page.click('button:has-text("검색")')
            await page.wait_for_load_state("networkidle")

            # 해당 고객 행 확인
            row = page.locator(f'tr:has-text("{phone}")')
            if await row.count() == 0:
                log.warning(f"회원 없음: {phone}")
                return {"success": False, "error": f"'{phone}' 회원을 찾을 수 없습니다."}

            # 포인트 적립 버튼 클릭
            await row.first.locator('button:has-text("포인트 적립")').click()
            await page.wait_for_timeout(600)

            # 모달 입력
            modal = page.locator('[role="dialog"], .modal-content, .modal').first
            # 두 번째 input = 적립할 포인트 (첫 번째는 현재포인트 표시)
            await modal.locator("input").nth(1).fill(str(points))
            await modal.locator("textarea").fill(reason)
            await modal.locator('button:has-text("저장")').click()
            await page.wait_for_timeout(1000)

            await context.storage_state(path=SESSION_FILE)
            log.info(f"적립 완료: {phone} → {points:,}점 [{branch}]")
            return {"success": True, "phone": phone, "points": points, "branch": branch}

        except PlaywrightTimeoutError as e:
            log.error(f"타임아웃: {e}")
            return {"success": False, "error": "타임아웃"}
        except Exception as e:
            log.error(f"오류: {e}")
            return {"success": False, "error": str(e)}
        finally:
            await browser.close()


if __name__ == "__main__":
    # 테스트 실행
    result = asyncio.run(credit_points("010-0000-0000", bag_needed=True, branch="별내점"))
    print(result)
