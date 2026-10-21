"""Browser checks with an isolated server and data directory (no paid API calls)."""
from pathlib import Path
import base64
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "web_preview"


def main():
    OUTPUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="robo-browser-") as temporary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, LINGSHU_PUBLIC_MODE="1", LINGSHU_DATA_DIR=temporary,
                   DEEPSEEK_API_KEY="", LINGSHU_MEMORY_DIR=str(Path(temporary) / "memory"),
                   MEMPALACE_PALACE_PATH=str(Path(temporary) / "palace"))
        with (Path(temporary) / "server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen([sys.executable, "start_web.py", "--port", str(port)],
                                      cwd=ROOT, env=env, stdout=log, stderr=log)
            url = f"http://127.0.0.1:{port}"
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen(url + "/health", timeout=0.5)
                        break
                    except Exception:
                        if server.poll() is not None:
                            raise RuntimeError("Test server failed to start")
                        time.sleep(0.1)
                errors = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 900})
                    # Mandatory JS/CSS/model files must work even with external sites blocked.
                    context.route("https://**/*", lambda route: route.abort())
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    upload = Path(temporary) / "test-avatar.png"
                    Image.new("RGB", (160, 240), "#cc5577").save(upload)
                    legacy_avatar = "data:image/png;base64," + base64.b64encode(upload.read_bytes()).decode("ascii")
                    # Reproduce a browser that already has an old uploaded portrait.
                    # Default Live2D must win without deleting that stored image.
                    page.goto(url + "/health", wait_until="domcontentloaded")
                    page.evaluate("""async source => {
                        const db = await new Promise((resolve, reject) => {
                            const request = indexedDB.open('lingshu_visual_store', 1);
                            request.onupgradeneeded = () => request.result.createObjectStore('visuals');
                            request.onsuccess = () => resolve(request.result);
                            request.onerror = () => reject(request.error);
                        });
                        await new Promise((resolve, reject) => {
                            const tx = db.transaction('visuals', 'readwrite');
                            tx.objectStore('visuals').put({type: 'url', url: source}, 'avatar');
                            tx.oncomplete = resolve;
                            tx.onerror = () => reject(tx.error);
                        });
                        db.close();
                        localStorage.removeItem('ls_avatar_mode');
                    }""", legacy_avatar)
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_function("!isAnimating && _l2dModel !== null", timeout=25000)
                    assert page.evaluate("!avatarIsCustom && _l2dApp.ticker.started")
                    assert page.locator("#live2d-container").is_visible()
                    assert page.evaluate("async () => (await idbGet('avatar')).url") == legacy_avatar
                    assert page.locator("#hero-img").evaluate("el => getComputedStyle(el).display") == "none"
                    assert page.locator("#console-toggle").is_hidden()
                    assert page.evaluate("""() => {
                        const m = _l2dModel, b = _l2dBounds;
                        const top = m.y + (b.centerY-b.height/2)*m.scale.y;
                        return top >= 0 && top+b.height*m.scale.y <= _l2dApp.renderer.height;
                    }""")
                    page.screenshot(path=str(OUTPUT / "home.png"))
                    page.locator('[data-target="1"]').click()
                    page.wait_for_function("currentPage === 1 && !isAnimating")
                    page.locator("#voice-btn").click()
                    page.locator("#vn-input").fill("网页测试")
                    page.locator("#vn-send").click()
                    page.wait_for_function("!chatInFlight")
                    page.wait_for_function("document.querySelector('#vn-dialog').textContent.includes('尚未配置聊天服务')")
                    assert "尚未配置聊天服务" in page.locator("#vn-dialog").inner_text()

                    # A stopped timer must never append a delayed demonstration line.
                    page.evaluate("DEMO_SCRIPT[0].d = 250")
                    page.locator("#demo-btn").click()
                    page.locator("#demo-btn").click()
                    count = page.locator(".dialog-line").count()
                    page.wait_for_timeout(350)
                    assert page.locator(".dialog-line").count() == count
                    # Run every demonstration step to catch scope errors and stray hardware calls.
                    page.evaluate("DEMO_SCRIPT.forEach(step => {step.d = 20})")
                    page.locator("#demo-btn").click()
                    page.wait_for_function("!_demoRunning")
                    assert "预设演示结束" in page.locator("#vn-dialog").inner_text()
                    page.locator("#vn-clear").click()
                    page.wait_for_function("document.querySelector('#vn-dialog').children.length === 0")
                    page.screenshot(path=str(OUTPUT / "chat.png"))

                    page.locator('[data-target="2"]').click()
                    page.wait_for_function("currentPage === 2 && !isAnimating")
                    page.locator("#upload-avatar").set_input_files(str(upload))
                    page.wait_for_function("avatarIsCustom")
                    assert page.locator("#live2d-container").is_hidden()
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_function("!isAnimating && avatarIsCustom")
                    assert page.locator("#hero-img").is_visible()
                    page.locator('[data-target="2"]').click()
                    page.wait_for_function("currentPage === 2 && !isAnimating")
                    page.locator("#reset-avatar").click()
                    page.wait_for_function("!avatarIsCustom && _l2dModel !== null")
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_function("!isAnimating && _l2dModel !== null", timeout=25000)
                    assert page.evaluate("!avatarIsCustom && _l2dApp.ticker.started")
                    assert page.locator("#live2d-container").is_visible()
                    assert page.locator("#hero-img").is_hidden()
                    page.locator('[data-target="2"]').click()
                    page.wait_for_function("currentPage === 2 && !isAnimating")
                    page.screenshot(path=str(OUTPUT / "settings.png"))

                    mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                                                  device_scale_factor=1, has_touch=True)
                    mobile.route("https://**/*", lambda route: route.abort())
                    phone = mobile.new_page()
                    phone.on("pageerror", lambda error: errors.append(str(error)))
                    phone.goto(url, wait_until="domcontentloaded")
                    phone.wait_for_function("!isAnimating && _l2dModel !== null", timeout=25000)
                    phone.locator('[data-target="1"]').click()
                    phone.wait_for_function("currentPage === 1 && !isAnimating")
                    for selector in ("#vn-input", "#vn-send", "#vn-clear"):
                        rect = phone.locator(selector).bounding_box()
                        assert rect and rect["x"] >= 0 and rect["x"] + rect["width"] <= 391, (selector, rect)
                        assert phone.locator(selector).evaluate("""el => {
                            const rect = el.getBoundingClientRect();
                            const target = document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
                            return el === target || el.contains(target);
                        }"""), ("mobile control is covered", selector)
                    phone.locator("#vn-input").fill("手机输入测试")
                    phone.locator("#voice-btn").click()
                    phone.locator("#vn-send").click()
                    phone.wait_for_function("!chatInFlight")
                    phone.wait_for_function("document.querySelector('#vn-dialog').textContent.includes('尚未配置聊天服务')")
                    phone.screenshot(path=str(OUTPUT / "mobile.png"))

                    fallback = browser.new_page(viewport={"width": 1280, "height": 800})
                    fallback.on("pageerror", lambda error: errors.append(str(error)))
                    fallback.route("**/*.moc3", lambda route: route.abort())
                    fallback.route("https://**/*", lambda route: route.abort())
                    fallback.goto(url, wait_until="domcontentloaded")
                    fallback.wait_for_function("!isAnimating && !_l2dLoading", timeout=25000)
                    assert fallback.locator("#hero-img").is_visible()
                    assert fallback.locator("#hero-img").evaluate("el => el.complete && el.naturalWidth > 0")

                    # A pending configuration request must not block page entry or navigation.
                    pending_config = []
                    slow = browser.new_page(viewport={"width": 1280, "height": 800})
                    slow.on("pageerror", lambda error: errors.append(str(error)))
                    slow.route("https://**/*", lambda route: route.abort())
                    slow.route("**/config", lambda route: pending_config.append(route))
                    slow.goto(url, wait_until="domcontentloaded")
                    slow.wait_for_function("!isAnimating && _l2dModel !== null", timeout=10000)
                    assert pending_config
                    assert slow.locator("#preloader").is_hidden()
                    slow.locator('[data-target="1"]').click()
                    slow.wait_for_function("currentPage === 1 && !isAnimating")
                    pending_config.pop().fulfill(status=200, content_type="application/json",
                                                 body='{"public_mode":true,"chat_configured":false}')
                    slow.wait_for_function("document.querySelector('#connection-status').textContent.includes('在线聊天待配置')")

                    # Double-clicking the real HTML must load sibling assets and finish loading.
                    preview = browser.new_page(viewport={"width": 1280, "height": 800})
                    preview.on("pageerror", lambda error: errors.append(str(error)))
                    local_failures = []
                    preview.on("requestfailed", lambda request: local_failures.append(request.url)
                               if request.url.startswith("file:") else None)
                    preview.route("https://**/*", lambda route: route.abort())
                    preview.goto((ROOT / "index.html").as_uri(), wait_until="domcontentloaded")
                    preview.wait_for_function("!isAnimating", timeout=10000)
                    assert preview.locator("#preloader").is_hidden()
                    assert preview.locator("#file-preview-notice").is_visible()
                    assert preview.locator("#hero-img").evaluate("el => el.complete && el.naturalWidth > 0")
                    assert preview.evaluate("_l2dModel === null && socket === null")
                    preview.locator('[data-target="1"]').click()
                    preview.wait_for_function("currentPage === 1 && !isAnimating")
                    preview.locator("#vn-input").fill("文件预览测试")
                    preview.locator("#vn-send").click()
                    preview.wait_for_function("!chatInFlight")
                    preview.wait_for_function("document.querySelector('#vn-dialog').textContent.includes('run_web.bat')")
                    preview.locator("#vn-clear").click()
                    preview.wait_for_function("document.querySelector('#vn-dialog').children.length === 0")
                    preview.evaluate("DEMO_SCRIPT.forEach(step => {step.d = 20})")
                    preview.locator("#demo-btn").click()
                    preview.wait_for_function("!_demoRunning")
                    assert "预设演示结束" in preview.locator("#vn-dialog").inner_text()
                    assert not local_failures, local_failures
                    preview.screenshot(path=str(OUTPUT / "file-preview.png"))
                    assert not errors, errors
                    browser.close()
                print("PASS: offline assets, model fit, chat, demo stop/restart/full script, visitor controls,")
                print("      legacy portrait does not override Live2D, upload/reset/reload, mobile controls,")
                print("      model failure fallback, pending config, file preview/chat guidance/demo; 0 page errors.")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    main()
