import json
import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from job_agent_core.address_search import choose_result, search_query
from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.models import PersonalInfo, ResumeProfile
from tests.test_browser_worker import FakeSessions

FORM = """
<!doctype html><html lang="ko"><body>
<form id="f">
  <div class="row"><label for="zip">우편번호</label><input id="zip" readonly>
    <button id="find" type="button">우편번호 찾기</button></div>
  <div class="row"><label for="addr">기본주소</label><input id="addr" readonly></div>
  <div class="row"><label for="detail">상세주소</label><input id="detail"></div>
</form>
<div id="layer"></div>
<script>
  window.addEventListener('message', (event) => {
    document.getElementById('zip').value = event.data.zonecode;
    document.getElementById('addr').value = event.data.address;
    document.getElementById('layer').innerHTML = '';
  });
  document.getElementById('find').addEventListener('click', () => {
    document.getElementById('layer').innerHTML =
      '<iframe src="https://postcode.map.kakao.com/search" width="500" height="400"></iframe>';
  });
</script></body></html>
"""

# Mimics the Kakao widget: #region_name search box, li.list_post_item results, button.link_post.
WIDGET = """
<!doctype html><html><body>
<input id="region_name" type="text"><ul id="list"></ul>
<script>
  const rows = %s;
  document.getElementById('region_name').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    document.getElementById('list').innerHTML = rows.map((row, index) =>
      `<li class="list_post_item" data-zonecode="${row[0]}" data-addr="${row[1]}">${row[0]} 도로명
        <button type="button" class="link_post" data-index="${index}">${row[1]}</button> 지번 ${row[2]}</li>`).join('');
    document.querySelectorAll('.link_post').forEach((button) => button.addEventListener('click', () => {
      const row = rows[Number(button.dataset.index)];
      parent.postMessage({zonecode: row[0], address: row[1]}, '*');
    }));
  });
</script></body></html>
"""


class AddressQueryTests(unittest.TestCase):
    def test_query_stops_at_the_building_number(self):
        self.assertEqual(search_query("서울특별시 중구 세종대로 110, 3층 301호"), "서울특별시 중구 세종대로 110")
        self.assertEqual(search_query("경기도 성남시 분당구 정자동 178-1 101동"), "경기도 성남시 분당구 정자동 178-1")

    def test_only_a_single_match_is_chosen(self):
        rows = [
            {"zonecode": "04524", "addr": "서울 중구 세종대로 110", "text": "04524 서울 중구 세종대로 110"},
            {"zonecode": "99999", "addr": "부산 중구 세종대로 110", "text": "99999 부산 중구 세종대로 110"},
        ]
        self.assertEqual(choose_result(rows, "서울특별시 중구 세종대로 110", "")[0], None)
        self.assertEqual(choose_result(rows, "서울특별시 중구 세종대로 110", "04524"), (0, []))
        index, candidates = choose_result(rows, "서울특별시 중구 세종대로 110", "")
        self.assertEqual(candidates, ["(04524) 서울 중구 세종대로 110", "(99999) 부산 중구 세종대로 110"])
        # A result that does not contain the resume's road name and number is never picked.
        self.assertEqual(choose_result(rows[:1], "서울특별시 중구 을지로 1", "")[0], None)


class AddressSearchBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.sessions = FakeSessions(self.page, Path(self.temp.name))
        self.sessions.session.approved_domains.add("postcode.map.kakao.com")
        self.worker = BrowserAutomationWorker(self.sessions)
        self.profile = ResumeProfile(
            personal=PersonalInfo(
                name={"full": "홍길동"},
                address="서울특별시 중구 세종대로 110",
                address_detail="3층",
            )
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def run_fill(self, rows):
        async def widget(route):
            await route.fulfill(content_type="text/html; charset=utf-8", body=WIDGET % json.dumps(rows, ensure_ascii=False))

        await self.page.route("https://postcode.map.kakao.com/**", widget)
        await self.page.set_content(FORM)
        report = await self.worker.fill("task", self.profile)
        return report, {item.label: item for item in report.results}

    async def values(self):
        return [await self.page.locator(selector).input_value() for selector in ("#zip", "#addr", "#detail")]

    async def test_single_result_fills_address_then_detail(self):
        report, rows = await self.run_fill([["04524", "서울 중구 세종대로 110", "서울 중구 태평로1가 31"]])
        self.assertEqual(await self.values(), ["04524", "서울 중구 세종대로 110", "3층"])
        self.assertEqual(rows["기본주소"].status, "filled_verified", rows["기본주소"].reason)
        self.assertEqual(rows["우편번호"].status, "filled_verified")
        self.assertEqual(rows["상세주소"].status, "filled_verified")
        self.assertEqual(report.field_count, 3)

    async def test_several_results_are_left_to_the_user_with_candidates(self):
        _, rows = await self.run_fill(
            [["04524", "서울 중구 세종대로 110", "-"], ["04525", "서울 중구 세종대로 110-1", "-"]]
        )
        self.assertEqual(await self.values(), ["", "", ""])
        self.assertEqual(rows["기본주소"].status, "needs_human")
        self.assertIn("여러 개", rows["기본주소"].reason)
        self.assertIn("(04524) 서울 중구 세종대로 110", rows["기본주소"].candidates)
        # 상세주소 alone would be filled under an empty address; it waits for the user.
        self.assertEqual(rows["상세주소"].status, "needs_human")
        # The steps are shown for diagnosis, but never the address itself.
        self.assertIn("진단: button:found > widget:frame > results:2", rows["기본주소"].reason)
        self.assertNotIn("세종대로", rows["기본주소"].reason)
        # The search window stays open with the resume address already searched.
        widget = next(frame for frame in self.page.frames if "postcode.map.kakao.com" in frame.url)
        self.assertEqual(await widget.locator("#region_name").input_value(), "서울특별시 중구 세종대로 110")

        # The user picks a result; the next run keeps it and fills 상세주소.
        await widget.locator(".link_post").first.click()
        await self.page.wait_for_function("document.getElementById('addr').value !== ''")
        report = await self.worker.fill("task", self.profile)
        rows = {item.label: item for item in report.results}
        self.assertEqual(rows["기본주소"].status, "kept_existing", (rows["기본주소"].reason, await self.values()))
        self.assertEqual(rows["상세주소"].status, "filled_verified")
        self.assertEqual(await self.values(), ["04524", "서울 중구 세종대로 110", "3층"])

    async def test_popup_page_that_embeds_the_widget(self):
        async def site_popup(route):
            await route.fulfill(
                content_type="text/html; charset=utf-8",
                body='<iframe src="https://postcode.map.kakao.com/search" width="500" height="400"></iframe>'
                "<script>window.addEventListener('message', (e) => { opener.postMessage(e.data, '*'); window.close(); });</script>",
            )

        await self.page.context.route("https://example.test/zip-popup", site_popup)
        await self.page.context.route(
            "https://postcode.map.kakao.com/**",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body=WIDGET % json.dumps([["04524", "서울 중구 세종대로 110", "-"]], ensure_ascii=False),
            ),
        )
        await self.page.set_content(FORM.replace(
            "document.getElementById('layer').innerHTML =\n      '<iframe src=\"https://postcode.map.kakao.com/search\" width=\"500\" height=\"400\"></iframe>';",
            "window.open('https://example.test/zip-popup', 'zip', 'width=500,height=500');",
        ))
        report = await self.worker.fill("task", self.profile)
        rows = {item.label: item for item in report.results}
        self.assertEqual(rows["기본주소"].status, "filled_verified", rows["기본주소"].reason)
        self.assertEqual(await self.values(), ["04524", "서울 중구 세종대로 110", "3층"])

    async def test_unapproved_widget_domain_asks_for_approval(self):
        # Defensive: sessions approve the postcode domains by default, but a removed approval is honoured.
        self.sessions.session.approved_domains.discard("postcode.map.kakao.com")
        _, rows = await self.run_fill([["04524", "서울 중구 세종대로 110", "-"]])
        self.assertEqual(await self.values(), ["", "", ""])
        self.assertIn("허용", rows["기본주소"].reason)
        self.assertIn("postcode.map.kakao.com", self.sessions.session.discovered_frame_hosts)
        # The second check must not fill 상세주소 on its own either.
        asked = []

        async def lookup(items):
            asked.extend(item.field.label for item in items)
            return {item.field.key: "3층" for item in items}, {}

        requests = [(item.fingerprint, item.status) for item in rows.values() if item.status == "needs_human"]
        await self.worker.fill_lookup("task", self.profile, lookup, requests)
        self.assertEqual(asked, [])
        self.assertEqual(await self.values(), ["", "", ""])

    async def test_submit_type_search_button_is_never_clicked(self):
        await self.page.set_content(
            """<form id="f" onsubmit="event.preventDefault(); window.submitted = 1">
              <label for="addr">주소</label><input id="addr" readonly><button>주소 검색</button>
              <label for="detail">상세주소</label><input id="detail"></form>"""
        )
        report = await self.worker.fill("task", self.profile)
        rows = {item.label: item for item in report.results}
        self.assertIn("제출 버튼", rows["주소"].reason)
        self.assertEqual(rows["상세주소"].status, "needs_human")
        self.assertIsNone(await self.page.evaluate("window.submitted"))


if __name__ == "__main__":
    unittest.main()
