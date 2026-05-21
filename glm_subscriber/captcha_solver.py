"""CAPTCHA target character identification logic with fuzzy matching.

Extracts target characters from CAPTCHA prompt area using DOM parsing.
Uses multi-round OCR with fuzzy character matching for robustness.
"""

from typing import List, Optional, Tuple
import re
import random
import time

import numpy as np
from loguru import logger
from playwright.sync_api import Page

from glm_subscriber.rapidocr_engine import RapidOCREngine as OCREngine
from glm_subscriber.types import CharDetection, ClickTarget, SolverResult
from glm_subscriber.captcha_capture import CaptchaCapture
from glm_subscriber.browser import click_at_viewport_position, is_captcha_passed, get_device_scale_factor


def refresh_captcha(page) -> None:
    """Click the refresh button on Tencent CAPTCHA to get a new challenge."""
    try:
        refresh_btn = page.locator(".tencent-captcha-dy__footer-icon--refresh")
        if refresh_btn.count() > 0:
            refresh_btn.first.click(timeout=2000)
            logger.info("Refreshed CAPTCHA challenge")
            time.sleep(1)
            return
    except Exception as e:
        logger.debug(f"Refresh button click failed: {e}")


# Keywords to filter out from OCR results
_FILTER_KEYWORDS = {
    "请", "依次", "点击", "顺序", "选择", "找到", "图中", "汉字", "以下",
    ":", "：", "、", "，", ",", ".", "。", "？", "?", "！", "!",
    "AI", "生成", "背景", "验证", "成功", "错误", "重试", "刷新", "确定",
    "加载", "失败",
}

_MULTI_CHAR_FILTER_PHRASES = {
    "AI生成背景", "验证成功", "验证错误", "请重试", "加载失败",
    "请点击刷新", "确定",
}


# --- Fuzzy character matching: visually similar chars OCR confuses at small size ---
_FUZZY_CHAR_GROUPS = [
    ("才", "材", "财"),
    ("仓", "苍", "沧", "舱"),
    ("丁", "订", "钉", "顶"),
    ("方", "放", "仿", "访"),
    ("青", "清", "请", "情", "晴"),
    ("马", "吗", "妈", "码", "骂"),
    ("巴", "吧", "把", "爸", "芭"),
    ("包", "抱", "饱", "胞", "苞"),
    ("白", "百", "伯", "泊", "柏"),
    ("扁", "编", "遍", "篇", "偏"),
    ("并", "饼", "拼", "瓶"),
    ("土", "士", "工"),
    ("未", "末"),
    ("日", "曰", "目"),
    ("己", "已", "巳"),
    ("干", "千", "于"),
    ("人", "入", "八"),
    ("天", "夫", "无"),
    ("午", "牛"),
    ("贝", "见"),
    ("王", "玉", "主"),
    ("力", "刀"),
    ("木", "本", "术"),
    ("太", "大", "犬"),
    ("辨", "辩", "瓣", "辫"),
    ("裁", "载", "栽"),
    ("拔", "拨"),
    ("折", "拆"),
    ("析", "折"),
    ("田", "由", "甲", "申"),
    ("测", "侧"),
    ("崇", "祟"),
    ("晴", "睛"),
    ("燥", "躁", "操", "澡"),
    ("待", "侍"),
    ("历", "厉"),
    ("风", "冈"),
    ("处", "外"),
    ("间", "问"),
    ("酒", "洒"),
    ("亨", "享"),
    ("免", "兔"),
    ("戌", "戍", "戊"),
    ("哀", "衷", "衰"),
    ("贷", "货"),
    ("栗", "粟"),
]

_FUZZY_LOOKUP: dict = {}
for _group in _FUZZY_CHAR_GROUPS:
    for _ch in _group:
        if _ch not in _FUZZY_LOOKUP:
            _FUZZY_LOOKUP[_ch] = set()
        _FUZZY_LOOKUP[_ch].update(g for g in _group if g != _ch)


def _get_fuzzy_variants(char: str) -> List[str]:
    if len(char) != 1:
        return []
    return list(_FUZZY_LOOKUP.get(char, []))


