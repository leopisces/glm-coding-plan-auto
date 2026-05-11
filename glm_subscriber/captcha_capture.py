"""CAPTCHA image capture and region splitting module."""

import cv2
import numpy as np
import yaml
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from glm_subscriber.types import BoundingBox, CaptchaRegion


class CaptchaCapture:
    """CAPTCHA image capture and region splitting.

    Handles capturing CAPTCHA images from browser pages and splitting
    them into main character area and prompt area.
    """

    DEFAULT_LAYOUT_RATIO = 0.75  # 75% for main area, 25% for prompt area

    def __init__(self, config: dict = None):
        """Initialize CaptchaCapture with configuration.

        Args:
            config: Configuration dict. If None, loads from config.yaml.
        """
        if config is None:
            config = self._load_config()

        self.config = config
        self.selectors = config.get("selectors", {})
        self.captcha_container_selector = self.selectors.get("captcha_container", "")

        # Layout ratio: portion of image for main area (top)
        # Default 0.75 means top 75% is main area, bottom 25% is prompt area
        self.layout_ratio = config.get("layout_ratio", self.DEFAULT_LAYOUT_RATIO)

        logger.debug(f"CaptchaCapture initialized with layout_ratio={self.layout_ratio}")

    def _load_config(self) -> dict:
        """Load configuration from config.yaml.

        Returns:
            Configuration dict loaded from YAML file.
        """
        config_path = Path(__file__).parent.parent / "config.yaml"
        if not config_path.exists():
            logger.warning(f"Config file not found at {config_path}, using defaults")
            return {}

        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def capture_captcha(self, page) -> np.ndarray:
        """Screenshot the CAPTCHA element and return as numpy array.

        Args:
            page: Playwright Page instance

        Returns:
            CAPTCHA image as numpy array (BGR format for cv2)

        Raises:
            Exception: If screenshot capture fails
        """
        selector = self.captcha_container_selector
        if not selector:
            raise ValueError("CAPTCHA container selector not configured in config.yaml")

        try:
            # Use page.locator to screenshot the element
            screenshot_bytes = page.locator(selector).screenshot()

            # Convert bytes to numpy array using cv2
            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if image is None:
                raise ValueError("Failed to decode screenshot image")

            logger.debug(f"Captured CAPTCHA image: shape={image.shape}")
            return image

        except Exception as e:
            logger.error(f"Failed to capture CAPTCHA image: {e}")
            raise

    def split_regions(
        self, image: np.ndarray, layout_ratio: float = None
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Split CAPTCHA image into main area and prompt area.

        The CAPTCHA image typically has:
        - Top portion: main character area with scattered characters
        - Bottom portion: prompt area with "请依次点击：字A 字B 字C"

        Args:
            image: CAPTCHA image as numpy array
            layout_ratio: Portion for main area (top). If None, uses config value.
                         Default 0.75 means top 75% is main area.

        Returns:
            Tuple of (main_area, prompt_area):
            - main_area: Top portion containing characters to click
            - prompt_area: Bottom portion containing instruction text
        """
        if layout_ratio is None:
            layout_ratio = self.layout_ratio

        height, width = image.shape[:2]
        split_line = int(height * layout_ratio)

        main_area = image[0:split_line, :]
        prompt_area = image[split_line:, :]

        logger.debug(
            f"Split image: height={height}, split_line={split_line}, "
            f"main_area_shape={main_area.shape}, prompt_area_shape={prompt_area.shape}"
        )

        return main_area, prompt_area

    def get_element_bbox(self, page, selector: str) -> Tuple[float, float, float, float]:
        """Get element bounding box in viewport coordinates.

        Args:
            page: Playwright Page instance
            selector: CSS selector for the element

        Returns:
            Tuple of (x, y, width, height) representing bounding box
        """
        try:
            bbox = page.locator(selector).bounding_box()

            if bbox is None:
                logger.warning(f"Element not found or not visible: {selector}")
                return None

            logger.debug(f"Element bbox for '{selector}': {bbox}")
            return (bbox["x"], bbox["y"], bbox["width"], bbox["height"])

        except Exception as e:
            logger.error(f"Failed to get bbox for '{selector}': {e}")
            raise

    def capture_element(self, page, selector: str) -> np.ndarray:
        """Screenshot a specific element and return as numpy array.

        Args:
            page: Playwright Page instance
            selector: CSS selector for the element to screenshot

        Returns:
            Image as numpy array (BGR format for cv2)

        Raises:
            Exception: If screenshot capture fails
        """
        try:
            screenshot_bytes = page.locator(selector).first.screenshot()

            nparr = np.frombuffer(screenshot_bytes, np.uint8)
            image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if image is None:
                raise ValueError("Failed to decode screenshot image")

            logger.debug(f"Captured element '{selector}': shape={image.shape}")
            return image

        except Exception as e:
            logger.error(f"Failed to capture element '{selector}': {e}")
            raise

    def save_debug_image(self, image: np.ndarray, name: str) -> str:
        """Save image to debug_output directory for analysis.

        Args:
            image: Image as numpy array (BGR format)
            name: Filename without extension

        Returns:
            Path to saved file
        """
        debug_dir = Path(__file__).parent.parent / "debug_output"
        debug_dir.mkdir(parents=True, exist_ok=True)

        filepath = debug_dir / f"{name}.png"

        try:
            success = cv2.imwrite(str(filepath), image)
            if success:
                logger.debug(f"Debug image saved: {filepath}")
                return str(filepath)
            else:
                logger.error(f"Failed to write debug image: {filepath}")
                return None

        except Exception as e:
            logger.error(f"Error saving debug image: {e}")
            raise

    def wait_for_captcha_image(
        self, page, selector: str, timeout: int = 10000
    ) -> bool:
        """Wait for CAPTCHA image to fully load.

        Uses naturalWidth check to verify the image has actual dimensions,
        which indicates it has loaded successfully.

        Args:
            page: Playwright Page instance
            selector: CSS selector for the CAPTCHA image element
            timeout: Maximum wait time in milliseconds (default: 10000)

        Returns:
            True if image loaded within timeout, False otherwise
        """
        try:
            # Wait for the image element to have naturalWidth > 0
            # This is more reliable than just waiting for the element
            is_loaded = page.wait_for_function(
                f"""
                () => {{
                    const img = document.querySelector('{selector}');
                    return img && img.naturalWidth > 0;
                }}
                """,
                timeout=timeout,
            )

            if is_loaded:
                logger.debug(f"CAPTCHA image loaded: {selector}")
                return True
            else:
                logger.warning(f"CAPTCHA image did not load within {timeout}ms")
                return False

        except Exception as e:
            logger.warning(f"Error waiting for CAPTCHA image: {e}")
            return False

    def create_captcha_region(
        self, name: str, bbox: Tuple[float, float, float, float]
    ) -> CaptchaRegion:
        """Create a CaptchaRegion from bounding box coordinates.

        Args:
            name: Region identifier (e.g., 'main_area', 'prompt_area')
            bbox: Bounding box (x1, y1, x2, y2)

        Returns:
            CaptchaRegion instance
        """
        return CaptchaRegion(name=name, bbox=bbox)

    def get_region_from_bbox(
        self, page, selector: str, region_name: str
    ) -> CaptchaRegion:
        """Get a CaptchaRegion by capturing an element and creating a region.

        Args:
            page: Playwright Page instance
            selector: CSS selector for the element
            region_name: Name for the region

        Returns:
            CaptchaRegion with element's bounding box
        """
        bbox = self.get_element_bbox(page, selector)
        if bbox:
            x, y, width, height = bbox
            # Convert (x, y, width, height) to (x1, y1, x2, y2)
            bbox_tuple = (x, y, x + width, y + height)
            return self.create_captcha_region(region_name, bbox_tuple)
        return None