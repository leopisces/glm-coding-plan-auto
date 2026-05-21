"""RapidOCR engine wrapper for Chinese character detection and recognition.

Uses PaddleOCR models converted to ONNX format via ONNX Runtime.
Much faster inference than PaddleOCR native (~2-6x speedup) with the same accuracy.
Supports return_single_char_box for individual character bounding boxes.
"""

import time
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from loguru import logger
from rapidocr_onnxruntime import RapidOCR

from glm_subscriber.types import BoundingBox, CharDetection


class RapidOCREngine:
    """OCR engine using RapidOCR (PaddleOCR ONNX Runtime).

    Key advantages over PaddleOCR native:
    - 2-6x faster inference (ONNX Runtime vs PaddlePaddle)
    - Native single-character bounding box support (return_single_char_box)
    - No need for multi-char splitting logic
    - Lightweight: only onnxruntime dependency
    """

    def __init__(self, config: dict = None):
        config = config or {}
        logger.info("Initializing RapidOCR engine (ONNX Runtime)")

        params = {}

        # Use PP-OCRv4 server models for best accuracy on CAPTCHA
        # RapidOCR supports: PP-OCRv4, PP-OCRv5
        ocr_version = config.get("ocr_version", "v4")
        model_type = config.get("model_type", "server")  # server > mobile for accuracy

        if ocr_version == "v5":
            from rapidocr_onnxruntime import OCRVersion
            params["Det.ocr_version"] = OCRVersion.PPOCRV5
            params["Rec.ocr_version"] = OCRVersion.PPOCRV5
            params["Cls.ocr_version"] = OCRVersion.PPOCRV5

        # Detection thresholds (lower = more sensitive, catches faint chars)
        params["Det.box_thresh"] = config.get("text_det_box_thresh", 0.3)
        params["Det.text_score"] = config.get("text_det_thresh", 0.3)
        params["Det.unclip_ratio"] = config.get("text_det_unclip_ratio", 1.8)

        # Recognition threshold
        params["Rec.text_score"] = config.get("text_rec_score_thresh", 0.3)

        self._ocr = RapidOCR(params=params)
        self._config = config
        logger.success("RapidOCR engine initialized")

    def warmup(self) -> None:
        blank = np.ones((100, 100, 3), dtype=np.uint8) * 255
        start = time.perf_counter()
        self.detect_text(blank)
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(f"Warmup completed in {elapsed:.1f}ms")

    def detect_text(
        self, image: np.ndarray, target_chars: list = None
    ) -> List[CharDetection]:
        """Detect text in image using RapidOCR with multi-strategy + retry approach.

        Phase 1: 2x upscale (most reliable, ~500ms)
        Phase 2: original image (~800ms, complementary)
        Phase 3 (retry): 2x upscale with lowered thresholds (catches faint/misread chars)

        Early-exits when all target_chars are found.
        Saves debug JSON when in debug mode.

        Args:
            image: Input image (BGR numpy array)
            target_chars: Target characters for early-exit optimization

        Returns:
            List of CharDetection objects (one per character)
        """
        if image is None or image.size == 0:
            logger.warning("Empty image provided to detect_text")
            return []

        all_raw: List[CharDetection] = []
        h, w = image.shape[:2]
        img2x = cv2.resize(image, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)

        # --- Phase 1: 2x upscale (most reliable) ---
        dets_2x = self._run_ocr(img2x, scale_x=w / (w * 2), scale_y=h / (h * 2))
        logger.info(f"RapidOCR '2x_upscale': {len(dets_2x)} chars")
        all_raw.extend(dets_2x)

        if target_chars and self._all_found(all_raw, target_chars):
            logger.info(f"All {len(target_chars)} targets found after '2x_upscale', skipping remaining")
            return all_raw

        # --- Phase 2: original image ---
        dets_orig = self._run_ocr(image)
        logger.info(f"RapidOCR 'original': {len(dets_orig)} chars")
        all_raw.extend(dets_orig)

        if target_chars and self._all_found(all_raw, target_chars):
            logger.info(f"All {len(target_chars)} targets found after 'original', skipping remaining")
            return all_raw

        # --- Phase 3: 2x upscale with lowered thresholds (retry) ---
        # When targets still not found, re-run with lower detection/recognition
        # thresholds to catch characters that are faint or misrecognized.
        logger.info("Targets not fully matched, retrying with lower thresholds...")
        dets_retry = self._run_ocr(
            img2x,
            scale_x=w / (w * 2), scale_y=h / (h * 2),
            text_score=0.15, box_thresh=0.15, unclip_ratio=2.2,
        )
        logger.info(f"RapidOCR '2x_retry_low_thresh': {len(dets_retry)} chars")
        all_raw.extend(dets_retry)

        # Save debug info if configured
        if self._config.get("debug"):
            self._save_ocr_debug(image, all_raw, target_chars)

        merged = self._merge_detections(all_raw, iou_threshold=0.3)
        logger.info(f"RapidOCR: {len(all_raw)} raw -> {len(merged)} after merge")
        return merged

    @staticmethod
    def _all_found(detections: list, targets: list) -> bool:
        """Check if all target characters are in the detection set."""
        found = {d.text for d in detections}
        return all(ch in found for ch in targets)

    def _run_ocr(
        self, image: np.ndarray, scale_x: float = 1.0, scale_y: float = 1.0,
        text_score: float = None, box_thresh: float = None, unclip_ratio: float = None,
    ) -> List[CharDetection]:
        """Run RapidOCR on an image and return individual character detections.

        Args:
            image: Input image (BGR numpy array)
            scale_x: X scale factor to map coords back to original image
            scale_y: Y scale factor to map coords back to original image
            text_score: Override recognition confidence threshold
            box_thresh: Override detection box threshold
            unclip_ratio: Override detection unclip ratio

        Returns:
            List of CharDetection objects
        """
        kwargs = dict(return_word_box=True, return_single_char_box=True)
        if text_score is not None:
            kwargs["text_score"] = text_score
        if box_thresh is not None:
            kwargs["box_thresh"] = box_thresh
        if unclip_ratio is not None:
            kwargs["unclip_ratio"] = unclip_ratio

        result = self._ocr(image, **kwargs)

        # rapidocr_onnxruntime returns (result_list, elapse_list)
        # result_list: [[box, text, score, word_boxes], ...] or None
        # word_boxes: [[char_box, ...], ...] when return_single_char_box=True
        if not result or not result[0]:
            return []

        result_list = result[0]
        detections = []

        for item in result_list:
            box = item[0]   # [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            text = item[1]  # recognized text string
            score = item[2] # confidence float

            text = text.strip()
            if not text:
                continue

            # Check for single-character boxes (item[3] if present)
            word_boxes = item[3] if len(item) > 3 else None

            if word_boxes and len(word_boxes) > 0 and text:
                # Use individual character bounding boxes
                # word_boxes format: [[char_box], [char_box], ...]
                # where char_box = [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
                for ci, char_box in enumerate(word_boxes):
                    char_text = text[ci] if ci < len(text) else ""
                    if not char_text.strip():
                        continue

                    xs = [p[0] * scale_x for p in char_box]
                    ys = [p[1] * scale_y for p in char_box]

                    bbox = BoundingBox(
                        x1=int(min(xs)),
                        y1=int(min(ys)),
                        x2=int(max(xs)),
                        y2=int(max(ys)),
                    )
                    center = (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)))

                    det = CharDetection(
                        text=char_text,
                        confidence=float(score),
                        bbox=bbox,
                        center=center,
                    )
                    detections.append(det)
            else:
                # Fallback: use line-level box and split multi-char texts
                xs = [p[0] * scale_x for p in box]
                ys = [p[1] * scale_y for p in box]

                if len(text) == 1:
                    bbox = BoundingBox(
                        x1=int(min(xs)), y1=int(min(ys)),
                        x2=int(max(xs)), y2=int(max(ys)),
                    )
                    center = (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)))
                    det = CharDetection(
                        text=text, confidence=float(score), bbox=bbox, center=center,
                    )
                    detections.append(det)
                else:
                    # Split multi-char detection by width
                    n = len(text)
                    x_min, x_max = min(xs), max(xs)
                    y_min, y_max = min(ys), max(ys)
                    total_w = x_max - x_min
                    if total_w > 0 and n > 0:
                        cw = total_w / n
                        for j, ch in enumerate(text):
                            cx1 = int(x_min + j * cw)
                            cx2 = int(x_min + (j + 1) * cw)
                            ch_bbox = BoundingBox(x1=cx1, y1=int(y_min), x2=cx2, y2=int(y_max))
                            ch_center = ((cx1 + cx2) / 2, (y_min + y_max) / 2)
                            det = CharDetection(
                                text=ch, confidence=float(score), bbox=ch_bbox, center=ch_center,
                            )
                            detections.append(det)

        return detections

    def _merge_detections(
        self, detections: List[CharDetection], iou_threshold: float
    ) -> List[CharDetection]:
        """Merge overlapping detections, keeping highest confidence per position."""
        if not detections:
            return []

        sorted_dets = sorted(detections, key=lambda d: d.confidence, reverse=True)
        merged: List[CharDetection] = []

        for det in sorted_dets:
            dominated = False
            for existing in merged:
                if existing.text == det.text and self._compute_iou(existing.bbox, det.bbox) > iou_threshold:
                    dominated = True
                    break
            if not dominated:
                merged.append(det)

        return merged

    @staticmethod
    def _compute_iou(box_a: BoundingBox, box_b: BoundingBox) -> float:
        x1 = max(box_a.x1, box_b.x1)
        y1 = max(box_a.y1, box_b.y1)
        x2 = min(box_a.x2, box_b.x2)
        y2 = min(box_a.y2, box_b.y2)

        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = max(0, box_a.x2 - box_a.x1) * max(0, box_a.y2 - box_a.y1)
        area_b = max(0, box_b.x2 - box_b.x1) * max(0, box_b.y2 - box_b.y1)
        union = area_a + area_b - intersection

        if union <= 0:
            return 0.0
        return intersection / union

    @staticmethod
    def _save_ocr_debug(image, detections, target_chars):
        """Save OCR detection details to JSON for debugging."""
        import json as _json
        from pathlib import Path

        debug_dir = Path("debug_output")
        debug_dir.mkdir(exist_ok=True)

        info = {
            "image_shape": list(image.shape),
            "target_chars": target_chars if target_chars else [],
            "detections": [
                {
                    "text": d.text,
                    "confidence": round(d.confidence, 4),
                    "center": [round(d.center[0], 1), round(d.center[1], 1)] if d.center else None,
                    "bbox": [d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2],
                }
                for d in detections
            ],
        }
        with open(debug_dir / "ocr_last.json", "w", encoding="utf-8") as f:
            _json.dump(info, f, ensure_ascii=False, indent=2)
        logger.debug(f"OCR debug saved to debug_output/ocr_last.json")