def filter_target_keywords(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return True
    if len(cleaned) == 1 and cleaned in _FILTER_KEYWORDS:
        return True
    if cleaned in _FILTER_KEYWORDS:
        return True
    if all(ch in _FILTER_KEYWORDS for ch in cleaned):
        return True
    return False


def _is_click_text_captcha(page: Page) -> bool:
    try:
        click_wrap = page.locator(".tencent-captcha-dy__click-type-wrap")
        if click_wrap.count() > 0:
            return True
        for sel in [".tencent-captcha-dy__header-text", ".tencent-captcha-dy__header-title-wrap"]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    text = loc.first.text_content(timeout=2000)
                    if text and "请依次点击" in text:
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _is_sliding_captcha(page: Page) -> bool:
    """Detect if current CAPTCHA is sliding puzzle type (not click-text).
    
    Sliding CAPTCHA always has a slider block element.
    Do NOT use container-visible-without-click-text as a fallback —
    the click-text DOM may not have rendered yet, causing false positives
    that skip every CAPTCHA and create an infinite loop.
    """
    try:
        slider = page.locator(".tencent-captcha-dy__slider-block")
        if slider.count() > 0 and slider.first.is_visible():
            return True
    except Exception:
        pass
    return False


def extract_targets_from_dom(page: Page) -> List[str]:
    if not _is_click_text_captcha(page):
        logger.info("Current CAPTCHA is not click-text type, skipping")
        return []

    selectors = [
        ".tencent-captcha-dy__header-text",
        ".tencent-captcha-dy__header-title-wrap",
        ".tencent-captcha-dy__header-wrap",
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() == 0:
                continue
            text = loc.first.text_content(timeout=2000)
            if not text:
                continue
            text = text.strip()
            logger.debug(f"Found DOM text: {text}")

            m = re.search(r'请依次点击\s*[:：]?\s*(.+)', text)
            if m:
                target_part = m.group(1).strip()
                chars = re.split(r'[\s,，、]+', target_part)
                targets = [c.strip() for c in chars if c.strip() and len(c.strip()) == 1]
                if targets:
                    logger.info(f"Extracted targets from DOM via regex: {targets}")
                    return targets
        except Exception as e:
            logger.debug(f"Selector {selector} error: {e}")
            continue

    logger.debug("No target characters found in DOM")
    return []


def detect_char_positions(
    main_image: np.ndarray, ocr: OCREngine, target_chars: List[str] = None
) -> List[CharDetection]:
    logger.debug(f"Detecting character positions in main image (shape: {main_image.shape})")
    raw_detections = ocr.detect_text(main_image, target_chars=target_chars)

    filtered = []
    for det in raw_detections:
        if det.text in _MULTI_CHAR_FILTER_PHRASES:
            logger.debug(f"Filtered out non-target phrase: '{det.text}'")
            continue
        if len(det.text) == 1 and det.text in _FILTER_KEYWORDS:
            logger.debug(f"Filtered out keyword char: '{det.text}'")
            continue
        if det.confidence < 0.5:
            logger.debug(f"Filtered out low confidence: '{det.text}' conf={det.confidence:.2f}")
            continue
        filtered.append(det)

    logger.debug(f"Detected {len(raw_detections)} raw, {len(filtered)} after filtering")
    return filtered


def match_target_chars(
    targets: List[str], detections: List[CharDetection], fuzzy: bool = True
) -> List[ClickTarget]:
    logger.debug(f"Matching targets {targets} to {len(detections)} detections")
    click_targets = []

    detection_lookup: dict = {}
    for det in detections:
        texts = [det.text]
        if fuzzy:
            texts.extend(_get_fuzzy_variants(det.text))
        for t in texts:
            if t not in detection_lookup:
                detection_lookup[t] = []
            detection_lookup[t].append(det)

    used_indices: set = set()  # Track detections already assigned

    for i, target_char in enumerate(targets):
        search_texts = [target_char]
        if fuzzy:
            search_texts.extend(_get_fuzzy_variants(target_char))

        matching_detections = []
        for st in search_texts:
            if st in detection_lookup:
                for det in detection_lookup[st]:
                    det_idx = id(det)  # Unique ID for each detection object
                    if det_idx not in used_indices:
                        matching_detections.append(det)

        if not matching_detections:
            logger.warning(f"Target '{target_char}' (index {i}) not found")
            click_target = ClickTarget(char=target_char, index=i, detection=None, position=None)
            click_targets.append(click_target)
            continue

        best_detection = max(matching_detections, key=lambda d: d.confidence)
        used_indices.add(id(best_detection))  # Mark as used

        if best_detection.text != target_char:
            logger.info(f"Fuzzy match: target '{target_char}' -> OCR '{best_detection.text}' (conf={best_detection.confidence:.3f})")

        click_target = ClickTarget(char=target_char, index=i, detection=best_detection, position=None)
        click_targets.append(click_target)

    logger.info(f"Matched {sum(1 for ct in click_targets if ct.detection is not None)}/{len(targets)} targets")
    return click_targets


def map_to_viewport_coords(
    click_targets: List[ClickTarget],
    element_bbox,
    region_offset: Tuple[float, float],
    scale_factor: float = 1.0
) -> List[ClickTarget]:
    if not click_targets:
        return click_targets

    if isinstance(element_bbox, dict):
        element_x = element_bbox.get('x', 0)
        element_y = element_bbox.get('y', 0)
    else:
        element_x, element_y, _, _ = element_bbox

    offset_x, offset_y = region_offset

    for click_target in click_targets:
        if click_target.detection is None:
            continue

        img_x = click_target.detection.center_x + offset_x
        img_y = click_target.detection.center_y + offset_y
        css_x = img_x / scale_factor
        css_y = img_y / scale_factor
        viewport_x = element_x + css_x
        viewport_y = element_y + css_y

        click_target.position = (int(viewport_x), int(viewport_y))

    return click_targets


class CaptchaSolver:

    def __init__(self, ocr_engine: OCREngine, captcha_capture: CaptchaCapture, config: dict = None):
        self.ocr_engine = ocr_engine
        self.captcha_capture = captcha_capture
        self.config = config or {}
        self.confidence_threshold = self.config.get("confidence", {}).get("solver_threshold", 0.6)
        logger.debug(f"CaptchaSolver initialized with confidence threshold {self.confidence_threshold}")

    def solve(self, page) -> SolverResult:
        logger.info("Starting CAPTCHA solving flow")
        start_time = time.time()
        max_ocr_rounds = 3
        image_area_selector = self.config.get("selectors", {}).get("image_area", ".tencent-captcha-dy__image-area")

        for ocr_round in range(max_ocr_rounds):
            logger.debug(f"OCR round {ocr_round + 1}/{max_ocr_rounds}")

            # Wait for image area first — otherwise the DOM header may still
            # contain stale target text from a previous CAPTCHA cycle.
            try:
                page.wait_for_selector(image_area_selector, timeout=5000, state="visible")
                time.sleep(0.5)
            except Exception as e:
                if ocr_round < max_ocr_rounds - 1:
                    logger.warning(f"Image area not visible on round {ocr_round + 1}, refreshing...")
                    refresh_captcha(page)
                    time.sleep(2)
                    continue
                return SolverResult(success=False, target_text="", clicked_positions=[],
                                    detections=[], targets_found=0, targets_requested=0,
                                    error=f"image_area_not_visible: {str(e)}", retry_allowed=True)

            # Step 1: Extract target characters from DOM
            target_chars = extract_targets_from_dom(page)
            if not target_chars:
                logger.info("No click-text targets, refreshing...")
                refresh_captcha(page)
                time.sleep(2)
                target_chars = extract_targets_from_dom(page)
                if not target_chars:
                    if ocr_round < max_ocr_rounds - 1:
                        refresh_captcha(page)
                        time.sleep(2)
                        continue
                    return SolverResult(success=False, target_text="", clicked_positions=[],
                                        detections=[], targets_found=0, targets_requested=0,
                                        error="no_click_text_captcha", retry_allowed=True)

            logger.info(f"Target characters: {target_chars}")

            # Step 2: Screenshot image area
            try:
                captcha_image = self.captcha_capture.capture_element(page, image_area_selector)
            except Exception as e:
                logger.error(f"Failed to capture image area: {e}")
                return SolverResult(success=False, target_text="".join(target_chars),
                                    clicked_positions=[], detections=[], targets_found=0,
                                    targets_requested=len(target_chars),
                                    error=f"capture_failed: {str(e)}", retry_allowed=True)

            if self.config.get("debug"):
                self.captcha_capture.save_debug_image(captcha_image, f"captcha_ocr_round_{ocr_round}")

            # Step 3: OCR
            detections = detect_char_positions(captcha_image, self.ocr_engine, target_chars=target_chars)
            logger.info(f"Detected {len(detections)} characters in image area")

            # Step 4: Match targets with fuzzy matching
            click_targets = match_target_chars(target_chars, detections, fuzzy=True)
            matched_targets = [ct for ct in click_targets if ct.detection is not None]
            unmatched_targets = [ct for ct in click_targets if ct.detection is None]

            all_matched = len(matched_targets) == len(target_chars)
            overall_confidence = (sum(ct.detection.confidence for ct in matched_targets) / len(matched_targets)) if matched_targets else 0.0

            logger.info(f"Matched {len(matched_targets)}/{len(target_chars)} targets, confidence={overall_confidence:.3f}")

            if not all_matched:
                logger.warning(f"Only matched {len(matched_targets)}/{len(target_chars)}, missing: {[ct.char for ct in unmatched_targets]}")
                if ocr_round < max_ocr_rounds - 1:
                    logger.info("Refreshing CAPTCHA...")
                    refresh_captcha(page)
                    time.sleep(2)
                    continue
                else:
                    return SolverResult(success=False, target_text="".join(target_chars),
                                        clicked_positions=[], detections=detections,
                                        targets_found=len(matched_targets), targets_requested=len(target_chars),
                                        error="incomplete_match", retry_allowed=True)

            if overall_confidence < self.confidence_threshold:
                logger.warning(f"Low confidence {overall_confidence:.3f}")
                if ocr_round < max_ocr_rounds - 1:
                    logger.info("Low confidence, refreshing...")
                    refresh_captcha(page)
                    time.sleep(2)
                    continue
                else:
                    return SolverResult(success=False, target_text="".join(target_chars),
                                        clicked_positions=[], detections=detections,
                                        targets_found=len(matched_targets), targets_requested=len(target_chars),
                                        error="low_confidence", retry_allowed=True)

            break
        else:
            return SolverResult(success=False, target_text="".join(target_chars) if target_chars else "",
                                clicked_positions=[], detections=[],
                                targets_found=0, targets_requested=len(target_chars) if target_chars else 0,
                                error="all_ocr_rounds_failed", retry_allowed=True)

        # Step 5: Map coordinates
        try:
            image_area_bbox = self.captcha_capture.get_element_bbox(page, image_area_selector)
            if image_area_bbox is None:
                return SolverResult(success=False, target_text="".join(target_chars),
                                    clicked_positions=[], detections=detections,
                                    targets_found=len(matched_targets), targets_requested=len(target_chars),
                                    error="element_bbox_not_found", retry_allowed=True)
            scale_factor = get_device_scale_factor(page)
            click_targets = map_to_viewport_coords(click_targets, image_area_bbox, (0, 0), scale_factor)
        except Exception as e:
            return SolverResult(success=False, target_text="".join(target_chars),
                                clicked_positions=[], detections=detections,
                                targets_found=len(matched_targets), targets_requested=len(target_chars),
                                error=f"coordinate_mapping_failed: {str(e)}", retry_allowed=True)

        # Step 6: Click targets
        clicked_positions = []
        for click_target in click_targets:
            if click_target.detection is None or click_target.position is None:
                logger.warning(f"Target '{click_target.char}' not found, skipping")
                continue
            x, y = click_target.position
            logger.info(f"Clicking target '{click_target.char}' at ({x:.1f}, {y:.1f})")
            try:
                click_at_viewport_position(page, x, y)
                clicked_positions.append((int(x), int(y)))
            except Exception as e:
                logger.error(f"Failed to click at ({x}, {y}): {e}")

        # Step 7: Confirm
        try:
            from glm_subscriber.browser import click_confirm_button
            click_confirm_button(page)
        except Exception as e:
            logger.warning(f"Failed to click confirm button: {e}")

        # Step 8: Check
        time.sleep(1)
        passed = is_captcha_passed(page)
        elapsed = time.time() - start_time
        logger.info(f"CAPTCHA solving completed in {elapsed:.2f}s, passed={passed}")

        return SolverResult(
            success=passed,
            target_text="".join(target_chars),
            clicked_positions=clicked_positions,
            detections=detections,
            targets_found=len(matched_targets),
            targets_requested=len(target_chars),
            error=None if passed else "captcha_not_passed",
            retry_allowed=not passed
        )


if __name__ == "__main__":
    print("CAPTCHA solver module loaded.")
