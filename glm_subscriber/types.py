"""Type definitions for GLM auto-subscribe tool."""

from typing import List, Tuple
from dataclasses import dataclass


@dataclass
class BoundingBox:
    """Bounding box for detected object.

    Attributes:
        x1: Left coordinate
        y1: Top coordinate
        x2: Right coordinate
        y2: Bottom coordinate
    """
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        """Get width of bounding box."""
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        """Get height of bounding box."""
        return self.y2 - self.y1

    @property
    def center(self) -> Tuple[float, float]:
        """Get center coordinates (x, y)."""
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)


@dataclass
class CaptchaRegion:
    """Region of interest in CAPTCHA image for OCR processing.

    Attributes:
        name: Region identifier (e.g., 'target_text', 'click_options')
        bbox: Bounding box coordinates (x1, y1, x2, y2)
    """
    name: str
    bbox: Tuple[float, float, float, float]


@dataclass
class CharDetection:
    """Detected character with position and confidence.

    Attributes:
        text: The detected character text
        confidence: Confidence score (0.0 to 1.0)
        bbox: Bounding box of the detection
        center: Center coordinates (x, y) as tuple
    """
    text: str
    confidence: float
    bbox: BoundingBox
    center: Tuple[float, float] = None

    def __post_init__(self):
        """Calculate center from bbox if not provided."""
        if self.center is None and self.bbox:
            self.center = ((self.bbox.x1 + self.bbox.x2) / 2,
                          (self.bbox.y1 + self.bbox.y2) / 2)

    @property
    def center_x(self) -> float:
        """Get center X coordinate."""
        return self.center[0]

    @property
    def center_y(self) -> float:
        """Get center Y coordinate."""
        return self.center[1]


@dataclass
class ClickTarget:
    """Target character to click in CAPTCHA.

    Attributes:
        char: The character to find and click
        index: Order index (0-based) in the sequence
        detection: The character detection result (if found)
        position: Absolute pixel position (x, y) for clicking
    """
    char: str
    index: int
    detection: CharDetection = None
    position: Tuple[int, int] = None


@dataclass
class SolverResult:
    """Result of CAPTCHA solving attempt.

    Attributes:
        success: Whether CAPTCHA was solved successfully
        target_text: The text that was supposed to be clicked
        clicked_positions: List of (x, y) positions that were clicked
        detections: All character detections found
        targets_found: Number of targets successfully found
        targets_requested: Total number of targets requested
        error: Error message if failed
        retry_allowed: Whether retrying on same CAPTCHA is allowed
    """
    success: bool
    target_text: str
    clicked_positions: List[Tuple[int, int]]
    detections: List[CharDetection]
    targets_found: int
    targets_requested: int
    error: str = None
    retry_allowed: bool = True

    @property
    def all_targets_found(self) -> bool:
        """Check if all targets were found."""
        return self.targets_found == self.targets_requested


@dataclass
class OcrResult:
    """Result from OCR processing.

    Attributes:
        text: Full recognized text
        confidence: Average confidence score
        detections: List of individual character detections
    """
    text: str
    confidence: float
    detections: List[CharDetection]


@dataclass
class CaptchaConfig:
    """Configuration for CAPTCHA solving.

    Attributes:
        cdp_port: Chrome DevTools Protocol port
        captcha_selectors: CSS selectors for CAPTCHA elements
        ocr_timeout: OCR processing timeout in seconds
        click_delay: Delay between clicks in milliseconds
        max_retries: Maximum retry attempts
        confidence_threshold: Minimum confidence for detection
    """
    cdp_port: int = 9222
    captcha_selectors: dict = None
    ocr_timeout: int = 30
    click_delay: int = 500
    max_retries: int = 3
    confidence_threshold: float = 0.7

    def __post_init__(self):
        """Set default selectors if not provided."""
        if self.captcha_selectors is None:
            self.captcha_selectors = {
                "captcha_container": "",
                "target_text": "",
                "click_option": "",
                "confirm_button": ""
            }
