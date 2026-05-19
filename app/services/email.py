"""Transactional email service with the 12 BRD templates.

Architecture:
- `send_template(...)` is the single entrypoint. It renders, sends, and writes
  a `NotificationLog` row for audit + idempotency (via `dedupe_key`).
- Resend HTTPS API when SMTP_HOST points at Resend, otherwise SMTP submission.
  Most PaaS hosts (Railway included) block outbound SMTP ports (25/465/587)
  to deter spam, so going over HTTPS port 443 to api.resend.com is the only
  reliable channel from a hobby-tier container.
- stdout fallback for dev (no creds = print to console).
- Templates are pure Python f-strings - swap to Jinja2 if templates grow.
"""
from __future__ import annotations

import json
import smtplib
import threading
import traceback
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.misc import NotificationLog

settings = get_settings()


# ───────────────────────── Transport ─────────────────────────

def _send_via_smtp(*, to: str, subject: str, html: str, text: Optional[str]) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or "Please use an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")
    # 10s timeout - Resend usually responds in <1s; anything more is a hang.
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def _send_via_resend_http(*, to: str, subject: str, html: str, text: Optional[str]) -> None:
    """Send through Resend's HTTPS REST API.

    Reuses the SMTP_PASSWORD env var as the Resend API key (it's the same
    secret either way) and SMTP_FROM as the sender. Port 443 is open on
    every PaaS plan so this works where SMTP submission is blocked.
    """
    payload: dict = {
        "from": settings.smtp_from,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.smtp_password}",
            "Content-Type": "application/json",
            # Cloudflare (in front of api.resend.com) blocks the default
            # urllib UA (Python-urllib/x.y) as automated tooling, returning
            # 403 with error code 1010. A descriptive UA bypasses the check.
            "User-Agent": "jaratrade-api/1.0 (+https://api.jaratrade.com)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Resend HTTP {e.code}: {body[:300]}") from e


def _send_quiet(
    *,
    to: str,
    subject: str,
    html: str,
    text: Optional[str],
    log_id: Optional[str] = None,
) -> None:
    """Thread target: dispatch via the right transport for the configured
    provider. If `log_id` is set, write the real success/failure back to
    the NotificationLog row when the thread is done - this is what lets
    the dedupe logic in `send_template` distinguish "previously succeeded"
    from "previously failed and worth retrying"."""
    err_text: Optional[str] = None
    try:
        # Resend's SMTP submission gateway is blocked on most PaaS hosts -
        # prefer the HTTPS REST API when configured for Resend.
        if "resend" in (settings.smtp_host or "").lower():
            _send_via_resend_http(to=to, subject=subject, html=html, text=text)
        else:
            _send_via_smtp(to=to, subject=subject, html=html, text=text)
    except Exception:  # noqa: BLE001
        err_text = traceback.format_exc()
        traceback.print_exc()

    # Best-effort writeback. Daemon thread can't reuse the request session
    # (it's already closed), so open a fresh one.
    if log_id is not None:
        try:
            from ..database import SessionLocal  # local import to avoid cycles
            with SessionLocal() as wdb:
                row = wdb.get(NotificationLog, log_id)
                if row is not None:
                    row.success = err_text is None
                    if err_text:
                        row.error = err_text[:2000]
                    wdb.commit()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def send_email(*, to: str, subject: str, html: str, text: Optional[str] = None) -> None:
    """Low-level send. Prefer `send_template` so logs/idempotency apply.

    SMTP runs in a background daemon thread so a 10-second Resend roundtrip
    doesn't block API responses (POST /imp/order was waiting ~25-45s in
    prod because it sends two confirmation emails synchronously).
    """
    if not settings.smtp_host or not settings.smtp_user:
        print(f"\n[EMAIL DEV] to={to}  subject={subject}\n{(text or html)[:600]}\n")
        return
    threading.Thread(
        target=_send_quiet,
        kwargs={"to": to, "subject": subject, "html": html, "text": text},
        daemon=True,
        name=f"mail-{subject[:30]}",
    ).start()


# ───────────────────────── Logging + idempotency ─────────────────────────

