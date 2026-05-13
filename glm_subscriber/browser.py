"""Browser connection module with CDP and persistent context support."""

import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import yaml
from loguru import logger
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright

_playwright: Optional[Playwright] = None
_browser = None  # Browser or BrowserContext depending on mode
_mode: str = "cdp"  # "cdp" or "persistent"


def _find_config_path() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        for p in [exe_dir / "config.yaml", Path("config.yaml")]:
            if p.exists():
                return p
    for p in [Path("config.yaml"), Path(__file__).parent.parent / "config.yaml"]:
        if p.exists():
            return p
    return Path("config.yaml")


def _load_config() -> dict:
    config_path = _find_config_path()
    if not config_path.exists():
        logger.warning(f"Config file not found at {config_path}, using defaults")
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_selectors() -> dict:
    """Get selectors from config."""
    config = _load_config()
    return config.get("selectors", {})


def _apply_browser_path(kwargs: dict) -> None:
    """Set executable_path for packaged exe where Playwright can't find its bundled Chromium."""
    # 1. Try user's Chrome installation
    chrome_paths = [
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in chrome_paths:
        if os.path.isfile(p):
            kwargs["executable_path"] = p
            logger.info(f"Using system Chrome: {p}")
            return

    # 2. Try Playwright's pre-installed Chromium
    pw_chromium = os.path.expandvars(
        r"%LOCALAPPDATA%\ms-playwright\chromium-1208\chrome-win64\chrome.exe"
    )
    if os.path.isfile(pw_chromium):
        kwargs["executable_path"] = pw_chromium
        logger.info(f"Using Playwright Chromium: {pw_chromium}")
        return

    logger.warning("No browser found. Install Chrome or run: playwright install chromium")


def connect_browser(cdp_port: int = 9222) -> Browser:
    """Connect to Chrome via CDP."""
    global _playwright, _browser, _mode
    _mode = "cdp"

    logger.info(f"Connecting to Chrome via CDP on port {cdp_port}...")
    try:
        _playwright = sync_playwright().start()
        cdp_url = f"http://127.0.0.1:{cdp_port}"
        _browser = _playwright.chromium.connect_over_cdp(cdp_url)
        logger.success(f"Connected to Chrome via CDP at {cdp_url}")
        return _browser
    except Exception as e:
        logger.error(f"Failed to connect to Chrome via CDP: {e}")
        raise


def _load_ycl_script() -> str:
    """Load the ycl.js helper script content."""
    ycl_path = Path(__file__).parent.parent / "js" / "ycl.js"
    if ycl_path.exists():
        return ycl_path.read_text(encoding="utf-8")
    logger.warning(f"ycl.js not found at {ycl_path}")
    return ""


def _inject_ycl_on_page(page: Page) -> None:
    """Evaluate ycl.js on an already-loaded page."""
    script = _load_ycl_script()
    if script:
        try:
            page.evaluate(script)
            logger.info("ycl.js injected on current page via evaluate")
        except Exception as e:
            logger.warning(f"Failed to evaluate ycl.js on page: {e}")


def launch_persistent_context(user_data_dir: str, headless: bool = False, url: str = "") -> BrowserContext:
    global _playwright, _browser, _mode
    _mode = "persistent"

    logger.info(f"Launching persistent browser context: {user_data_dir}")
    try:
        _playwright = sync_playwright().start()
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        launch_kwargs = dict(
            user_data_dir=user_data_dir,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        if getattr(sys, "frozen", False):
            _apply_browser_path(launch_kwargs)

        _browser = _playwright.chromium.launch_persistent_context(**launch_kwargs)
        ycl_script = _load_ycl_script()
        if ycl_script:
            _browser.add_init_script(ycl_script)
            logger.info("ycl.js added as init script on browser context")

        if _browser.pages:
            page = _browser.pages[0]
        else:
            page = _browser.new_page()
        _inject_stealth(page)
        _inject_ycl_on_page(page)

        if url and (not page.url or page.url == "about:blank"):
            logger.info(f"Navigating to {url}")
            page.goto(url, timeout=30000, wait_until="domcontentloaded")

        logger.success("Persistent browser context launched")
        return _browser
    except Exception as e:
        logger.error(f"Failed to launch persistent browser context: {e}")
        raise


def _inject_stealth(page: Page) -> None:
    """Inject stealth scripts into page to hide automation.

    Args:
        page: Playwright Page instance
    """
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
        logger.debug("Stealth scripts injected successfully")
    except ImportError:
        logger.warning("playwright-stealth not installed, skipping stealth injection")
    except Exception as e:
        logger.warning(f"Failed to inject stealth scripts: {e}")


def find_and_click(page: Page, selector: str, timeout: int = 5000) -> bool:
    """Find element and click it.

    Args:
        page: Playwright Page instance
        selector: CSS selector for the element
        timeout: Timeout in milliseconds (default: 5000)

    Returns:
        True if element was found and clicked, False otherwise
    """
    try:
        page.wait_for_selector(selector, timeout=timeout)
        page.click(selector)
        logger.debug(f"Clicked element: {selector}")
        return True
    except Exception as e:
        logger.warning(f"Failed to find or click element '{selector}': {e}")
        return False


def wait_for_element(page: Page, selector: str, timeout: int = 5000) -> bool:
    """Wait for element to appear on page.

    Args:
        page: Playwright Page instance
        selector: CSS selector for the element
        timeout: Timeout in milliseconds (default: 5000)

    Returns:
        True if element appeared, False if timeout
    """
    try:
        page.wait_for_selector(selector, timeout=timeout, state="attached")
        logger.debug(f"Element found: {selector}")
        return True
    except Exception as e:
        logger.warning(f"Element '{selector}' did not appear within {timeout}ms: {e}")
        return False


def get_page_url(page: Page) -> str:
    """Get current page URL.

    Args:
        page: Playwright Page instance

    Returns:
        Current page URL
    """
    return page.url


def take_screenshot(page: Page, path: str) -> bool:
    """Take page screenshot.

    Args:
        page: Playwright Page instance
        path: File path to save screenshot

    Returns:
        True if screenshot was taken successfully, False otherwise
    """
    try:
        # Ensure directory exists
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path)
        logger.debug(f"Screenshot saved to: {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to take screenshot: {e}")
        return False


def get_device_scale_factor(page: Page) -> float:
    """Get device scale factor for coordinate mapping.

    Args:
        page: Playwright Page instance

    Returns:
        Device scale factor (usually 1.0 or 2.0 for retina displays)
    """
    try:
        viewport = page.viewport_size
        if viewport:
            # Try to get from CDP session if available
            session = page.context._impl_obj._channel
            if session:
                try:
                    # Access the underlying session to get device scale factor
                    from playwright._impl._helper import get_device_scale_factor
                    return page.evaluate("() => window.devicePixelRatio")
                except Exception:
                    pass
        return 1.0
    except Exception:
        return 1.0


def get_mode() -> str:
    return _mode


def cleanup() -> None:
    """Clean up browser resources.

    CDP mode: disconnect without killing Chrome.
    Persistent mode: do nothing — keep Chromium open for user to complete payment.
    """
    global _playwright, _browser, _mode
    if _mode == "persistent":
        logger.info("Persistent mode: keeping browser open for payment")
        return
    if _mode == "cdp" and _browser:
        try:
            _browser.close()
        except Exception:
            pass
    _browser = None
    _playwright = None


# Convenience function to get first page from CDP connection
def get_first_page(browser) -> Optional[Page]:
    """Get the first meaningful page from browser.

    Works with both CDP (Browser with contexts) and persistent (BrowserContext with pages).
    """
    try:
        # Persistent context: browser IS the context, has .pages directly
        if _mode == "persistent":
            if browser.pages:
                page = browser.pages[0]
                _inject_stealth(page)
                _inject_ycl_on_page(page)
                logger.info(f"Using page: {page.url}")
                return page
            logger.warning("No pages found in persistent context")
            return None

        # CDP mode: browser has contexts which have pages
        if browser.contexts:
            context = browser.contexts[0]
            if context.pages:
                for page in context.pages:
                    url = page.url
                    if url and not url.startswith("chrome://") and not url.startswith("about:"):
                        _inject_stealth(page)
                        logger.info(f"Using page: {url}")
                        return page
                page = context.pages[0]
                _inject_stealth(page)
                return page
        logger.warning("No pages found in CDP browser context")
        return None
    except Exception as e:
        logger.error(f"Failed to get first page: {e}")
        return None


class BrowserClosedError(Exception):
    """Raised when the browser/page/context has been closed."""


def is_page_closed(page: Page) -> bool:
    """Check if the page or its browser context has been closed."""
    try:
        if page.is_closed():
            return True
        browser = page.context.browser
        if browser is None or not browser.is_connected():
            return True
        return False
    except Exception:
        return True


_PLAN_INDEX = {"Lite": 0, "Pro": 1, "Max": 2}
_BILLING_CYCLE_INDEX = {"monthly": 0, "quarterly": 1, "yearly": 2}
_BILLING_CYCLE_LABEL = {"monthly": "连续包月", "quarterly": "连续包季", "yearly": "连续包年"}


def select_billing_cycle(page: Page, cycle: str = "") -> bool:
    cycle = cycle or "quarterly"
    cycle_index = _BILLING_CYCLE_INDEX.get(cycle)
    if cycle_index is None:
        logger.warning(f"Unknown billing_cycle '{cycle}', valid: {list(_BILLING_CYCLE_INDEX.keys())}")
        return False

    try:
        tabs = page.locator(".switch-tab-item")
        active = page.locator(".switch-tab-item.active")
        if active.count() > 0:
            active_text = active.first.inner_text(timeout=3000)
            expected = _BILLING_CYCLE_LABEL[cycle]
            if expected in active_text:
                logger.info(f"Billing cycle already set to '{cycle}' ({expected})")
                return True

        tab = tabs.nth(cycle_index)
        tab.click(timeout=5000, force=True)
        logger.info(f"Selected billing cycle '{cycle}' (index={cycle_index})")
        time.sleep(0.5)
        return True
    except Exception as e:
        if "closed" in str(e).lower():
            raise BrowserClosedError(str(e)) from e
        logger.warning(f"Failed to select billing cycle '{cycle}': {e}")
        return False


def click_subscribe_button(page: Page, billing_cycle: str = "") -> bool:
    selectors = _get_selectors()
    plan = selectors.get("plan", "")
    cycle = billing_cycle or selectors.get("billing_cycle", "")
    plan_index = _PLAN_INDEX.get(plan)

    if cycle:
        select_billing_cycle(page, cycle)

    if plan_index is not None:
        try:
            card = page.locator(".package-card").nth(plan_index)
            btn = card.locator("button.buy-btn").first
            btn.click(timeout=5000, force=True)
            logger.debug(f"Clicked subscribe button [{plan}] (index={plan_index})")
            return True
        except Exception as e:
            if "closed" in str(e).lower():
                raise BrowserClosedError(str(e)) from e
            logger.warning(f"Failed to click subscribe button [{plan}]: {e}")
            return False

    selector = selectors.get("subscribe_button") or selectors.get("captcha_container")
    if not selector:
        logger.warning("No subscribe_button selector found in config")
        return False
    try:
        loc = page.locator(selector).first
        loc.click(timeout=5000, force=True)
        logger.debug(f"Clicked subscribe button (fallback): {selector}")
        return True
    except Exception as e:
        if "closed" in str(e).lower():
            raise BrowserClosedError(str(e)) from e
        logger.warning(f"Failed to click subscribe button: {e}")
        return False


def wait_for_captcha_popup(page: Page, timeout: int = 10000) -> bool:
    """Wait for CAPTCHA popup to appear.
    
    Checks Tencent CAPTCHA container (#tcaptcha_transform_dy) and 
    click-type wrap (.tencent-captcha-dy__click-type-wrap).

    Args:
        page: Playwright Page instance
        timeout: Timeout in milliseconds (default: 10000)

    Returns:
        True if CAPTCHA popup appeared, False if timeout
    """
    selectors = _get_selectors()
    
    # Try configured selector first
    captcha_sel = selectors.get("captcha_container", "")
    check_selectors = [captcha_sel] if captcha_sel else []
    check_selectors.extend([
        ".tencent-captcha-dy__click-type-wrap",
        "#tcaptcha_transform_dy",
    ])
    
    for sel in check_selectors:
        if not sel:
            continue
        try:
            page.wait_for_selector(sel, timeout=timeout, state="visible")
            logger.debug(f"CAPTCHA popup appeared: {sel}")
            return True
        except Exception as e:
            if "closed" in str(e).lower():
                raise BrowserClosedError(str(e)) from e
            continue
    
    logger.warning(f"CAPTCHA popup did not appear within {timeout}ms")
    return False


def click_confirm_button(page: Page) -> bool:
    """Click the confirm button after CAPTCHA characters are selected.
    
    Tries configured selector first, then Tencent CAPTCHA confirm button.

    Args:
        page: Playwright Page instance

    Returns:
        True if button was clicked successfully, False otherwise.
    """
    selectors = _get_selectors()
    confirm_selectors = []
    
    configured = selectors.get("confirm_button", "")
    if configured:
        confirm_selectors.append(configured)
    confirm_selectors.extend([
        ".tencent-captcha-dy__verify-confirm-btn",
    ])
    
    for selector in confirm_selectors:
        try:
            loc = page.locator(selector).first
            loc.click(timeout=3000, force=True)
            logger.debug(f"Clicked confirm button: {selector}")
            return True
        except Exception as e:
            logger.debug(f"Confirm selector {selector} failed: {e}")
            continue
    
    logger.warning("Failed to click any confirm button")
    return False


def is_captcha_passed(page: Page) -> bool:
    """Detect if CAPTCHA was passed.
    
    Checks multiple strategies:
    1. Tencent CAPTCHA mask layer disappeared
    2. Tencent CAPTCHA container is no longer visible
    3. Success icon appeared in CAPTCHA

    Args:
        page: Playwright Page instance

    Returns:
        True if CAPTCHA passed, False otherwise.
    """
    # Strategy 1: Tencent CAPTCHA mask layer hidden or gone
    try:
        mask = page.locator("#tCaptchaMaskLayer")
        if mask.count() > 0 and not mask.first.is_visible():
            logger.debug("CAPTCHA passed: mask layer hidden")
            return True
    except Exception:
        pass

    # Strategy 2: Tencent CAPTCHA container no longer visible
    try:
        container = page.locator("#tcaptcha_transform_dy")
        if container.count() > 0 and not container.first.is_visible():
            logger.debug("CAPTCHA passed: container hidden")
            return True
    except Exception:
        pass

    # Strategy 3: Look for success indicators in Tencent CAPTCHA
    try:
        # Tencent CAPTCHA shows a success state
        success_el = page.locator(".tencent-captcha-dy__verify-status-img--success")
        if success_el.count() > 0 and success_el.first.is_visible():
            logger.debug("CAPTCHA passed: success icon visible")
            return True
    except Exception:
        pass

    # Strategy 4: Generic success class patterns
    try:
        for selector in ["[class*='success']", "[class*='-success']"]:
            try:
                if page.is_visible(selector):
                    logger.debug(f"CAPTCHA passed: success element ({selector})")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def is_captcha_failed(page: Page) -> bool:
    """Detect if CAPTCHA failed.

    Checks for error messages:
    1. Elements with class containing "error", "fail", "wrong"
    2. Text containing "验证失败", "请重新", "错误"

    Args:
        page: Playwright Page instance

    Returns:
        True if CAPTCHA failed, False otherwise
    """
    try:
        # Strategy 1: Look for error class patterns
        error_selectors = [
            "[class*='error']",
            "[class*='fail']",
            "[class*='wrong']",
            "[class*='Error']",
            "[class*='Fail']",
            "[class*='Wrong']",
            "[id*='error']",
            "[id*='fail']",
        ]
        for selector in error_selectors:
            try:
                if page.is_visible(selector):
                    logger.debug(f"CAPTCHA failed: error element found ({selector})")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Strategy 2: Look for error text patterns
    try:
        error_texts = ["验证失败", "请重新", "错误", "incorrect", "wrong", "failed"]
        page_text = page.content()
        for text in error_texts:
            if text.lower() in page_text.lower():
                logger.debug(f"CAPTCHA failed: error text found ('{text}')")
                return True
    except Exception:
        pass

    return False


def click_at_viewport_position(page: Page, x: float, y: float) -> None:
    """Click at viewport coordinates with random offset and delay.

    Args:
        page: Playwright Page instance
        x: X coordinate in viewport
        y: Y coordinate in viewport
    """
    # Add random offset ±5px
    offset_x = random.uniform(-5, 5)
    offset_y = random.uniform(-5, 5)
    target_x = x + offset_x
    target_y = y + offset_y

    logger.debug(f"Clicking at viewport position ({target_x:.2f}, {target_y:.2f}) with ±5px offset")

    # Perform click
    page.mouse.click(target_x, target_y)

    # Add random delay between clicks (100-200ms)
    time.sleep(random.uniform(0.1, 0.2))
