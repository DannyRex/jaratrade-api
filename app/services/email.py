"""Transactional email service with the 12 BRD templates.

Architecture:
- `send_template(...)` is the single entrypoint. It renders, sends, and writes
  a `NotificationLog` row for audit + idempotency (via `dedupe_key`).
- Resend HTTPS API when SMTP_HOST points at Resend, otherwise SMTP submission.
  Most PaaS hosts (Railway included) block outbound SMTP ports (25/465/587)
  to deter spam, so going over HTTPS port 443 to api.resend.com is the only
  reliable channel from a hobby-tier container.
- stdout fallback for dev (no creds = print to console).
- Templates are hand-rolled, email-client-safe HTML (see the template section).
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


# ════════════════════════ Template system ════════════════════════
# Hand-rolled, email-client-safe HTML. Rules followed throughout:
#   - table-based layout only (Outlook / Gmail ignore flexbox & grid)
#   - every style inlined (Gmail strips <style> blocks in <head>)
#   - one 600px-wide centred card, web-safe font stack, light theme
# Brand palette is lifted straight from the app's logo glyph.

_NAVY = "#0b1a3f"      # brand navy - header / wordmark backdrop
_COBALT = "#1d4ed8"    # brand cobalt - buttons, links, accents
_SKY = "#60a5fa"       # brand sky - wordmark highlight
_AMBER = "#fb923c"     # accent amber - "next steps" notes
_INK = "#0f172a"       # near-black body text
_BODY = "#374151"      # default paragraph grey
_MUTED = "#6b7280"     # secondary / labels
_FAINT = "#9ca3af"     # footer fine print
_LINE = "#e5e7eb"      # hairline borders
_PAGE = "#eef1f5"      # page background behind the card
_TINT = "#f4f6fb"      # cobalt-tinted panel fill
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

_SITE = (getattr(settings, "site_url", "") or "https://jaratrade.com").rstrip("/")
_SYMBOL = {"NGN": "&#8358;", "GBP": "&#163;", "USD": "$"}


def _money(amount, currency: str = "NGN") -> str:
    """Format a monetary amount with the right currency symbol."""
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    sym = _SYMBOL.get((currency or "").upper())
    return f"{sym}{n:,.2f}" if sym else f"{n:,.2f} {currency}".strip()


# ───────────────────────── Layout shell ─────────────────────────

def _header() -> str:
    return f"""<tr><td style="background:{_NAVY};padding:24px 40px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="font-family:{_FONT};font-size:23px;font-weight:800;letter-spacing:-0.5px;color:#ffffff;">Jara<span style="color:{_SKY};">trade</span></td>
<td align="right" style="font-family:{_FONT};font-size:10px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:#8ea3cc;">Nigeria &#8594; UK Trade</td>
</tr></table></td></tr>
<tr><td style="height:3px;background:{_COBALT};line-height:3px;font-size:0;">&nbsp;</td></tr>"""


def _footer() -> str:
    return f"""<tr><td style="padding:26px 40px 32px 40px;font-family:{_FONT};border-top:1px solid {_LINE};background:#fbfbfc;">
<p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:{_INK};">Jaratrade</p>
<p style="margin:0 0 14px 0;font-size:12px;line-height:1.6;color:{_MUTED};">The B2B marketplace connecting Nigerian exporters with UK importers - with funds held securely until goods are received.</p>
<p style="margin:0;font-size:11px;line-height:1.7;color:{_FAINT};">You're receiving this transactional email because of activity on your Jaratrade account.<br>
&copy; Jaratrade Ltd &nbsp;&middot;&nbsp; <a href="{_SITE}" style="color:{_MUTED};text-decoration:underline;">jaratrade.com</a> &nbsp;&middot;&nbsp; <a href="{_SITE}/contact" style="color:{_MUTED};text-decoration:underline;">Contact support</a></p>
</td></tr>"""


def _layout(content: str, *, preheader: str = "") -> str:
    """Wrap rendered content in the full branded email shell."""
    pre = ""
    if preheader:
        pre = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
               f'mso-hide:all;">{preheader}{"&#8203;&nbsp;" * 40}</div>')
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<title>Jaratrade</title>
</head>
<body style="margin:0;padding:0;background:{_PAGE};-webkit-text-size-adjust:100%;">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{_PAGE};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {_LINE};">
{_header()}
<tr><td style="padding:38px 40px 26px 40px;font-family:{_FONT};">
{content}
</td></tr>
{_footer()}
</table>
</td></tr>
</table>
</body></html>"""


