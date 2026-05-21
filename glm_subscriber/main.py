"""CLI entry point for GLM auto-subscribe tool."""

import argparse
import multiprocessing
import os
import sys
import time
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path

import yaml
from loguru import logger

from glm_subscriber.rapidocr_engine import RapidOCREngine as OCREngine
from glm_subscriber.browser import (
    BrowserClosedError,
    cleanup,
    click_subscribe_button,
    connect_browser,
    get_mode,
    get_first_page,
    is_page_closed,
    launch_persistent_context,
    wait_for_captcha_popup,
)
from glm_subscriber.captcha_capture import CaptchaCapture
from glm_subscriber.captcha_solver import CaptchaSolver, _is_sliding_captcha
from glm_subscriber.notify import send_notification
from glm_subscriber.types import CaptchaConfig


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(".")


def setup_logging(log_level: str, instance: str = "") -> None:
    logger.remove()
    tag = f"[{instance}] " if instance else ""
    log_fmt = f"<green>{{time:YYYY-MM-DD HH:mm:ss}}</green> | <level>{{level: <8}}</level> | {tag}<level>{{message}}</level>"
    logger.add(sys.stderr, level=log_level, format=log_fmt)
    if instance:
        log_dir = _base_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / f"glm_subscriber_{instance}.log",
            level=log_level,
            format=f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level: <8}} | {tag}{{message}}",
            rotation="10MB",
            retention="7 days",
        )


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.is_absolute():
        exe_config = _base_dir() / config_path
        if exe_config.exists():
            path = exe_config
    if not path.exists():
        logger.warning(f"Config file not found: {config_path}, using defaults")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_payment_amount(page) -> str:
    try:
        amount = page.evaluate("""() => {
            const dialog = document.querySelector('.pay-dialog');
            if (!dialog) return '';
            const priceEl = dialog.querySelector('.info-price');
            if (priceEl) return priceEl.innerText.trim();
            const scanBox = dialog.querySelector('.scan-code-box');
            if (scanBox) {
                const m = scanBox.innerText.match(/[$￥¥]\\s*(\\d[\\d.]*)/);
                if (m) return '￥' + m[1];
            }
            return '';
        }""")
        return str(amount) if amount else ""
    except Exception:
        return ""


def _check_payment_page(page) -> str:
    """Check payment dialog status.

    Returns:
        "valid"  - payment dialog with a real amount (e.g. ￥470.4) is visible
        "empty"  - payment dialog is visible but has no amount (just "￥" without digits)
        "busy"   - "购买人数较多" or similar retry message is showing
        "none"   - no payment dialog visible
    """
    try:
        result = page.evaluate("""() => {
            const dialog = document.querySelector('.pay-dialog');
            if (!dialog) {
                // Also check for busy/error messages outside pay-dialog
                const body = document.body.innerText || '';
                if (/购买人数较多/.test(body)) return 'busy';
                return 'none';
            }
            if (dialog.offsetParent === null && !dialog.offsetWidth) return 'none';

            // Check for busy/retry messages inside dialog
            const dialogText = dialog.innerText || '';
            if (/购买人数较多|稍后重试/.test(dialogText)) return 'busy';

            // Make sure CAPTCHA is not still blocking
            const mask = document.querySelector('#tCaptchaMaskLayer');
            if (mask && mask.offsetParent !== null) return 'none';
            const captcha = document.querySelector('#tcaptcha_transform_dy');
            if (captcha && captcha.offsetParent !== null) return 'none';

            // Check the info-price element inside the dialog for actual amount
            const priceEl = dialog.querySelector('.info-price');
            if (priceEl) {
                const priceText = priceEl.innerText.trim();
                // If the price element contains a digit after ￥, it's a real amount
                if (/\\d/.test(priceText)) {
                    return 'valid';
                }
                // Only "￥" with no number → empty / no real order
                return 'empty';
            }

            // Fallback: scan the scan-code-box for price digits
            const scanBox = dialog.querySelector('.scan-code-box');
            if (scanBox) {
                const scanText = scanBox.innerText;
                // Look for pattern like ￥470.4 or ¥132.3
                if (/[$￥¥]\\s*\\d+/.test(scanText)) {
                    return 'valid';
                }
                return 'empty';
            }

            // Fallback: check body text
            const body = document.body.innerText || '';
            if (/[$￥¥]\\s*\\d+/.test(body)) {
                return 'valid';
            }

            return 'empty';
        }""")
        return str(result)
    except Exception:
        return "none"


