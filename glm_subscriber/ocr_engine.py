"""PaddleOCR engine wrapper for Chinese character detection and recognition."""

import time
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
import yaml
from loguru import logger
from paddleocr import PaddleOCR

from glm_subscriber.types import BoundingBox, CharDetection


_DEFAULT_OCR_CONFIG = {
    "lang": "ch",
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_textline_orientation": True,
    "enable_mkldnn": False,
    "use_tensorrt": False,
    "text_det_thresh": 0.3,
    "text_det_box_thresh": 0.5,
    "text_det_unclip_ratio": 1.6,
    "text_recognition_batch_size": 6,
}

_PADDLEOCR_CONSTRUCTOR_PARAMS = {
    "lang", "ocr_version",
    "use_doc_orientation_classify", "use_doc_unwarping", "use_textline_orientation",
    "text_det_limit_side_len", "text_det_limit_type",
    "text_det_thresh", "text_det_box_thresh", "text_det_unclip_ratio",
    "text_det_input_shape", "text_rec_score_thresh",
    "text_recognition_batch_size", "textline_orientation_batch_size",
    "return_word_box", "text_rec_input_shape",
    "text_detection_model_name", "text_detection_model_dir",
    "text_recognition_model_name", "text_recognition_model_dir",
    "doc_orientation_classify_model_name", "doc_orientation_classify_model_dir",
    "doc_unwarping_model_name", "doc_unwarping_model_dir",
    "textline_orientation_model_name", "textline_orientation_model_dir",
}

_PADDLEOCR_KWARGS_PARAMS = {
    "enable_mkldnn", "use_tensorrt", "cpu_threads", "device",
    "engine", "engine_config", "enable_hpi", "precision",
    "mkldnn_cache_capacity", "enable_cinn",
}

_PADDLEOCR_ALL_PARAMS = _PADDLEOCR_CONSTRUCTOR_PARAMS | _PADDLEOCR_KWARGS_PARAMS