# ───────────────────────── Building blocks ─────────────────────────

def _eyebrow(text: str) -> str:
    return f'<p style="margin:0 0 8px 0;font-size:15px;color:{_BODY};font-family:{_FONT};">{text}</p>'


def _h1(text: str) -> str:
    return (f'<h1 style="margin:0 0 14px 0;font-size:26px;line-height:1.25;'
            f'font-weight:800;letter-spacing:-0.5px;color:{_INK};font-family:{_FONT};">{text}</h1>')


def _p(text: str, *, muted: bool = False, size: int = 15) -> str:
    color = _MUTED if muted else _BODY
    return (f'<p style="margin:0 0 16px 0;font-size:{size}px;line-height:1.65;'
            f'color:{color};font-family:{_FONT};">{text}</p>')


def _button(label: str, href: str) -> str:
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 24px 0;"><tr>
<td style="border-radius:9px;background:{_COBALT};">
<a href="{href}" style="display:inline-block;padding:13px 30px;font-family:{_FONT};font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:9px;">{label}</a>
</td></tr></table>"""


def _panel(label: str, value: str, *, sub: str = "") -> str:
    """Highlight card: small uppercase label, big value, optional sub-line."""
    sub_html = (f'<p style="margin:8px 0 0 0;font-size:13px;color:{_MUTED};'
                f'font-family:{_FONT};">{sub}</p>') if sub else ""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 24px 0;">
<tr><td style="background:{_TINT};border:1px solid #e3e8f3;border-radius:12px;padding:20px 22px;">
<p style="margin:0;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:{_COBALT};font-family:{_FONT};">{label}</p>
<p style="margin:6px 0 0 0;font-size:24px;font-weight:800;letter-spacing:-0.3px;color:{_INK};font-family:{_FONT};">{value}</p>
{sub_html}</td></tr></table>"""


def _kv(label: str, value: str) -> str:
    """A stacked label / value line inside a detail column."""
    return (f'<p style="margin:0 0 13px 0;font-family:{_FONT};">'
            f'<span style="display:block;font-size:11px;color:{_MUTED};text-transform:uppercase;letter-spacing:0.5px;">{label}</span>'
            f'<span style="display:block;font-size:14px;color:{_INK};font-weight:600;margin-top:3px;">{value}</span></p>')


def _detail_columns(left_title: str, left_html: str, right_title: str, right_html: str) -> str:
    """Two side-by-side detail blocks (Delivery details / Order details)."""
    def head(t: str) -> str:
        return (f'<p style="margin:0 0 12px 0;font-size:12px;font-weight:700;letter-spacing:0.8px;'
                f'text-transform:uppercase;color:{_INK};font-family:{_FONT};'
                f'border-bottom:2px solid {_LINE};padding-bottom:8px;">{t}</p>')
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:10px 0 22px 0;">
<tr>
<td valign="top" width="50%" style="padding-right:14px;">{head(left_title)}{left_html}</td>
<td valign="top" width="50%" style="padding-left:14px;">{head(right_title)}{right_html}</td>
</tr></table>"""


def _section_title(text: str) -> str:
    return (f'<p style="margin:24px 0 4px 0;font-size:12px;font-weight:700;letter-spacing:0.8px;'
            f'text-transform:uppercase;color:{_INK};font-family:{_FONT};'
            f'border-bottom:2px solid {_LINE};padding-bottom:8px;">{text}</p>')


def _line_items(items: list, currency: str) -> str:
    rows = ""
    for it in items:
        name = it.get("name", "Item")
        qty = it.get("quantity", 1)
        unit = it.get("unit_price", 0)
        sub = it.get("subtotal", 0)
        rows += f"""<tr>
