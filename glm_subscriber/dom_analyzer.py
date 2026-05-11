"""DOM Analyzer for GLM CAPTCHA - Analyzes CAPTCHA structure and collects selectors.

This module connects to Chrome via CDP and inspects the CAPTCHA popup DOM
to identify selectors and layout structure for automation.

Usage:
    python -m glm_subscriber.dom_analyzer --cdp-port 9222
    python -m glm_subscriber.dom_analyzer --cdp-port 9222 --url "https://chatglm.cn"
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from glm_subscriber.browser import connect_browser, get_first_page, take_screenshot

SHUMEI_PATTERNS = [
    ".sm-captcha",
    "#sm-captcha",
    "[class*='sm-captcha']",
    "[class*='shumei']",
    "iframe[src*='ishumei']",
    "iframe[src*='shumei']",
]

GEETEST_PATTERNS = [
    ".geetest_panel",
    ".geetest_popup",
    "[class*='geetest']",
    "iframe[src*='geetest']",
]

GENERIC_CAPTCHA_PATTERNS = [
    "[class*='captcha']",
    "[class*='verify']",
    "[class*='challenge']",
    "img[src*='captcha']",
    "[class*='popup']",
]


class DomAnalyzer:

    def __init__(self, cdp_port: int = 9222):
        self.cdp_port = cdp_port
        self.browser = None
        self.page = None
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "captcha_provider": None,
            "selectors": {},
            "elements": [],
            "screenshots": [],
            "layout_ratios": {},
        }

    def connect(self) -> bool:
        try:
            self.browser = connect_browser(self.cdp_port)
            self.page = get_first_page(self.browser)
            if self.page:
                logger.success("Connected to browser")
                return True
            logger.error("No pages found in browser")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to browser: {e}")
            return False

    def find_captcha_elements(self) -> List[Dict]:
        elements = []

        # Try ShuMei patterns
        logger.info("Searching for ShuMei CAPTCHA patterns...")
        for pattern in SHUMEI_PATTERNS:
            found = self._find_elements_by_selector(pattern, "shumei")
            elements.extend(found)

        # Try GeeTest patterns
        logger.info("Searching for GeeTest CAPTCHA patterns...")
        for pattern in GEETEST_PATTERNS:
            found = self._find_elements_by_selector(pattern, "geetest")
            elements.extend(found)

        # Try generic patterns
        logger.info("Searching for generic CAPTCHA patterns...")
        for pattern in GENERIC_CAPTCHA_PATTERNS:
            found = self._find_elements_by_selector(pattern, "generic")
            elements.extend(found)

        return elements

    def _find_elements_by_selector(self, selector: str, provider: str) -> List[Dict]:
        found = []
        try:
            if self.page:
                elements = self.page.query_selector_all(selector)
                for el in elements:
                    info = self._extract_element_info(el, selector, provider)
                    if info:
                        found.append(info)

            # Also try frames/iframes
            frames = self.page.frames if self.page else []
            for frame in frames:
                try:
                    frame_elements = frame.query_selector_all(selector)
                    for el in frame_elements:
                        info = self._extract_element_info(el, selector, provider, frame_url=frame.url)
                        if info:
                            found.append(info)
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Selector '{selector}' found no elements: {e}")

        return found

    def _extract_element_info(self, element, selector: str, provider: str, frame_url: str = None) -> Optional[Dict]:
        try:
            bbox = element.bounding_box()
            if not bbox:
                return None

            tag_name = element.evaluate("el => el.tagName")
            class_name = element.evaluate("el => el.className") or ""
            id_attr = element.evaluate("el => el.id") or ""
            src_attr = element.evaluate("el => el.src") if tag_name.lower() == "img" else None

            return {
                "selector": selector,
                "provider": provider,
                "tag": tag_name.lower(),
                "class": class_name,
                "id": id_attr,
                "src": src_attr,
                "frame_url": frame_url,
                "bbox": {
                    "x": bbox.x,
                    "y": bbox.y,
                    "width": bbox.width,
                    "height": bbox.height,
                },
            }
        except Exception as e:
            logger.debug(f"Failed to extract element info: {e}")
            return None

    def identify_captcha_provider(self, elements: List[Dict]) -> str:
        providers = [el.get("provider") for el in elements]

        if "shumei" in providers:
            return "shumei"
        elif "geetest" in providers:
            return "geetest"
        else:
            return "unknown"

    def calculate_layout_ratios(self, elements: List[Dict]) -> Dict:
        ratios = {}
        images = [el for el in elements if el.get("tag") == "img"]
        containers = [el for el in elements if el.get("tag") in ("div", "span", "section")]

        if images and containers:
            img = images[0]
            img_height = img["bbox"]["height"]

            for container in containers:
                cont_height = container["bbox"]["height"]
                if cont_height > img_height and img_height > 0:
                    ratio = img_height / cont_height
                    ratios["image_to_container"] = round(ratio, 3)
                    ratios["container_height"] = cont_height
                    ratios["image_height"] = img_height
                    break

        return ratios

    def take_captcha_screenshots(self, elements: List[Dict], output_dir: Path) -> List[str]:
        screenshots = []

        for i, el in enumerate(elements):
            if el.get("bbox"):
                bbox = el["bbox"]
                try:
                    filename = f"captcha_element_{i}_{el.get('provider', 'unknown')}_{el.get('tag', 'el')}.png"
                    filepath = output_dir / filename

                    if self.page:
                        self.page.screenshot(
                            path=str(filepath),
                            clip={
                                "x": bbox["x"],
                                "y": bbox["y"],
                                "width": bbox["width"],
                                "height": bbox["height"],
                            },
                        )
                        screenshots.append(str(filepath))
                        logger.info(f"Saved screenshot: {filepath}")
                except Exception as e:
                    logger.warning(f"Failed to screenshot element {i}: {e}")

        try:
            if self.page:
                full_path = output_dir / "captcha_full_page.png"
                self.page.screenshot(path=str(full_path))
                screenshots.append(str(full_path))
                logger.info(f"Saved full page screenshot: {full_path}")
        except Exception as e:
            logger.warning(f"Failed to take full page screenshot: {e}")

        return screenshots

    def generate_analysis_document(self, output_path: Path) -> None:
        doc = f"""# GLM Subscription CAPTCHA DOM Analysis