def _close_captcha_popup(page) -> None:
    """Close CAPTCHA popup by clicking the X button."""
    try:
        close_btn = page.locator(".tencent-captcha-dy__header-close")
        if close_btn.count() > 0:
            close_btn.first.click(timeout=2000)
            logger.debug("Closed CAPTCHA popup via X button")
            return
    except Exception:
        pass
    try:
        mask = page.locator("#tCaptchaMaskLayer")
        if mask.count() > 0:
            mask.first.click(timeout=2000, force=True)
            logger.debug("Clicked mask layer to close")
    except Exception:
        pass


def _close_payment_dialog(page) -> None:
    """Close the payment dialog by clicking the X (close) button."""
    try:
        close_btn = page.locator(".pay-dialog .el-dialog__headerbtn")
        if close_btn.count() > 0:
            close_btn.first.click(timeout=3000, force=True)
            logger.info("Closed payment dialog via X button")
            return
    except Exception:
        pass
    try:
        # Fallback: click the overlay/mask behind the dialog
        overlay = page.locator(".el-dialog__wrapper .custom-mask")
        if overlay.count() > 0:
            overlay.first.click(timeout=2000, force=True)
            logger.info("Clicked overlay to close payment dialog")
            return
    except Exception:
        pass
    try:
        # Fallback: hide via JS (nuclear option)
        page.evaluate("""() => {
            const dialog = document.querySelector('.pay-dialog');
            if (dialog) dialog.style.display = 'none';
            const wrapper = document.querySelector('.el-dialog__wrapper');
            if (wrapper) wrapper.style.display = 'none';
            const mask = document.querySelector('.custom-mask');
            if (mask) mask.style.display = 'none';
        }""")
        logger.info("Force-hid payment dialog via JS")
    except Exception:
        logger.warning("Failed to close payment dialog")