class OCREngine:

    def __init__(self, config: dict = None):
        ocr_config = {**_DEFAULT_OCR_CONFIG}
        if config:
            ocr_config.update(config)

        constructor_params = {
            k: v for k, v in ocr_config.items() if k in _PADDLEOCR_CONSTRUCTOR_PARAMS
        }
        kwargs_params = {
            k: v for k, v in ocr_config.items() if k in _PADDLEOCR_KWARGS_PARAMS
        }

        all_params = {**constructor_params, **kwargs_params}
        logger.info(f"Initializing PaddleOCR with {len(all_params)} params")
        self._ocr = PaddleOCR(**all_params)
        self._config = ocr_config
        logger.success("PaddleOCR engine initialized")

    def warmup(self) -> None:
        blank = np.ones((100, 100, 3), dtype=np.uint8) * 255
        start = time.perf_counter()
        self.detect_text(blank)
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(f"Warmup completed in {elapsed:.1f}ms")

    def detect_text(self, image: np.ndarray, target_chars: list = None) -> List[CharDetection]:
        if image is None or image.size == 0:
            logger.warning("Empty image provided to detect_text")
            return []

        # Run OCR with multiple preprocessing strategies and merge results.
        # Different preprocessing helps PaddleOCR detect characters it misses
        # with the default image (e.g. characters on complex/noisy backgrounds).
        # Pass target_chars for early-exit optimization.
        all_detections = self.detect_text_multi(image, target_chars=target_chars)
        return all_detections

    def detect_text_multi(
        self, image: np.ndarray, merge_iou_threshold: float = 0.3,
        target_chars: list = None,
    ) -> List[CharDetection]:
        """Run OCR with multiple preprocessing strategies and merge results.

        Strategies are ordered by reliability (best first). If target_chars
        is provided, we can early-exit as soon as all targets are found,
        skipping slower/less-reliable strategies.

        Strategy priority (based on empirical testing):
        1. 2x upscale  — most reliable, finds chars others miss (~6s)
        2. original    — fast baseline, good when image is clean (~2.5s)
        3. clahe       — contrast enhancement, sometimes finds missed chars (~2s)
        4. binary_otsu — rarely useful, last resort (~1s)

        Args:
            image: Input image (BGR numpy array)
            merge_iou_threshold: IoU threshold for merging overlapping detections
            target_chars: List of target characters to find. If all are found
                         after a strategy, remaining strategies are skipped.

        Returns:
            Merged list of CharDetection objects
        """
        all_raw: List[CharDetection] = []

        # Ordered by reliability: best first for early-exit
        strategy_names = ["2x_upscale", "original", "clahe", "binary_otsu"]

        for name in strategy_names:
            processed_img = self._apply_strategy(image, name)

            start = time.perf_counter()
            results = list(self._ocr.predict(processed_img))
            elapsed = (time.perf_counter() - start) * 1000

            if not results:
                logger.info(f"OCR strategy '{name}': 0 detections in {elapsed:.1f}ms")
                continue

            # Parse and scale coordinates back to original image space
            detections = self._parse_ocr_result(results[0])

            # If image was scaled, scale coordinates back
            h_proc, w_proc = processed_img.shape[:2]
            h_orig, w_orig = image.shape[:2]
            if w_proc != w_orig or h_proc != h_orig:
                sx = w_orig / w_proc
                sy = h_orig / h_proc
                for det in detections:
                    det.bbox.x1 = int(det.bbox.x1 * sx)
                    det.bbox.y1 = int(det.bbox.y1 * sy)
                    det.bbox.x2 = int(det.bbox.x2 * sx)
                    det.bbox.y2 = int(det.bbox.y2 * sy)
                    if det.center:
                        det.center = (det.center[0] * sx, det.center[1] * sy)

            logger.info(
                f"OCR strategy '{name}': {len(detections)} detections in {elapsed:.1f}ms"
            )
            for det in detections:
                logger.debug(f"  [{name}] '{det.text}' conf={det.confidence:.3f} at ({det.center[0]:.0f},{det.center[1]:.0f})")

            all_raw.extend(detections)

            # Early-exit: if we have all target chars, skip remaining strategies
            if target_chars:
                found_chars = {det.text for det in all_raw}
                if all(ch in found_chars for ch in target_chars):
                    logger.info(f"All {len(target_chars)} targets found after '{name}', skipping remaining strategies")
                    break

        # Merge overlapping detections: keep highest confidence per unique char position
        merged = self._merge_detections(all_raw, merge_iou_threshold)
        logger.info(f"Multi-OCR: {len(all_raw)} raw detections -> {len(merged)} after merge")
        return merged

    def _apply_strategy(self, image: np.ndarray, name: str) -> np.ndarray:
        """Apply a single preprocessing strategy to the image.

        Args:
            image: Original image (BGR numpy array)
            name: Strategy name: "original", "2x_upscale", "clahe", "binary_otsu"

        Returns:
            Preprocessed image (BGR numpy array)
        """
        if name == "original":
            return image

        if name == "2x_upscale":
            h, w = image.shape[:2]
            return cv2.resize(image, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)

        if name == "clahe":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)
            return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)

        if name == "binary_otsu":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

        logger.warning(f"Unknown OCR strategy: {name}")
        return image

    def _merge_detections(
        self, detections: List[CharDetection], iou_threshold: float
    ) -> List[CharDetection]:
        """Merge overlapping detections, keeping highest confidence per position.

        For detections of the same text with IoU > threshold, keep the one
        with highest confidence. Detections of different text at the same
        position are kept separately (different characters may overlap).

        Args:
            detections: All raw detections from multiple OCR passes
            iou_threshold: IoU threshold for considering detections as overlapping

        Returns:
            Deduplicated list of CharDetection objects
        """
        if not detections:
            return []

        # Sort by confidence descending (higher confidence first)
        sorted_dets = sorted(detections, key=lambda d: d.confidence, reverse=True)
        merged: List[CharDetection] = []

        for det in sorted_dets:
            # Check if this detection overlaps with an already-merged detection of the same text
            dominated = False
            for existing in merged:
                if existing.text == det.text and self._compute_iou(existing.bbox, det.bbox) > iou_threshold:
                    # Same text, overlapping position -> already have a higher-confidence one
                    dominated = True
                    break
            if not dominated:
                merged.append(det)

        return merged

    @staticmethod
    def _compute_iou(box_a: BoundingBox, box_b: BoundingBox) -> float:
        """Compute Intersection over Union of two bounding boxes.

        Args:
            box_a: First bounding box
            box_b: Second bounding box

        Returns:
            IoU value between 0.0 and 1.0
        """
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

    def detect_text_in_region(
        self,
        image: np.ndarray,
        region: tuple,
    ) -> List[CharDetection]:
        x1, y1, x2, y2 = region

        h, w = image.shape[:2]
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(w, int(x2)), min(h, int(y2))

        if x2 <= x1 or y2 <= y1:
            logger.warning(f"Invalid region: ({x1},{y1},{x2},{y2})")
            return []

        cropped = image[y1:y2, x1:x2]
        detections = self.detect_text(cropped)

        offset_detections = []
        for det in detections:
            offset_bbox = BoundingBox(
                x1=det.bbox.x1 + x1,
                y1=det.bbox.y1 + y1,
                x2=det.bbox.x2 + x1,
                y2=det.bbox.y2 + y1,
            )
            offset_det = CharDetection(
                text=det.text,
                confidence=det.confidence,
                bbox=offset_bbox,
            )
            offset_detections.append(offset_det)

        logger.debug(f"Detected {len(offset_detections)} chars in region ({x1},{y1},{x2},{y2})")
        return offset_detections

    def preprocess_image(self, image: np.ndarray, method: str = "default") -> np.ndarray:
        if method == "default":
            return image

        if method == "grayscale":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

        if method == "binary":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            binary = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 11, 2,
            )
            return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

        if method == "denoise":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            denoised = cv2.GaussianBlur(gray, (3, 3), 0)
            return cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR)

        logger.warning(f"Unknown preprocessing method: {method}")
        return image

    def _parse_ocr_result(self, result) -> List[CharDetection]:
        rec_texts = result.get("rec_texts", [])
        rec_scores = result.get("rec_scores", [])
        rec_polys = result.get("rec_polys", [])

        if not rec_texts:
            return []

        detections = []
        for i, (text, score, poly) in enumerate(zip(rec_texts, rec_scores, rec_polys)):
            text = text.strip()
            if not text:
                continue

            xs = poly[:, 0]
            ys = poly[:, 1]

            # If the detected text is a single character, use as-is
            if len(text) == 1:
                bbox = BoundingBox(
                    x1=int(min(xs)),
                    y1=int(min(ys)),
                    x2=int(max(xs)),
                    y2=int(max(ys)),
                )
                center = (float(np.mean(xs)), float(np.mean(ys)))
                detection = CharDetection(
                    text=text,
                    confidence=float(score),
                    bbox=bbox,
                    center=center,
                )
                detections.append(detection)
            else:
                # Multi-character detection: split into individual characters
                # by dividing the bounding box width equally among characters.
                # This is needed because PaddleOCR often merges adjacent Chinese
                # characters into a single detection (e.g. "趁崩" instead of "趁", "崩").
                n_chars = len(text)
                x_min = int(min(xs))
                x_max = int(max(xs))
                y_min = int(min(ys))
                y_max = int(max(ys))
                total_width = x_max - x_min
                
                if total_width > 0 and n_chars > 0:
                    char_width = total_width / n_chars
                    for j, ch in enumerate(text):
                        ch_x1 = int(x_min + j * char_width)
                        ch_x2 = int(x_min + (j + 1) * char_width)
                        ch_bbox = BoundingBox(
                            x1=ch_x1,
                            y1=y_min,
                            x2=ch_x2,
                            y2=y_max,
                        )
                        ch_cx = (ch_x1 + ch_x2) / 2
                        ch_cy = (y_min + y_max) / 2
                        ch_detection = CharDetection(
                            text=ch,
                            confidence=float(score),  # Inherit parent confidence
                            bbox=ch_bbox,
                            center=(ch_cx, ch_cy),
                        )
                        detections.append(ch_detection)
                        logger.debug(f"Split multi-char '{text}': char '{ch}' at center ({ch_cx:.0f},{ch_cy:.0f})")
                else:
                    # Fallback: can't split, add as-is
                    bbox = BoundingBox(x1=x_min, y1=y_min, x2=x_max, y2=y_max)
                    center = (float(np.mean(xs)), float(np.mean(ys)))
                    detection = CharDetection(
                        text=text,
                        confidence=float(score),
                        bbox=bbox,
                        center=center,
                    )
                    detections.append(detection)

        return detections