def send_template(
    db: Session,
    *,
    template: str,
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    user_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> bool:
    """Send + log. Returns True on send, False if deduped (prior success).

    Pass a stable `dedupe_key` (e.g. `invoice:user_id:2026-05`) to make sends
    idempotent across retries.

    Dedupe semantics: we skip if a prior send with the same key was a
    confirmed success. If a prior send failed (e.g. SMTP outage), the failed
    log row is deleted so the retry can write a fresh row - otherwise the
    column's unique constraint would forever block the retry.
    """
    if dedupe_key:
        existing = (
            db.query(NotificationLog)
            .filter(NotificationLog.dedupe_key == dedupe_key)
            .first()
        )
        if existing is not None:
            if existing.success:
                return False
            # Previous attempt failed - clear it so the new attempt isn't
            # blocked by the unique constraint on dedupe_key.
            db.delete(existing)
            db.flush()

    log = NotificationLog(
        user_id=user_id,
        template=template,
        channel="email",
        to_address=to,
        subject=subject,
        dedupe_key=dedupe_key,
        success=True,  # optimistic; daemon thread writes the real outcome
    )
    db.add(log)
    try:
        db.commit()
    except IntegrityError:
        # Another concurrent send beat us to the dedupe key.
        db.rollback()
        return False
    db.refresh(log)

    # Dispatch via the background thread, telling it which row to update
    # with the real success/failure when the send completes.
    if not settings.smtp_host or not settings.smtp_user:
        print(f"\n[EMAIL DEV] to={to}  subject={subject}\n{(text or html)[:600]}\n")
        return True
    threading.Thread(
        target=_send_quiet,
        kwargs={
            "to": to,
            "subject": subject,
            "html": html,
            "text": text,
            "log_id": log.id,
        },
        daemon=True,
        name=f"mail-{subject[:30]}",
    ).start()
    return True


# ───────────────────────── Template helpers ─────────────────────────
# Each function returns (subject, html). All copy adapted from the BRD.

def _wrap(body: str) -> str:
    return f"""<!doctype html><html><body style="font-family:system-ui,Arial,sans-serif;color:#1a1a1a;max-width:560px;margin:auto;padding:24px;line-height:1.6">{body}<hr style="margin:32px 0;border:none;border-top:1px solid #e5e7eb"><p style="font-size:12px;color:#6b7280">Jaratrade Ltd</p></body></html>"""


def t_welcome_verify(name: str, verify_link: str, code: str = "") -> tuple[str, str]:
    """Verification email. Renders both a click-to-verify button (link) and
    the raw code, because the /auth/verify-email page offers a paste-the-code
    fallback for cross-device flows (e.g. read email on phone, verify on
    desktop). Without showing the code, that fallback was a dead UI promise.
    """
    subject = "Welcome to Jaratrade! Please verify your email"
    code_block = f"""
    <p style="margin-top:18px;font-size:14px;color:#374151">Or if the button doesn't work, paste this code into the verification page:</p>
    <p style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;background:#f3f4f6;padding:10px 14px;border-radius:6px;border:1px solid #e5e7eb;word-break:break-all;display:inline-block">{code}</p>""" if code else ""
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Thank you for registering with Jaratrade! To complete your registration, please verify your email by clicking the link below:</p>
    <p><a href="{verify_link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Verify my email</a></p>
    {code_block}
    <p>If you didn't sign up, you can safely ignore this message.</p>
    <p>Best regards,<br>The Jaratrade Team</p>"""
    return subject, _wrap(body)


def t_account_under_review(name: str) -> tuple[str, str]:
    subject = "Your Jaratrade account is under review"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Thank you for registering your business with Jaratrade. Your account is currently under review for verification. We'll notify you once it's been activated.</p>
    <p>This usually takes 1-2 business days.</p>
    <p>Best regards,<br>The Jaratrade Team</p>"""
    return subject, _wrap(body)


def t_new_exporter_pending_review_admin(
    business_name: str,
    business_email: str,
    contact_name: str,
    contact_email: str,
    review_link: str,
) -> tuple[str, str]:
    """Admin-side alert for a fresh exporter signup that needs KYC review."""
    subject = f"New exporter pending KYC review: {business_name or contact_name or 'unknown'}"
    body = f"""<p>A new exporter just signed up and is awaiting KYC review.</p>
    <table style="border-collapse:collapse;margin-top:8px">
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Business</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{business_name or '(not yet provided)'}</td></tr>
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Business email</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{business_email or '(not yet provided)'}</td></tr>
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Contact</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{contact_name or '(not provided)'}</td></tr>
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Contact email</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{contact_email or '(not provided)'}</td></tr>
    </table>
    <p style="margin-top:18px"><a href="{review_link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Open KYC queue</a></p>
    <p>If the exporter hasn't completed their business profile yet, the KYC review screen will show them as pending. Their docs land as they fill them in.</p>"""
    return subject, _wrap(body)


def t_account_activated(name: str, login_link: str) -> tuple[str, str]:
    subject = "Your Jaratrade account is now active!"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Congratulations! Your Jaratrade account has been successfully activated. You can now start trading.</p>
    <p><a href="{login_link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Log in</a></p>
    <p>Best regards,<br>The Jaratrade Team</p>"""
    return subject, _wrap(body)


def t_account_rejected(name: str, reason: str) -> tuple[str, str]:
    subject = "An update on your Jaratrade application"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Thank you for applying to Jaratrade. Unfortunately we weren't able to approve your account at this time:</p>
    <blockquote style="border-left:3px solid #d1d5db;padding-left:12px;color:#4b5563">{reason}</blockquote>
    <p>You can update your details and re-apply, or reach out to support if you'd like to discuss.</p>
    <p>Best regards,<br>The Jaratrade Team</p>"""
    return subject, _wrap(body)


def t_password_reset(name: str, reset_link: str) -> tuple[str, str]:
    subject = "Reset your Jaratrade password"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>You requested to reset your password. Click the link below to choose a new one:</p>
    <p><a href="{reset_link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Reset password</a></p>
    <p>This link expires in 30 minutes. If you didn't request this, ignore this email.</p>"""
    return subject, _wrap(body)


def t_order_placed_buyer(name: str, order_no: str, total: str, link: str) -> tuple[str, str]:
    subject = f"Order confirmation - {order_no}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Thank you for your order! Here are the details:</p>
    <p><strong>Order number:</strong> {order_no}<br>
    <strong>Total:</strong> {total}</p>
    <p><a href="{link}">Track your order</a></p>
    <p>Next steps: we'll notify the exporter to confirm and prepare your shipment.</p>"""
    return subject, _wrap(body)


def t_order_received_seller(name: str, order_no: str, importer_name: str, total: str, link: str) -> tuple[str, str]:
    subject = f"New order received - {order_no}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>You've received a new order from <strong>{importer_name}</strong>.</p>
    <p><strong>Order number:</strong> {order_no}<br>
    <strong>Total:</strong> {total}</p>
    <p><a href="{link}">View order</a></p>
    <p>Please confirm and prepare for shipment.</p>"""
    return subject, _wrap(body)


def t_order_status_update(name: str, order_no: str, status: str, extra: str = "") -> tuple[str, str]:
    nice = {
        "paid": "Your payment has been confirmed.",
        "shipped": "Your order has been shipped.",
        "delivered": "Your order has been delivered.",
        "cancelled": "Your order has been cancelled.",
    }.get(status, f"Your order status changed to {status}.")
    subject = f"Order {order_no} - {status}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>{nice}</p>
    {f'<p>{extra}</p>' if extra else ''}
    <p><strong>Order number:</strong> {order_no}</p>"""
    return subject, _wrap(body)


def t_payment_invoice(name: str, order_no: str, total: str, paid_on: str) -> tuple[str, str]:
    subject = f"Payment receipt - {order_no}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Thank you for your payment. Here is your receipt:</p>
    <table style="border-collapse:collapse;margin-top:8px">
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Order</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{order_no}</td></tr>
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Total</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{total}</td></tr>
      <tr><td style="padding:6px 12px;border:1px solid #e5e7eb">Paid on</td><td style="padding:6px 12px;border:1px solid #e5e7eb">{paid_on}</td></tr>
    </table>"""
    return subject, _wrap(body)


def t_transaction_limit_warning(name: str, percent_used: int, plan_name: str) -> tuple[str, str]:
    subject = "You're approaching your free-plan limit"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>You've used <strong>{percent_used}%</strong> of your {plan_name} monthly transaction limit.</p>
    <p>Consider upgrading to Premium for unlimited transactions and priority support.</p>"""
    return subject, _wrap(body)


def t_review_prompt(name: str, exporter_name: str, link: str) -> tuple[str, str]:
    subject = "Tell us about your experience"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>We hope your recent order from {exporter_name} met your expectations. Could you take a moment to leave a rating and review?</p>
    <p><a href="{link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Leave a review</a></p>"""
    return subject, _wrap(body)


def t_review_received(name: str, link: str) -> tuple[str, str]:
    subject = "You've received new feedback"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>You've received new feedback on a recent order. <a href="{link}">View it here</a>.</p>"""
    return subject, _wrap(body)


def t_account_updated(name: str, what: str) -> tuple[str, str]:
    subject = "Your Jaratrade account was updated"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>For your security, we're letting you know your <strong>{what}</strong> was updated.</p>
    <p>If this wasn't you, please contact support immediately.</p>"""
    return subject, _wrap(body)


def t_2fa_enabled(name: str) -> tuple[str, str]:
    subject = "Two-factor authentication enabled"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Two-factor authentication has been enabled on your Jaratrade account. From now on, you'll need a code from your authenticator app to log in.</p>
    <p>If you didn't enable this, contact support immediately.</p>"""
    return subject, _wrap(body)


def t_subscription_payment_confirmed(name: str, plan_title: str, amount: str, period_end: str) -> tuple[str, str]:
    subject = "Subscription payment confirmed"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Your subscription payment of <strong>{amount}</strong> for the <strong>{plan_title}</strong> plan has been processed.</p>
    <p>You're all set until <strong>{period_end}</strong>.</p>"""
    return subject, _wrap(body)


def t_subscription_renewal_reminder(name: str, plan_title: str, period_end: str) -> tuple[str, str]:
    subject = "Subscription renewal reminder"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Just a reminder, your <strong>{plan_title}</strong> plan renews on <strong>{period_end}</strong>.</p>
    <p>If your payment method needs an update, please log in and refresh it.</p>"""
    return subject, _wrap(body)


def t_subscription_cancelled(name: str, plan_title: str, period_end: str) -> tuple[str, str]:
    subject = "Subscription cancelled"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>You've cancelled your <strong>{plan_title}</strong> auto-renewal. You'll keep premium access until <strong>{period_end}</strong>, after which your account will move to the free plan.</p>
    <p>You can reactivate at any time from your subscription settings.</p>"""
    return subject, _wrap(body)


def t_subscription_expired(name: str, plan_title: str) -> tuple[str, str]:
    subject = "Your premium plan has expired"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Your <strong>{plan_title}</strong> plan has expired and your account is now on the free tier.</p>
    <p>You can upgrade again any time from your subscription settings.</p>"""
    return subject, _wrap(body)


def t_subscription_renewed(name: str, plan_title: str, amount: str, period_end: str, last4: str) -> tuple[str, str]:
    subject = f"Subscription renewed - {plan_title}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Your <strong>{plan_title}</strong> subscription was renewed for <strong>{amount}</strong>.</p>
    <p>We charged the card ending in <strong>{last4 or 'on file'}</strong>. You're set until <strong>{period_end}</strong>.</p>"""
    return subject, _wrap(body)


def t_subscription_renewal_failed(name: str, plan_title: str, attempts: int, manage_link: str) -> tuple[str, str]:
    subject = f"Action needed - couldn't renew your {plan_title} subscription"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>We tried to renew your <strong>{plan_title}</strong> subscription but the charge failed (attempt {attempts}).</p>
    <p>Update your payment method to keep premium access:</p>
    <p><a href="{manage_link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Update card</a></p>
    <p>If we can't process the renewal after three attempts, your account will move to the free tier.</p>"""
    return subject, _wrap(body)


def t_inventory_stale_reminder(name: str, count: int, manage_link: str) -> tuple[str, str]:
    subject = "Confirm your stock - keep search ranking"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>You have <strong>{count}</strong> product{'s' if count != 1 else ''} that haven't been updated in over a week.</p>
    <p>Confirm stock (or update prices) to stay prioritised in search results:</p>
    <p><a href="{manage_link}" style="display:inline-block;background:#2563eb;color:white;padding:10px 18px;border-radius:6px;text-decoration:none">Review products</a></p>"""
    return subject, _wrap(body)


def t_dispute_raised_buyer(name: str, order_no: str, reason: str) -> tuple[str, str]:
    subject = f"Dispute received for order {order_no}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>We've received your dispute on order <strong>{order_no}</strong>.</p>
    <p><strong>Reason:</strong> {reason}</p>
    <p>Our support team will review within 24 hours and email you with next steps.</p>"""
    return subject, _wrap(body)


def t_dispute_raised_seller(name: str, order_no: str, importer_name: str, reason: str) -> tuple[str, str]:
    subject = f"Dispute filed on order {order_no}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>{importer_name} has raised a dispute on order <strong>{order_no}</strong>.</p>
    <p><strong>Reason:</strong> {reason}</p>
    <p>Our team is investigating and will be in touch if we need anything from you.</p>"""
    return subject, _wrap(body)


def t_dispute_resolved_buyer(name: str, order_no: str, resolution: str, amount: str = "") -> tuple[str, str]:
    nice = {
        "refund": f"We've issued a refund of <strong>{amount}</strong> to your original payment method. It may take 3-7 business days to appear.",
        "replacement": "A replacement shipment is being arranged. You'll receive tracking details once the exporter dispatches.",
        "dismissed": "After review, we weren't able to grant a refund or replacement on this dispute.",
    }.get(resolution, f"Resolution: {resolution}.")
    subject = f"Dispute resolved for order {order_no}"
    body = f"""<p>Hello {name or 'there'},</p>
    <p>Your dispute on order <strong>{order_no}</strong> has been resolved.</p>
    <p>{nice}</p>"""
    return subject, _wrap(body)


# ───────────────────────── Legacy aliases (backward-compat) ─────────────────────────

def verification_email(name: str, link: str) -> str:
    """Old call-site helper - returns just the HTML."""
    return t_welcome_verify(name, link)[1]


def password_reset_email(name: str, link: str) -> str:
    return t_password_reset(name, link)[1]
