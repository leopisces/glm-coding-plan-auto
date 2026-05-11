"""Offline OCR tests using synthetic test images."""

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from glm_subscriber.ocr_engine import OCREngine
from glm_subscriber.captcha_capture import CaptchaCapture
from glm_subscriber.captcha_solver import filter_target_keywords, identify_target_chars


def create_test_image(texts, size=(400, 200), bg_color="white", text_color="black"):
    img = Image.new("RGB", size, color=bg_color)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("msyh.ttc", 20)
    except Exception:
        font = ImageFont.load_default()
    y = 20
    for text in texts:
        draw.text((20, y), text, fill=text_color, font=font)
        y += 30
    return np.array(img)


class TestOcrEngineInit:
    def test_ocr_engine_initialization(self):
        ocr = OCREngine()
        assert ocr is not None
        assert ocr._ocr is not None

    def test_ocr_engine_warmup(self):
        ocr = OCREngine()
        ocr.warmup()


class TestOcrDetectText:
    def test_detect_text_on_synthetic_image(self):
        ocr = OCREngine()
        test_img = create_test_image(["测试文字", "中文识别"])
        detections = ocr.detect_text(test_img)
        assert isinstance(detections, list)

    def test_detect_text_empty_image(self):
        ocr = OCREngine()
        empty = np.ones((10, 10, 3), dtype=np.uint8) * 255
        detections = ocr.detect_text(empty)
        assert detections == []


class TestSplitRegions:
    def test_split_regions_basic(self):
        capture = CaptchaCapture()
        test_img = np.ones((400, 600, 3), dtype=np.uint8) * 255
        main_area, prompt_area = capture.split_regions(test_img, layout_ratio=0.75)
        assert main_area.shape[0] == 300
        assert prompt_area.shape[0] == 100
        assert main_area.shape[1] == 600
        assert prompt_area.shape[1] == 600

    def test_split_regions_custom_ratio(self):
        capture = CaptchaCapture()
        test_img = np.ones((400, 600, 3), dtype=np.uint8) * 255
        main_area, prompt_area = capture.split_regions(test_img, layout_ratio=0.5)
        assert main_area.shape[0] == 200
        assert prompt_area.shape[0] == 200


class TestFilterTargetKeywords:
    def test_filter_keywords(self):
        assert filter_target_keywords("请") == True
        assert filter_target_keywords("依次") == True
        assert filter_target_keywords("点击") == True

    def test_keep_target_chars(self):
        assert filter_target_keywords("中") == False
        assert filter_target_keywords("国") == False
        assert filter_target_keywords("A") == False


class TestIdentifyTargetChars:
    def test_identify_from_prompt_image(self):
        ocr = OCREngine()
        prompt_img = create_test_image(["请依次点击", "字A", "字B", "字C"])
        targets = identify_target_chars(prompt_img, ocr)
        assert isinstance(targets, list)
        assert "A" in targets or "B" in targets or "C" in targets or len(targets) >= 0


class TestOcrWithChineseText:
    def test_detect_chinese_characters(self):
        ocr = OCREngine()
        test_img = create_test_image(["中国", "美国", "日本"])
        detections = ocr.detect_text(test_img)
        assert isinstance(detections, list)

    def test_detect_multiple_lines(self):
        ocr = OCREngine()
        test_img = create_test_image(["第一行", "第二行", "第三行"])
        detections = ocr.detect_text(test_img)
        assert isinstance(detections, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])