<td style="padding:14px 0;border-top:1px solid {_LINE};font-family:{_FONT};">
<span style="font-size:14px;font-weight:700;color:{_INK};">{name}</span><br>
<span style="font-size:12px;color:{_MUTED};">Qty {qty} &times; {_money(unit, currency)}</span></td>
<td align="right" valign="top" style="padding:14px 0;border-top:1px solid {_LINE};font-family:{_FONT};font-size:14px;font-weight:700;color:{_INK};white-space:nowrap;">{_money(sub, currency)}</td>
</tr>"""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin-bottom:4px;">{rows}</table>')


def _totals(rows: list, currency: str) -> str:
    """rows: list of (label, amount, is_grand_total)."""
    out = ""
    for label, amount, grand in rows:
        if grand:
            out += (f'<tr><td style="padding:14px 0 0 0;border-top:2px solid {_INK};font-family:{_FONT};'
                    f'font-size:15px;font-weight:800;color:{_INK};">{label}</td>'
                    f'<td align="right" style="padding:14px 0 0 0;border-top:2px solid {_INK};font-family:{_FONT};'
                    f'font-size:18px;font-weight:800;color:{_INK};">{_money(amount, currency)}</td></tr>')
        else:
            out += (f'<tr><td style="padding:6px 0;font-family:{_FONT};font-size:13px;color:{_MUTED};">{label}</td>'
                    f'<td align="right" style="padding:6px 0;font-family:{_FONT};font-size:13px;color:{_INK};">'
                    f'{_money(amount, currency)}</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:8px 0 22px 0;">{out}</table>')


def _note(text: str) -> str:
    """Soft amber-flagged box for 'what happens next' style guidance."""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 14px 0;">
<tr><td style="background:#fdfaf5;border:1px solid #f3e6d2;border-left:3px solid {_AMBER};border-radius:8px;padding:14px 16px;font-family:{_FONT};font-size:13px;line-height:1.65;color:#7a6034;">{text}</td></tr></table>"""


def _info_rows(pairs: list) -> str:
    """A simple stacked key/value list (single column)."""
    return "".join(_kv(k, v) for k, v in pairs)


def _delivery_html(delivery: Optional[dict]) -> str:
    """Render a delivery address from whatever shape checkout stored."""
    delivery = delivery or {}
    name = delivery.get("name") or delivery.get("recipient") or delivery.get("full_name") or ""
    order_keys = [
        ("address", "address_line1", "line1", "street"),
        ("address_line2", "line2", "apartment"),
        ("city", "town"),
        ("state", "region", "county"),
        ("postcode", "postal_code", "zip", "zipcode"),
        ("country",),
    ]
    parts: list = []
    for group in order_keys:
        for k in group:
            v = delivery.get(k)
            if v:
                parts.append(str(v))
                break
    if not parts:
        # Fallback: render any plain scalar values we can find.
        for k, v in delivery.items():
            if k != "raw" and isinstance(v, (str, int, float)) and str(v).strip():
                parts.append(str(v))
    lines = ([f'<strong style="color:{_INK};">{name}</strong>'] if name else []) + parts
    if not lines:
        return f'<p style="margin:0;font-size:13px;color:{_MUTED};font-family:{_FONT};">As provided at checkout.</p>'
    return (f'<p style="margin:0;font-size:13px;line-height:1.75;color:{_BODY};'
            f'font-family:{_FONT};">' + "<br>".join(lines) + "</p>")


# ════════════════════════ Templates ════════════════════════
# Each function returns (subject, html). Copy adapted from the BRD.

def t_welcome_verify(name: str, verify_link: str, code: str = "") -> tuple[str, str]:
    """Verification email. Renders both a click-to-verify button and the raw
    code, because the verify-email page offers a paste-the-code fallback for
    cross-device flows (read email on phone, verify on desktop)."""
    subject = "Verify your email to get started on Jaratrade"
    code_block = ""
    if code:
        code_block = (
            _p("Or paste this code into the verification page if the button doesn't work:", muted=True, size=13)
            + f'<p style="margin:0 0 18px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
              f'font-size:18px;font-weight:700;letter-spacing:3px;background:{_TINT};color:{_INK};'
              f'padding:14px 18px;border-radius:8px;border:1px solid #e3e8f3;display:inline-block;">{code}</p>'
        )
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Welcome to Jaratrade.")
        + _p("You're one click away. Confirm your email address to activate your account and start trading.")
        + _button("Verify my email", verify_link)
        + code_block
        + _p("If you didn't create a Jaratrade account, you can safely ignore this email.", muted=True, size=13)
    )
    return subject, _layout(content, preheader="Confirm your email to activate your Jaratrade account.")


def t_account_under_review(name: str) -> tuple[str, str]:
    subject = "Your Jaratrade account is under review"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your account is under review.")
        + _p("Thanks for registering your business with Jaratrade. Our team is verifying your details - this usually takes 1-2 business days.")
        + _panel("Status", "Under review", sub="We'll email you the moment it's activated.")
        + _p("Nothing more is needed from you right now. Hang tight.", muted=True)
    )
    return subject, _layout(content, preheader="Your Jaratrade account is being verified.")