> Analysis generated at: {self.results['timestamp']}
> Run `python -m glm_subscriber.dom_analyzer --cdp-port {self.cdp_port}` to update.

---

## 验证码服务商 (CAPTCHA Provider)

**Provider:** {self.results.get('captcha_provider', 'Unknown')}

**Identification:**
"""

        provider = self.results.get("captcha_provider", "unknown")
        if provider == "shumei":
            doc += """- Detected: 数美 (ShuMei) CAPTCHA
- Look for classes containing: `sm-captcha`, `shumei`
- iframe domains: `*.ishumei.com`
"""
        elif provider == "geetest":
            doc += """- Detected: 极验 (GeeTest) CAPTCHA
- Look for classes containing: `geetest`, `geetest_panel`
- iframe domains: `*.geetest.com`
"""
        else:
            doc += """- CAPTCHA provider could not be identified
- Please inspect elements manually
"""

        doc += """
---

## Found CAPTCHA Elements

| Tag | Class | Selector | Provider | BBox (x,y,w,h) |
|-----|-------|----------|----------|-----------------|
"""
        for el in self.results.get("elements", []):
            bbox = el.get("bbox", {})
            bbox_str = f"{bbox.get('x', 0):.0f},{bbox.get('y', 0):.0f},{bbox.get('width', 0):.0f},{bbox.get('height', 0):.0f}"
            doc += f"| {el.get('tag', '')} | {el.get('class', '')[:50]} | {el.get('selector', '')} | {el.get('provider', '')} | {bbox_str} |\n"

        doc += """
---