def run_offline_test(debug: bool = False) -> None:
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np

    logger.info("Running offline OCR test mode")

    test_images = []

    prompt_texts = ["请依次点击", "字A", "字B", "字C"]
    char_images = ["中", "国", "美", "丽", "人", "口", "大", "天"]

    prompt_img = Image.new("RGB", (400, 60), color="white")
    draw = ImageDraw.Draw(prompt_img)
    try:
        font = ImageFont.truetype("msyh.ttc", 24)
    except Exception:
        font = ImageFont.load_default()
    draw.text((10, 15), " ".join(prompt_texts), fill="black", font=font)
    prompt_arr = np.array(prompt_img)
    test_images.append(("prompt", prompt_arr))
    logger.info(f"Created prompt test image: {' '.join(prompt_texts)}")

    main_img = Image.new("RGB", (600, 400), color="white")
    draw = ImageDraw.Draw(main_img)
    for i, char in enumerate(char_images):
        x = 30 + (i % 8) * 70
        y = 50 + (i // 8) * 150
        draw.rectangle([x - 5, y - 5, x + 45, y + 55], outline="gray", width=1)
        draw.text((x, y), char, fill="black", font=font)
    main_arr = np.array(main_img)
    test_images.append(("main", main_arr))
    logger.info(f"Created main area test image with characters: {char_images}")

    if debug:
        debug_dir = Path("debug_output")
        debug_dir.mkdir(exist_ok=True)
        for name, arr in test_images:
            from PIL import Image as PILImage
            PILImage.fromarray(arr).save(debug_dir / f"test_{name}.png")
        logger.info(f"Test images saved to {debug_dir}")

    logger.info("Initializing OCR engine...")
    ocr = OCREngine()
    ocr.warmup()

    logger.info("Testing OCR on prompt area...")
    from glm_subscriber.captcha_solver import detect_char_positions, match_target_chars
    detections = ocr.detect_text(test_images[0][1])
    logger.info(f"Prompt area detections: {[(d.text, d.confidence) for d in detections]}")

    prompt_detections = detect_char_positions(test_images[0][1], ocr)
    target_chars = [d.text for d in prompt_detections if len(d.text) == 1]
    logger.success(f"Identified target chars: {target_chars}")

    logger.info("Testing OCR on main area...")
    main_detections = ocr.detect_text(test_images[1][1])
    logger.info(f"Main area detections: {[(d.text, d.confidence, d.center) for d in main_detections]}")

    logger.info("Testing split_regions...")
    capture = CaptchaCapture()
    main_area, prompt_area = capture.split_regions(test_images[1][1])
    logger.info(f"Split result: main_area shape={main_area.shape}, prompt_area shape={prompt_area.shape}")

    logger.success("Offline test completed successfully!")


def _log_stats(stats: dict, instance_id: str = "", final: bool = False) -> None:
    attempts = stats["captcha_attempts"]
    solved = stats["captcha_solved"]
    failed = stats["captcha_failed"]
    full = stats["ocr_full_match"]
    partial = stats["ocr_partial_match"]
    solve_rate = f"{solved / attempts * 100:.1f}%" if attempts else "N/A"
    ocr_rate = f"{full / attempts * 100:.1f}%" if attempts else "N/A"
    prefix = f"[{instance_id}]" if instance_id else ""
    tag = "FINAL STATS" if final else "STATS"
    logger.info(
        f"{prefix} {tag} | attempts={attempts} solved={solved} failed={failed} "
        f"solve_rate={solve_rate} | ocr_full={full} ocr_partial={partial} ocr_rate={ocr_rate}"
    )

    import json

    stats_dir = _base_dir() / "logs"
    try:
        stats_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"Failed to create stats dir {stats_dir}: {e}")
        return

    stats_file = stats_dir / "stats.txt"
    tmp_file = stats_dir / "stats.tmp"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    inst_key = instance_id or "default"

    cur = {
        "id": inst_key,
        "attempts": attempts, "solved": solved, "failed": failed,
        "ocr_full": full, "ocr_partial": partial,
    }
    if stats.get("recent_errors"):
        cur["recent_errors"] = stats["recent_errors"][-10:]

    try:
        entries = {}
        if stats_file.exists():
            for line in stats_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#DATA#"):
                    try:
                        obj = json.loads(line[6:])
                        entries[obj["id"]] = obj
                    except (json.JSONDecodeError, KeyError):
                        pass
        entries[inst_key] = cur

        all_data = sorted(entries.values(), key=lambda x: x["id"])
        t_a = sum(d.get("attempts", 0) for d in all_data)
        t_s = sum(d.get("solved", 0) for d in all_data)
        t_f = sum(d.get("failed", 0) for d in all_data)
        t_full = sum(d.get("ocr_full", 0) for d in all_data)
        t_part = sum(d.get("ocr_partial", 0) for d in all_data)
        t_solve = f"{t_s / t_a * 100:.1f}%" if t_a else "N/A"
        t_ocr = f"{t_full / t_a * 100:.1f}%" if t_a else "N/A"

        hdr = f"{'Instance':<10} {'Attempts':>8} {'Solved':>8} {'Failed':>8} {'SolveRate':>10} {'OCR_Full':>9} {'OCR_Partial':>12} {'OCR_Rate':>9}"
        sep = "-" * len(hdr)

        lines = []
        for d in all_data:
            lines.append(f"#DATA#{json.dumps(d, ensure_ascii=False)}")
        lines.append("")
        lines.append("=" * len(hdr))
        lines.append(f"  GLM Coding Plan - CAPTCHA Stats | {ts}")
        lines.append("=" * len(hdr))
        lines.append("")
        lines.append(hdr)
        lines.append(sep)
        for d in all_data:
            a = d.get("attempts", 0)
            s = d.get("solved", 0)
            fa = d.get("failed", 0)
            fu = d.get("ocr_full", 0)
            pa = d.get("ocr_partial", 0)
            sr = f"{s / a * 100:.1f}%" if a else "N/A"
            or_ = f"{fu / a * 100:.1f}%" if a else "N/A"
            lines.append(f"{d['id']:<10} {a:>8} {s:>8} {fa:>8} {sr:>10} {fu:>9} {pa:>12} {or_:>9}")
        lines.append(sep)
        lines.append(f"{'TOTAL':<10} {t_a:>8} {t_s:>8} {t_f:>8} {t_solve:>10} {t_full:>9} {t_part:>12} {t_ocr:>9}")
        lines.append(sep)

        has_errors = False
        for d in all_data:
            errs = d.get("recent_errors", [])
            if errs:
                has_errors = True
                lines.append(f"\n[{d['id']}] Recent errors:")
                for err in errs[-10:]:
                    lines.append(f"  - {err}")
        if not has_errors:
            lines.append("\n  No errors.")

        tmp_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp_file.replace(stats_file)
    except Exception as e:
        logger.warning(f"Failed to write stats file: {e}")


