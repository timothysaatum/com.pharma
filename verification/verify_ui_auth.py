
import asyncio
from playwright.async_api import async_playwright
import os
import time

async def verify_ui():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1280, 'height': 1200}) # Increased height
        page = await context.new_page()

        print("Navigating to login page...")
        await page.goto("http://localhost:3000/login")

        print("Logging in...")
        admin_password = os.getenv("TEST_ADMIN_PASSWORD", "TemporaryPassword123!")
        await page.fill('input[name="username"]', 'admin')
        await page.fill('input[name="password"]', admin_password)
        await page.click('button[type="submit"]')

        print("Waiting for dashboard...")
        await page.wait_for_url("**/admin**", timeout=10000)

        # Verify Drugs Pagination
        print("Navigating to Drugs...")
        await page.goto("http://localhost:3000/admin/drugs")
        await page.wait_for_selector("text=Drug 1")
        await asyncio.sleep(1)
        # Scroll to bottom
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await page.screenshot(path="/home/jules/verification/drugs_page_v6.png")

        content = await page.content()
        if "Page 1 of 2" in content:
            print("SUCCESS: Found 'Page 1 of 2' on Drugs page.")
        else:
            print("FAILURE: Pagination text 'Page 1 of 2' NOT found on Drugs page.")

        # Verify Reports
        print("Navigating to Reports...")
        await page.goto("http://localhost:3000/reports")

        print("Verifying Daily Sales Report Pagination...")
        await page.wait_for_selector("text=Daily Sales")
        # Set date range to see all seeded data (last 60 days)
        await page.fill('input[type="date"] >> nth=0', '2026-04-01')
        await page.click('button:has-text("Refresh")')
        await asyncio.sleep(2)
        # Scroll to bottom
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await page.screenshot(path="/home/jules/verification/daily_sales_v6.png")

        content = await page.content()
        if "Showing page 1 of 2" in content:
            print("SUCCESS: Found 'Showing page 1 of 2' on Daily Sales report.")
        elif "Showing page" in content:
             print("Found 'Showing page' but not '1 of 2'.")
        else:
            print("Pagination text totally missing from Reports.")

        # Verify Drug Turnover
        print("Verifying Drug Turnover Pagination...")
        await page.click('button:has-text("Drug Turnover")')
        # Wait for Turnover specific table header
        await page.wait_for_selector("text=Units Sold", timeout=5000)
        await asyncio.sleep(2)
        # Scroll to bottom
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await page.screenshot(path="/home/jules/verification/drug_turnover_v6.png")

        content = await page.content()
        if "Showing page 1 of 2" in content:
             print("SUCCESS: Found 'Showing page 1 of 2' on Drug Turnover report.")
        else:
             print("INFO: 'Showing page 1 of 2' not found on Drug Turnover report.")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(verify_ui())