## 图片布局比例 (Image Layout Ratio)

"""
        ratios = self.results.get("layout_ratios", {})
        if ratios:
            doc += f"""- **Image to Container Ratio:** {ratios.get('image_to_container', 'N/A')}
- **Container Height:** {ratios.get('container_height', 'N/A')}px
- **Image Height:** {ratios.get('image_height', 'N/A')}px
"""
        else:
            doc += "- Layout ratios could not be calculated\n"

        doc += """
---

## Captured Screenshots

"""
        for shot in self.results.get("screenshots", []):
            doc += f"- `{Path(shot).name}`\n"

        doc += """
---

## Recommended Selectors (TBD - verify manually)

### For 数美 (ShuMei):
"""
        if provider == "shumei":
            for el in self.results.get("elements", []):
                if el.get("provider") == "shumei":
                    doc += f"- `{el.get('selector')}` ({el.get('class', '')[:40]})\n"
        else:
            doc += """- `.sm-captcha`
- `#sm-captcha`
- `[class*="sm-captcha"]`
- `[class*="shumei"]`
"""

        doc += """
### For 极验 (GeeTest):
"""
        if provider == "geetest":
            for el in self.results.get("elements", []):
                if el.get("provider") == "geetest":
                    doc += f"- `{el.get('selector')}` ({el.get('class', '')[:40]})\n"
        else:
            doc += """- `.geetest_panel`
- `.geetest_popup`
- `[class*="geetest"]`
"""

        doc += """
### Generic Fallback:
```css
[class*="captcha"]
[class*="verify"]
[class*="challenge"]
```

---

## Notes

- CAPTCHA is a **click-text type** ("请依次点击") where target text is embedded in the image
- The image text must be identified via OCR, not DOM
- The target sequence (e.g., "A B C") should be displayed somewhere in the DOM - OCR that too
- CAPTCHA iframe content may require `frame_locator()` in Playwright
"""

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(doc)

        logger.success(f"Analysis document written to: {output_path}")

    def analyze(self, url: str = None, output_path: Path = None) -> bool:
        if not self.connect():
            logger.warning("Browser not available - generating placeholder document")
            self._generate_placeholder(output_path)
            return False

        try:
            if url:
                logger.info(f"Navigating to: {url}")
                self.page.goto(url)
                time.sleep(2)

            logger.info("Searching for CAPTCHA elements...")
            elements = self.find_captcha_elements()
            self.results["elements"] = elements

            if elements:
                logger.success(f"Found {len(elements)} CAPTCHA-related elements")
            else:
                logger.warning("No CAPTCHA elements found on page")

            self.results["captcha_provider"] = self.identify_captcha_provider(elements)
            logger.info(f"Identified CAPTCHA provider: {self.results['captcha_provider']}")

            self.results["layout_ratios"] = self.calculate_layout_ratios(elements)

            if output_path:
                screenshot_dir = output_path.parent / "screenshots"
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                self.results["screenshots"] = self.take_captcha_screenshots(elements, screenshot_dir)

            if output_path:
                self.generate_analysis_document(output_path)

            return True

        except Exception as e:
            logger.error(f"Analysis failed: {e}")
            self._generate_placeholder(output_path)
            return False

        finally:
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass

    def _generate_placeholder(self, output_path: Path = None) -> None:
        self.results["captcha_provider"] = "unknown (browser unavailable)"
        self.results["elements"] = []
        self.results["layout_ratios"] = {}

        if output_path:
            self.generate_analysis_document(output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze CAPTCHA DOM structure on GLM subscription page"
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )

    analyzer = DomAnalyzer(cdp_port=args.cdp_port)
    output_path = Path(args.output)

    success = analyzer.analyze(url=args.url, output_path=output_path)

    if success:
        logger.info("Analysis complete!")
        sys.exit(0)
    else:
        logger.warning("Analysis completed with warnings (see document for details)")
        sys.exit(1)


if __name__ == "__main__":
    main()