def t_new_exporter_pending_review_admin(
    business_name: str,
    business_email: str,
    contact_name: str,
    contact_email: str,
    review_link: str,
) -> tuple[str, str]:
    """Admin-side alert for a fresh exporter signup that needs KYC review."""
    subject = f"New exporter pending KYC review: {business_name or contact_name or 'unknown'}"
    rows = _info_rows([
        ("Business", business_name or "(not yet provided)"),
        ("Business email", business_email or "(not yet provided)"),
        ("Contact", contact_name or "(not provided)"),
        ("Contact email", contact_email or "(not provided)"),
    ])
    content = (
        _h1("New exporter awaiting review.")
        + _p("A new exporter just signed up and is queued for KYC review.")
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 18px 0;"><tr><td style="background:{_TINT};border:1px solid #e3e8f3;border-radius:12px;padding:18px 22px;">{rows}</td></tr></table>'
        + _button("Open KYC queue", review_link)
        + _p("If the exporter hasn't finished their business profile yet, they'll show as pending - their documents land as they fill them in.", muted=True, size=13)
    )
    return subject, _layout(content, preheader="An exporter is waiting for KYC review.")


def t_account_activated(name: str, login_link: str) -> tuple[str, str]:
    subject = "Your Jaratrade account is now active"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("You're verified - welcome aboard.")
        + _p("Your Jaratrade account has been approved and is now fully active. You can log in and start trading right away.")
        + _button("Log in to Jaratrade", login_link)
        + _note("<strong>Tip:</strong> complete your storefront and product listings to start appearing in buyer searches.")
    )
    return subject, _layout(content, preheader="Your Jaratrade account has been approved.")


def t_account_rejected(name: str, reason: str) -> tuple[str, str]:
    subject = "An update on your Jaratrade application"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("An update on your application.")
        + _p("Thanks for applying to Jaratrade. We weren't able to approve your account at this time:")
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 18px 0;"><tr><td style="background:#fdf2f2;border:1px solid #f4d4d4;border-left:3px solid #dc2626;border-radius:8px;padding:14px 16px;font-family:{_FONT};font-size:14px;line-height:1.6;color:#7f1d1d;">{reason}</td></tr></table>'
        + _p("You're welcome to update your details and re-apply, or reach out to support if you'd like to talk it through.")
        + _button("Contact support", f"{_SITE}/contact")
    )
    return subject, _layout(content, preheader="An update on your Jaratrade application.")


def t_password_reset(name: str, reset_link: str) -> tuple[str, str]:
    subject = "Reset your Jaratrade password"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Reset your password.")
        + _p("We received a request to reset your Jaratrade password. Choose a new one with the button below.")
        + _button("Reset my password", reset_link)
        + _p("This link expires in 30 minutes. If you didn't request a reset, you can safely ignore this email - your password won't change.", muted=True, size=13)
    )
    return subject, _layout(content, preheader="Reset your Jaratrade password (link expires in 30 minutes).")


def t_order_placed_buyer(
    name: str,
    order_no: str,
    link: str,
    *,
    currency: str = "NGN",
    items: Optional[list] = None,
    subtotal: float = 0.0,
    logistics_fee: float = 0.0,
    platform_fee: float = 0.0,
    total: float = 0.0,
    delivery: Optional[dict] = None,
    order_date: str = "",
    shipping_mode: str = "",
) -> tuple[str, str]:
    """Rich buyer order confirmation: hero, order panel, delivery + order
    detail columns, line items and a full cost breakdown."""
    subject = f"Order confirmed - {order_no}"
    items = items or []
    ship_label = {"logistics": "Jaratrade logistics", "self": "Seller-arranged"}.get(
        (shipping_mode or "").lower(), (shipping_mode or "Logistics").title()
    )
    totals = [("Subtotal", subtotal, False)]
    if logistics_fee:
        totals.append(("Logistics", logistics_fee, False))
    totals.append(("Platform fee", platform_fee, False))
    totals.append(("Order total", total, True))
    order_details = _info_rows([
        ("Order date", order_date or "-"),
        ("Shipping", ship_label),
        ("Payment", "Awaiting payment"),
    ])
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Thank you for your order.")
        + _p("It's confirmed. We've shared it with the seller - you'll get an email the moment it ships.", muted=True)
        + _panel("Order number", order_no, sub=(f"Placed {order_date}" if order_date else ""))
        + _button("Track your order", link)
        + _detail_columns("Delivery details", _delivery_html(delivery), "Order details", order_details)
        + ((_section_title("Items") + _line_items(items, currency)) if items else "")
        + _totals(totals, currency)
        + _note("<strong>What happens next:</strong> the seller confirms and prepares your shipment. Your funds are held securely by Jaratrade and only released once you confirm the goods have arrived.")
    )
    return subject, _layout(content, preheader=f"Your order {order_no} is confirmed.")