def _wait_until_start_time(config: dict, args_ns) -> None:
    schedule_cfg = config.get("schedule", {})
    enabled = schedule_cfg.get("enabled", True)
    if not enabled:
        logger.info("定时模式已禁用 (schedule.enabled=false)，立即开始")
        return
    start_time_str = args_ns.start_time if args_ns.start_time else schedule_cfg.get("start_time", "")
    if not start_time_str:
        return

    try:
        target = datetime.strptime(start_time_str, "%H:%M:%S").time()
    except ValueError:
        try:
            target = datetime.strptime(start_time_str, "%H:%M").time()
        except ValueError:
            logger.error(f"Invalid start_time format: '{start_time_str}', expected HH:MM:SS or HH:MM")
            return

    now = datetime.now()
    target_dt = datetime.combine(now.date(), target)
    if target_dt <= now:
        target_dt = datetime.combine(now.date() + timedelta(days=1), target)

    remaining = (target_dt - now).total_seconds()
    logger.info(f"定时模式：目标时间 {start_time_str}，等待 {remaining:.0f} 秒后开始点击")
    _interruptible_sleep(remaining)
    logger.success(f"到达定时 {start_time_str}，开始点击！")


def _interruptible_sleep(seconds: float, check_interval: float = 1.0) -> None:
    elapsed = 0.0
    while elapsed < seconds:
        sleep_dur = min(check_interval, seconds - elapsed)
        time.sleep(sleep_dur)
        elapsed += sleep_dur
        if elapsed < seconds and int(elapsed) % 30 == 0 and int(elapsed) > 0:
            remaining = seconds - elapsed
            logger.info(f"定时等待中... 剩余 {remaining:.0f} 秒")


