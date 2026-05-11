import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate

from loguru import logger


def send_notification(config: dict, plan: str = "", amount: str = "") -> bool:
    notif = config.get("notification", {})
    if not notif.get("enabled", False):
        logger.info("Email notification disabled")
        return False

    smtp_host = notif.get("smtp_host", "smtp.qq.com")
    smtp_port = notif.get("smtp_port", 465)
    sender = notif.get("sender", "")
    password = notif.get("password", "")
    receiver = notif.get("receiver", "")

    if not all([sender, password, receiver]):
        logger.warning("Email notification config incomplete, skipping")
        return False

    subject = "GLM Coding 抢购成功!"
    body_parts = ["GLM Coding Plan 抢购成功！\n"]
    if plan:
        body_parts.append(f"套餐: {plan}")
    if amount:
        body_parts.append(f"金额: {amount}")
    body_parts.append("\n请尽快前往页面完成支付！")

    msg = MIMEText("\n".join(body_parts), "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [receiver], msg.as_string())
            server.quit()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [receiver], msg.as_string())
            server.quit()
        logger.success(f"Notification email sent to {receiver}")
        return True
    except Exception as e:
        logger.error(f"Failed to send notification email: {e}")
        return False