def t_order_received_seller(
    name: str,
    order_no: str,
    importer_name: str,
    link: str,
    *,
    currency: str = "NGN",
    items: Optional[list] = None,
    subtotal: float = 0.0,
    logistics_fee: float = 0.0,
    platform_fee: float = 0.0,
    total: float = 0.0,
    order_date: str = "",
) -> tuple[str, str]:
    """Rich seller new-order notification."""
    subject = f"New order received - {order_no}"
    items = items or []
    totals = [("Subtotal", subtotal, False)]
    if logistics_fee:
        totals.append(("Logistics", logistics_fee, False))
    totals.append(("Platform fee", platform_fee, False))
    totals.append(("Order total", total, True))
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("You've got a new order.")
        + _p(f"<strong>{importer_name}</strong> just placed an order with you. Confirm it and start preparing the shipment.", muted=True)
        + _panel("Order number", order_no, sub=(f"Received {order_date}" if order_date else ""))
        + _button("View &amp; confirm order", link)
        + ((_section_title("Items") + _line_items(items, currency)) if items else "")
        + _totals(totals, currency)
        + _note("<strong>Reminder:</strong> ship promptly and mark the order as shipped. Your payout is released once the buyer confirms delivery, or 1 day after the order is delivered.")
    )
    return subject, _layout(content, preheader=f"{importer_name} placed order {order_no}.")


def t_order_status_update(name: str, order_no: str, status: str, extra: str = "") -> tuple[str, str]:
    headline = {
        "paid": "Payment confirmed",
        "confirmed": "Your order is confirmed",
        "preparing": "Your order is being prepared",
        "shipped": "Your order has shipped",
        "delivered": "Your order was delivered",
        "cancelled": "Your order was cancelled",
        "refunded": "Your order was refunded",
    }.get(status, "Order update")
    blurb = {
        "paid": "We've confirmed your payment. The seller has been notified to prepare your shipment.",
        "confirmed": "The seller has confirmed your order and will begin preparing it.",
        "preparing": "The seller is preparing your order for shipment.",
        "shipped": "Good news - your order is on its way.",
        "delivered": "Your order has been delivered. Please confirm receipt so the seller can be paid.",
        "cancelled": "Your order has been cancelled. Any payment made will be returned.",
        "refunded": "A refund has been issued to your original payment method.",
    }.get(status, f"Your order status changed to {status}.")
    subject = f"Order {order_no} - {status}"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1(headline)
        + _p(blurb, muted=True)
        + _panel("Order number", order_no, sub=f"Status: {status.title()}")
        + (_p(extra) if extra else "")
        + _button("View your order", f"{_SITE}/importer/orders")
    )
    return subject, _layout(content, preheader=blurb)


def t_payment_invoice(name: str, order_no: str, total: str, paid_on: str) -> tuple[str, str]:
    subject = f"Payment receipt - {order_no}"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Payment received.")
        + _p("Thank you - your payment has cleared. Here's your receipt.", muted=True)
        + _panel("Amount paid", str(total), sub=f"Order {order_no}")
        + _detail_columns(
            "Receipt",
            _info_rows([("Order number", order_no), ("Paid on", paid_on or "-")]),
            "Payment",
            _info_rows([("Method", "Card / bank transfer"), ("Status", "Successful")]),
        )
        + _note("Keep this email as proof of payment. The seller has been notified to prepare your shipment, and your funds are held securely until you confirm delivery.")
    )
    return subject, _layout(content, preheader=f"Receipt for order {order_no}.")


def t_transaction_limit_warning(name: str, percent_used: int, plan_name: str) -> tuple[str, str]:
    subject = "You're approaching your plan limit"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("You're approaching your plan limit.")
        + _panel("Monthly limit used", f"{percent_used}%", sub=f"On your {plan_name} plan")
        + _p("Upgrade to Premium for unlimited transactions, lower fees and priority support - so a busy month never slows you down.")
        + _button("View upgrade options", f"{_SITE}/pricing")
    )
    return subject, _layout(content, preheader=f"You've used {percent_used}% of your {plan_name} limit.")