def _worker(instance_id: str, args_ns) -> None:
    os.environ["LOGURU_REMOVE_ALL_HANDLERS"] = "1"
    setup_logging(args_ns.log_level, instance=instance_id)

    if args_ns.instance:
        instance_config = Path(f"config_{args_ns.instance}.yaml")
        config_path = str(instance_config) if instance_config.exists() else args_ns.config
    elif instance_id:
        instance_config = Path(f"config_{instance_id}.yaml")
        config_path = str(instance_config) if instance_config.exists() else args_ns.config
        logger.info(f"Instance '{instance_id}': using config {config_path}")
    else:
        config_path = args_ns.config
    config = load_config(config_path)
    if args_ns.debug:
        config["debug"] = True

    if args_ns.billing_cycle:
        config.setdefault("selectors", {})["billing_cycle"] = args_ns.billing_cycle

    retry_config = config.get("retry", {})
    confidence_config = config.get("confidence", {})

    max_retries = args_ns.max_retries if args_ns.max_retries is not None else retry_config.get("max_attempts", 5)
    infinite = max_retries == -1
    if infinite:
        logger.info("Infinite retry mode enabled (max_attempts = -1)")
    confidence_threshold = (
        args_ns.confidence_threshold
        if args_ns.confidence_threshold is not None
        else confidence_config.get("min_detection", 0.6)
    )

    logger.info("Initializing OCR engine...")
    ocr = OCREngine(config.get("ocr", {}))
    ocr.warmup()

    logger.info("Initializing captcha capture...")
    capture = CaptchaCapture(config)

    logger.info("Initializing CAPTCHA solver...")
    solver = CaptchaSolver(ocr, capture, config)

    effective_instance = args_ns.instance or instance_id

    stats = {
        "captcha_attempts": 0, "captcha_solved": 0, "captcha_failed": 0,
        "ocr_full_match": 0, "ocr_partial_match": 0, "recent_errors": [],
    }

    try:
        browser_config = config.get("browser", {})
        browser_mode = args_ns.browser_mode or browser_config.get("mode", "cdp")

        if browser_mode == "persistent":
            user_data_dir = browser_config.get("user_data_dir", ".browser-data")
            if effective_instance:
                user_data_dir = f"{user_data_dir}-{effective_instance}"
            headless = browser_config.get("headless", False)
            url = browser_config.get("url", "")
            logger.info(f"Launching persistent browser (mode={browser_mode})...")
            browser = launch_persistent_context(user_data_dir, headless=headless, url=url)
        else:
            cdp_port = args_ns.cdp_port or browser_config.get("cdp_port", 9222)
            if effective_instance:
                cdp_port += int(effective_instance)
            logger.info(f"Connecting to browser via CDP on port {cdp_port}...")
            browser = connect_browser(cdp_port)

        logger.info("Getting first page...")
        page = get_first_page(browser)
        if page is None:
            logger.error("Failed to get page from browser")
            return

        _wait_until_start_time(config, args_ns)

        cycle = 0
        while True:
            cycle += 1
            if not infinite and cycle > max_retries:
                break

            if is_page_closed(page):
                logger.error("Browser/page has been closed, stopping worker.")
                break

            if infinite:
                logger.info(f"=== Cycle {cycle} ===")
            else:
                logger.info(f"=== Cycle {cycle}/{max_retries} ===")

            pay_status = _check_payment_page(page)
            if pay_status == "busy":
                logger.warning("购买人数较多，稍后重试...")
                _close_payment_dialog(page)
                time.sleep(2)
            elif pay_status == "empty":
                logger.info("Found empty payment dialog from previous cycle, closing it...")
                _close_payment_dialog(page)
                time.sleep(1)
            elif pay_status == "valid":
                logger.success("Payment page with real amount already showing! Stopping.")
                break

            logger.info("Clicking subscribe button...")
            try:
                billing_cycle = config.get("selectors", {}).get("billing_cycle", "")
                click_subscribe_button(page, billing_cycle=billing_cycle)
            except BrowserClosedError:
                logger.error("Browser closed while clicking subscribe button, stopping worker.")
                break

            logger.info("Waiting for CAPTCHA popup...")
            try:
                captcha_appeared = wait_for_captcha_popup(page, timeout=5000)
            except BrowserClosedError:
                logger.error("Browser closed while waiting for CAPTCHA, stopping worker.")
                break
            if not captcha_appeared:
                pay_status = _check_payment_page(page)
                if pay_status == "valid":
                    logger.success("Payment page with amount detected! Stopping.")
                    break
                if pay_status == "busy":
                    logger.warning("购买人数较多，稍后重试...")
                    _close_payment_dialog(page)
                    time.sleep(2)
                    continue
                if pay_status == "empty":
                    logger.warning("Payment dialog appeared but no amount, closing and retrying...")
                    _close_payment_dialog(page)
                    time.sleep(1)
                    continue
                logger.warning("CAPTCHA popup did not appear, retrying...")
                continue

            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('.tencent-captcha-dy__header-text')
                            || document.querySelector('.tencent-captcha-dy__header-title-wrap');
                        return el && el.textContent && el.textContent.includes('请依次点击');
                    }""",
                    timeout=3000,
                )
            except Exception:
                pass

            # Detect sliding CAPTCHA and skip it
            if _is_sliding_captcha(page):
                logger.info("Detected sliding CAPTCHA, closing and retrying...")
                _close_captcha_popup(page)
                time.sleep(1)
                continue

            result = solver.solve(page)
            stats["captcha_attempts"] += 1
            if result.success:
                stats["captcha_solved"] += 1
            else:
                stats["captcha_failed"] += 1
                err_msg = f"cycle={cycle} error={result.error} found={result.targets_found}/{result.targets_requested}"
                stats["recent_errors"].append(err_msg)
                if len(stats["recent_errors"]) > 20:
                    stats["recent_errors"] = stats["recent_errors"][-20:]
            if result.targets_found == result.targets_requested:
                stats["ocr_full_match"] += 1
            elif result.targets_found > 0:
                stats["ocr_partial_match"] += 1

            _log_stats(stats, effective_instance)

            if result.success:
                logger.success(f"CAPTCHA solved! Clicked: {result.clicked_positions}")

                time.sleep(1)
                pay_status = _check_payment_page(page)
                if pay_status == "valid":
                    logger.success("Payment page with real amount detected! Stopping.")
                    break
                if pay_status == "busy":
                    logger.warning("购买人数较多，稍后重试...")
                    _close_captcha_popup(page)
                    _close_payment_dialog(page)
                    time.sleep(2)
                    continue
                if pay_status == "empty":
                    logger.warning("Payment dialog has no amount, closing and retrying...")
                    _close_captcha_popup(page)
                    _close_payment_dialog(page)
                    continue
                logger.info("CAPTCHA passed but no payment page yet, continuing...")
            else:
                logger.warning(f"Cycle {cycle + 1} failed: {result.error}")

            _close_captcha_popup(page)

        pay_status = _check_payment_page(page)
        if pay_status == "valid":
            amount = _get_payment_amount(page)
            plan = config.get("selectors", {}).get("plan", "")
            logger.success(f"Done - payment page with amount is showing! ({amount})")
            send_notification(config, plan=plan, amount=amount)
            if get_mode() == "persistent":
                logger.info("浏览器保持打开，请完成支付后手动关闭")
                input("按回车键退出...")
        elif pay_status == "empty":
            logger.error("Payment page is showing but has no amount after all retries")
        elif infinite:
            logger.error("Exited infinite loop (should not happen without valid payment)")
        else:
            logger.error(f"Failed after {max_retries} cycles")

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Error: {e}")
    finally:
        if stats["captcha_attempts"] > 0:
            _log_stats(stats, effective_instance, final=True)
        logger.info("Cleaning up...")
        cleanup()


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        description="GLM Auto-Subscribe Tool - Click-text CAPTCHA solver"
    )
    parser.add_argument(
        "--browser-mode",
        type=str,
        default=None,
        choices=["cdp", "persistent"],
        help="Browser mode: 'cdp' (connect to running Chrome) or 'persistent' (launch own Chrome). Default: from config",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=None,
        help="Chrome DevTools Protocol port (default: from config or 9222)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Run offline test mode using sample images",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save intermediate images to debug_output/",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Maximum retry attempts (default: from config)",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=None,
        help="Minimum confidence threshold (default: from config)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--instance",
        type=str,
        default="",
        help="Instance ID for multi-run isolation (separate browser profile, log, config)",
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=None,
        help="Launch N parallel instances (1..N), each with auto-assigned instance ID",
    )
    parser.add_argument(
        "--billing-cycle",
        type=str,
        default=None,
        choices=["monthly", "quarterly", "yearly"],
        help="Billing cycle: monthly/quarterly/yearly (default: from config or quarterly)",
    )
    parser.add_argument(
        "--start-time",
        type=str,
        default=None,
        help="Scheduled click time in HH:MM:SS format (overrides config schedule.start_time)",
    )

    args = parser.parse_args()

    if args.test_mode:
        setup_logging(args.log_level, instance=args.instance)
        run_offline_test(debug=args.debug)
        return

    if args.instances is not None and args.instances > 0:
        setup_logging(args.log_level)
        logger.info(f"Launching {args.instances} parallel instances...")
        multiprocessing.freeze_support()
        processes = []
        for i in range(1, args.instances + 1):
            p = multiprocessing.Process(
                target=_worker,
                args=(str(i), args),
                name=f"instance-{i}",
                daemon=True,
            )
            p.start()
            logger.info(f"Instance {i} started (PID={p.pid})")
            time.sleep(3)
            processes.append(p)

        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            logger.info("Received Ctrl+C, terminating all instances...")
            for p in processes:
                p.terminate()
            for p in processes:
                p.join(timeout=5)
        logger.info("All instances exited")
    else:
        _worker(args.instance, args)


if __name__ == "__main__":
    main()