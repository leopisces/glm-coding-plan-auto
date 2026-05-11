"""Retry and error handling orchestrator for CAPTCHA solving.

Wraps CaptchaSolver with exponential backoff retry logic and
classifies errors into retryable vs non-retryable categories.
"""

import random
import sys
import time
from pathlib import Path

import yaml
from loguru import logger

from glm_subscriber.captcha_solver import CaptchaSolver
from glm_subscriber.types import SolverResult
from glm_subscriber.browser import is_captcha_failed


_NON_RETRYABLE_KEYWORDS = ("browser", "connection", "disconnected", "closed")

_WAIT_RETRYABLE_KEYWORDS = ("load", "timeout", "network", "capture_failed")


def _load_retry_config() -> dict:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        config_path = exe_dir / "config.yaml" if (exe_dir / "config.yaml").exists() else Path("config.yaml")
    else:
        for p in [Path("config.yaml"), Path(__file__).parent.parent / "config.yaml"]:
            if p.exists():
                config_path = p
                break
        else:
            config_path = Path("config.yaml")
    if not config_path.exists():
        logger.debug(f"Config file not found at {config_path}, using retry defaults")
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load config.yaml: {e}, using retry defaults")
        return {}


def classify_error(error: str) -> str:
    """Classify an error into retry strategy categories.

    Args:
        error: Error string from SolverResult.error

    Returns:
        One of:
        - "non_retryable": Unrecoverable (browser disconnected, etc.)
        - "retryable_wait": Retry after waiting (image load, timeout)
        - "retryable": Direct retry (low confidence, wrong clicks)
    """
    if not error:
        return "retryable"

    error_lower = error.lower()

    for keyword in _NON_RETRYABLE_KEYWORDS:
        if keyword in error_lower:
            return "non_retryable"

    for keyword in _WAIT_RETRYABLE_KEYWORDS:
        if keyword in error_lower:
            return "retryable_wait"

    return "retryable"


class CaptchaOrchestrator:
    """Retry and error handling wrapper around CaptchaSolver.

    Implements exponential backoff with jitter for retryable errors
    and immediate failure for non-retryable errors.

    Attributes:
        solver: CaptchaSolver instance for actual CAPTCHA solving.
        max_retries: Maximum number of retry attempts.
        backoff_multiplier: Exponential backoff multiplier.
        base_delay: Base delay in seconds for backoff calculation.
    """

    def __init__(self, solver: CaptchaSolver, config: dict = None):
        """Initialize the orchestrator.

        Args:
            solver: CaptchaSolver instance.
            config: Optional config dict. If not provided, reads from config.yaml.
                    Supports keys: retry.max_attempts, retry.backoff_multiplier.
        """
        self.solver = solver
        self.config = config or {}

        if self.config.get("retry", {}).get("max_attempts") is not None:
            self.max_retries = self.config["retry"]["max_attempts"]
        else:
            yaml_config = _load_retry_config()
            self.max_retries = yaml_config.get("retry", {}).get("max_attempts", 5)

        if self.config.get("retry", {}).get("backoff_multiplier") is not None:
            self.backoff_multiplier = self.config["retry"]["backoff_multiplier"]
        else:
            yaml_config = _load_retry_config()
            self.backoff_multiplier = yaml_config.get("retry", {}).get("backoff_multiplier", 1.5)

        self.base_delay = 0.5

        logger.info(
            f"CaptchaOrchestrator initialized: max_retries={self.max_retries}, "
            f"backoff_multiplier={self.backoff_multiplier}, base_delay={self.base_delay}s"
        )

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff delay with jitter.

        Formula: base_delay * (backoff_multiplier ** attempt) + jitter
        Jitter: random uniform in [0, delay * 0.3]

        Args:
            attempt: Current attempt number (0-based).

        Returns:
            Delay in seconds before next retry.
        """
        delay = self.base_delay * (self.backoff_multiplier ** attempt)
        jitter = random.uniform(0, delay * 0.3)
        return delay + jitter

    def solve_with_retry(self, page) -> SolverResult:
        """Solve CAPTCHA with retry and error handling.

        Retry strategies:
        - Low confidence → direct retry (same CAPTCHA, re-capture and re-recognize)
        - CAPTCHA failed (wrong clicks) → direct retry (CAPTCHA doesn't refresh)
        - Image load failure → wait then retry
        - Max retries reached → return failure
        - Unrecoverable error (browser disconnected) → return failure immediately

        Args:
            page: Playwright Page instance.

        Returns:
            SolverResult from the last attempt (success or failure).
        """
        last_result = None

        for attempt in range(self.max_retries):
            logger.info(f"CAPTCHA solve attempt {attempt + 1}/{self.max_retries}")

            try:
                result = self.solver.solve(page)
            except Exception as e:
                logger.error(f"Solver threw exception on attempt {attempt + 1}: {e}")
                last_result = SolverResult(
                    success=False,
                    target_text="",
                    clicked_positions=[],
                    detections=[],
                    targets_found=0,
                    targets_requested=0,
                    error=f"exception: {str(e)}",
                    retry_allowed=True,
                )
                error_category = classify_error(last_result.error)
                if error_category == "non_retryable":
                    logger.error(f"Non-retryable exception: {last_result.error}, giving up")
                    return last_result
                result = last_result

            if result.success:
                logger.success(
                    f"CAPTCHA solved on attempt {attempt + 1}/{self.max_retries}"
                )
                return result

            last_result = result
            error_category = classify_error(result.error)

            logger.warning(
                f"Attempt {attempt + 1} failed: error='{result.error}', "
                f"category='{error_category}', retry_allowed={result.retry_allowed}"
            )

            if error_category == "non_retryable":
                logger.error(f"Non-retryable error: {result.error}, giving up immediately")
                return result

            if not result.retry_allowed:
                logger.error("Solver indicates retry not allowed, giving up")
                return result

            if attempt >= self.max_retries - 1:
                logger.error(
                    f"Max retries ({self.max_retries}) reached, returning last failure"
                )
                return result

            delay = self._calculate_backoff(attempt)

            if error_category == "retryable_wait":
                extra_wait = random.uniform(1.0, 2.0)
                delay += extra_wait
                logger.info(
                    f"Load/timeout error, adding extra wait {extra_wait:.2f}s, "
                    f"total delay {delay:.2f}s"
                )

            logger.info(f"Waiting {delay:.2f}s before retry (attempt {attempt + 1} → {attempt + 2})")
            time.sleep(delay)

        # Should not reach here, but safety net
        if last_result is not None:
            return last_result

        return SolverResult(
            success=False,
            target_text="",
            clicked_positions=[],
            detections=[],
            targets_found=0,
            targets_requested=0,
            error="max_retries_exceeded",
            retry_allowed=False,
        )