def t_review_prompt(name: str, exporter_name: str, link: str) -> tuple[str, str]:
    subject = "How was your recent order?"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("How did we do?")
        + _p(f"We hope your recent order from <strong>{exporter_name}</strong> met your expectations. A quick rating helps other importers buy with confidence.")
        + _button("Leave a review", link)
        + _p("It takes less than a minute.", muted=True, size=13)
    )
    return subject, _layout(content, preheader=f"Rate your order from {exporter_name}.")


def t_review_received(name: str, link: str) -> tuple[str, str]:
    subject = "You've received new feedback"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("You've received new feedback.")
        + _p("A buyer just left a rating and review on one of your recent orders.")
        + _button("View your feedback", link)
    )
    return subject, _layout(content, preheader="A buyer left you a new review.")


def t_account_updated(name: str, what: str) -> tuple[str, str]:
    subject = "Your Jaratrade account was updated"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("A change to your account.")
        + _p(f"For your security, we're letting you know your <strong>{what}</strong> was just updated.")
        + _note("<strong>Didn't make this change?</strong> Contact support immediately so we can secure your account.")
        + _button("Contact support", f"{_SITE}/contact")
    )
    return subject, _layout(content, preheader=f"Your {what} was updated.")


def t_2fa_enabled(name: str) -> tuple[str, str]:
    subject = "Two-factor authentication enabled"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Two-factor authentication is on.")
        + _p("Your Jaratrade account is now protected with two-factor authentication. You'll enter a code from your authenticator app each time you log in.")
        + _note("<strong>Didn't enable this?</strong> Contact support immediately - someone may have access to your account.")
    )
    return subject, _layout(content, preheader="2FA is now active on your Jaratrade account.")


def t_subscription_payment_confirmed(name: str, plan_title: str, amount: str, period_end: str) -> tuple[str, str]:
    subject = "Subscription payment confirmed"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your subscription is active.")
        + _p(f"We've processed your payment for the <strong>{plan_title}</strong> plan. Thank you.")
        + _detail_columns(
            "Payment",
            _info_rows([("Plan", plan_title), ("Amount", str(amount))]),
            "Coverage",
            _info_rows([("Status", "Active"), ("Renews / ends", period_end)]),
        )
    )
    return subject, _layout(content, preheader=f"Your {plan_title} subscription is active.")


def t_subscription_renewal_reminder(name: str, plan_title: str, period_end: str) -> tuple[str, str]:
    subject = "Subscription renewal reminder"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your plan renews soon.")
        + _panel("Renews on", period_end, sub=f"{plan_title} plan")
        + _p("No action is needed if your payment method is up to date. To update your card, log in and visit your subscription settings.")
        + _button("Manage subscription", f"{_SITE}/settings/subscription")
    )
    return subject, _layout(content, preheader=f"Your {plan_title} plan renews on {period_end}.")


def t_subscription_cancelled(name: str, plan_title: str, period_end: str) -> tuple[str, str]:
    subject = "Subscription cancelled"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your auto-renewal is off.")
        + _p(f"You've cancelled auto-renewal for the <strong>{plan_title}</strong> plan. You'll keep full premium access until the date below, then move to the free plan.")
        + _panel("Premium access until", period_end)
        + _p("Changed your mind? You can reactivate any time from your subscription settings.", muted=True)
        + _button("Reactivate plan", f"{_SITE}/settings/subscription")
    )
    return subject, _layout(content, preheader=f"Premium access continues until {period_end}.")


def t_subscription_expired(name: str, plan_title: str) -> tuple[str, str]:
    subject = "Your premium plan has expired"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your premium plan has expired.")
        + _p(f"Your <strong>{plan_title}</strong> plan has ended and your account is now on the free tier. Some premium features are no longer available.")
        + _button("Upgrade again", f"{_SITE}/pricing")
    )
    return subject, _layout(content, preheader=f"Your {plan_title} plan has expired.")


def t_subscription_renewed(name: str, plan_title: str, amount: str, period_end: str, last4: str) -> tuple[str, str]:
    subject = f"Subscription renewed - {plan_title}"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your subscription was renewed.")
        + _p(f"We've renewed your <strong>{plan_title}</strong> plan. Thanks for staying with Jaratrade.")
        + _detail_columns(
            "Payment",
            _info_rows([("Amount", str(amount)), ("Card", f"ending {last4}" if last4 else "on file")]),
            "Coverage",
            _info_rows([("Plan", plan_title), ("Renews on", period_end)]),
        )
    )
    return subject, _layout(content, preheader=f"Your {plan_title} subscription renewed.")


def t_subscription_renewal_failed(name: str, plan_title: str, attempts: int, manage_link: str) -> tuple[str, str]:
    subject = f"Action needed - couldn't renew your {plan_title} subscription"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("We couldn't renew your subscription.")
        + _p(f"We tried to renew your <strong>{plan_title}</strong> plan but the charge didn't go through (attempt {attempts}).")
        + _p("Update your payment method to keep your premium access - it only takes a moment.")
        + _button("Update payment method", manage_link)
        + _note("If we can't process the renewal after three attempts, your account will move to the free tier.")
    )
    return subject, _layout(content, preheader=f"Update your card to keep your {plan_title} plan.")


def t_inventory_stale_reminder(name: str, count: int, manage_link: str) -> tuple[str, str]:
    subject = "Confirm your stock to keep your search ranking"
    plural = "s" if count != 1 else ""
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Time to refresh your listings.")
        + _panel("Needs attention", f"{count} product{plural}", sub="Not updated in over a week")
        + _p("Buyers prefer fresh listings, and so does search. Confirm stock or update prices to stay prioritised in results.")
        + _button("Review my products", manage_link)
    )
    return subject, _layout(content, preheader=f"{count} product{plural} need a stock update.")


def t_dispute_raised_buyer(name: str, order_no: str, reason: str) -> tuple[str, str]:
    subject = f"We've received your dispute - {order_no}"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("We've received your dispute.")
        + _p(f"Your dispute on order <strong>{order_no}</strong> has been logged. Our team will review it and email you with next steps.")
        + _panel("Dispute reason", str(reason).replace("_", " ").title(), sub=f"Order {order_no}")
        + _note("Our support team typically reviews disputes within 24 hours. Your funds remain held securely while we look into it.")
    )
    return subject, _layout(content, preheader=f"Your dispute on order {order_no} has been logged.")


def t_dispute_raised_seller(name: str, order_no: str, importer_name: str, reason: str) -> tuple[str, str]:
    subject = f"A dispute was filed on order {order_no}"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("A dispute was filed.")
        + _p(f"<strong>{importer_name}</strong> has raised a dispute on order <strong>{order_no}</strong>. Our team is reviewing it.")
        + _panel("Dispute reason", str(reason).replace("_", " ").title(), sub=f"Order {order_no}")
        + _note("There's nothing you need to do right now - we'll be in touch if we need information from you. The order's payout is paused until the dispute is resolved.")
    )
    return subject, _layout(content, preheader=f"{importer_name} disputed order {order_no}.")


def t_dispute_resolved_buyer(name: str, order_no: str, resolution: str, amount: str = "") -> tuple[str, str]:
    blurb = {
        "refund": f"We've issued a refund of <strong>{amount}</strong> to your original payment method. It can take 3-7 business days to appear.",
        "replacement": "A replacement shipment is being arranged. You'll receive tracking details once the seller dispatches it.",
        "dismissed": "After reviewing the evidence, we weren't able to grant a refund or replacement for this dispute.",
    }.get(resolution, f"Resolution: {resolution}.")
    subject = f"Your dispute has been resolved - {order_no}"
    content = (
        _eyebrow(f"Hi {name or 'there'},")
        + _h1("Your dispute has been resolved.")
        + _panel("Resolution", str(resolution).replace("_", " ").title(), sub=f"Order {order_no}")
        + _p(blurb)
        + _p("If you have any questions about this outcome, our support team is happy to help.", muted=True, size=13)
        + _button("Contact support", f"{_SITE}/contact")
    )
    return subject, _layout(content, preheader=f"Your dispute on order {order_no} has been resolved.")


# ───────────────────────── Legacy aliases (backward-compat) ─────────────────────────

def verification_email(name: str, link: str) -> str:
    """Old call-site helper - returns just the HTML."""
    return t_welcome_verify(name, link)[1]


def password_reset_email(name: str, link: str) -> str:
    return t_password_reset(name, link)[1]
