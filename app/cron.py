"""Scheduled jobs.

Run individually:
    python -m app.cron expire_subscriptions
    python -m app.cron renewal_reminders
    python -m app.cron all                    # run everything that's due

Wire to your scheduler of choice (systemd timer, Vercel Cron, Fly schedules,
GitHub Actions cron). Each task is idempotent.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import ExporterPlan, ImporterPlan, Subscription, User
from .services.email import (
    send_template,
    t_subscription_expired,
    t_subscription_renewal_reminder,
)

log = logging.getLogger("jaratrade.cron")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ───────────────────────── expire_subscriptions ─────────────────────────

def expire_subscriptions(db: Session) -> int:
    """Move active/cancelled subs whose period_end is past to `expired`,
    downgrade the user to the default free plan, and email them.

    Returns the number of users downgraded.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = (
        db.query(Subscription)
        .filter(
            Subscription.status.in_(["active", "cancelled"]),
            Subscription.period_end.isnot(None),
            Subscription.period_end < now,
        )
        .all()
    )
    downgraded = 0
    for sub in rows:
        sub.status = "expired"
        user = db.get(User, sub.user_id)
        if not user:
            continue
        # Downgrade only if their plan still matches this expired sub
        if user.plan_id == sub.plan_id:
            Model = ImporterPlan if sub.plan_role == "importer" else ExporterPlan
            default = db.query(Model).filter(Model.is_default == 1).first()
            user.plan_id = default.id if default else None
            user.plan_renewal_date = None
            user.plan_auto_renew = True
            downgraded += 1

            plan = db.get(Model, sub.plan_id)
            if plan:
                subject, html = t_subscription_expired(user.firstname or "there", plan.title)
                send_template(
                    db, template="subscription_expired",
                    to=user.email, subject=subject, html=html, user_id=user.id,
                    dedupe_key=f"sub_expired:{sub.id}",
                )
    db.commit()
    log.info("expire_subscriptions: %d expired, %d downgraded", len(rows), downgraded)
    return downgraded


# ───────────────────────── renewal_reminders ─────────────────────────

def renewal_reminders(db: Session, days_before: int = 3) -> int:
    """Email users whose plan renews within `days_before` days. Idempotent
    via the dedupe_key - same user/period only ever gets one reminder.
    """
    target = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days_before)
    rows = (
        db.query(Subscription)
        .filter(
            Subscription.status == "active",
            Subscription.period_end.isnot(None),
            Subscription.period_end <= target,
            Subscription.period_end > datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .all()
    )
    sent = 0
    for sub in rows:
        user = db.get(User, sub.user_id)
        if not user or not user.plan_auto_renew:
            continue
        Model = ImporterPlan if sub.plan_role == "importer" else ExporterPlan
        plan = db.get(Model, sub.plan_id)
        if not plan:
            continue
        subject, html = t_subscription_renewal_reminder(
            user.firstname or "there", plan.title, sub.period_end.strftime("%d %b %Y"),
        )
        ok = send_template(
            db, template="subscription_renewal_reminder",
            to=user.email, subject=subject, html=html, user_id=user.id,
            dedupe_key=f"sub_renewal:{sub.id}:{sub.period_end.date()}",
        )
        if ok:
            sent += 1
    log.info("renewal_reminders: %d sent (of %d candidates)", sent, len(rows))
    return sent


# ───────────────────────── process_renewals (tokenized auto-recharge) ─────────────────────────

def process_renewals(db: Session) -> int:
    """For subs whose period_end is in the next 24h and auto_renew=True, charge
    the stored Flutterwave card token to extend +30d. Retries are limited to 3
    failures before the subscription is allowed to expire and downgrade.
    """
    import asyncio
    import json
    import secrets

    from .config import get_settings
    from .models import Subscription, User
    from .services.flutterwave import tokenized_charge
    from .services.email import (
        send_template,
        t_subscription_renewed,
        t_subscription_renewal_failed,
    )

    s = get_settings()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window_end = now + timedelta(hours=24)

    rows = (
        db.query(Subscription)
        .filter(
            Subscription.status == "active",
            Subscription.period_end.isnot(None),
            Subscription.period_end <= window_end,
            Subscription.flw_card_token.isnot(None),
            Subscription.renewal_failure_count < 3,
        )
        .all()
    )

    charged = 0
    for sub in rows:
        user = db.get(User, sub.user_id)
        if not user or not user.plan_auto_renew:
            continue

        tx_ref = "JARAREN" + secrets.token_urlsafe(8).replace("-", "")[:10]
        try:
            flw = asyncio.run(
                tokenized_charge(
                    token=sub.flw_card_token,
                    amount=float(sub.amount),
                    currency=sub.currency,
                    tx_ref=tx_ref,
                    email=user.email,
                    customer_name=user.fullname or user.email,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.exception("renewal charge crashed for sub=%s", sub.id)
            flw = {"status": "failed", "error": repr(e)}

        sub.last_renewal_attempt_at = now
        if flw.get("status") == "successful":
            old_end = sub.period_end
            sub.period_start = old_end or now
            sub.period_end = (old_end or now) + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
            sub.tx_ref = tx_ref
            sub.provider_payload = json.dumps(flw)
            sub.renewal_failure_count = 0
            user.plan_renewal_date = sub.period_end
            db.commit()
            charged += 1

            from .models import ExporterPlan, ImporterPlan

            Model = ImporterPlan if sub.plan_role == "importer" else ExporterPlan
            plan = db.get(Model, sub.plan_id)
            if plan:
                subject, html = t_subscription_renewed(
                    user.firstname or "there",
                    plan.title,
                    f"{float(sub.amount):.2f} {sub.currency}",
                    sub.period_end.strftime("%d %b %Y"),
                    sub.flw_card_last4 or "on file",
                )
                send_template(
                    db, template="subscription_renewed",
                    to=user.email, subject=subject, html=html, user_id=user.id,
                    dedupe_key=f"renewed:{sub.id}:{sub.period_end.date()}",
                )
        else:
            sub.renewal_failure_count = (sub.renewal_failure_count or 0) + 1
            sub.provider_payload = json.dumps(flw)
            db.commit()

            from .models import ExporterPlan, ImporterPlan

            Model = ImporterPlan if sub.plan_role == "importer" else ExporterPlan
            plan = db.get(Model, sub.plan_id)
            if plan:
                subject, html = t_subscription_renewal_failed(
                    user.firstname or "there",
                    plan.title,
                    sub.renewal_failure_count,
                    f"{s.site_url}/{'importer' if sub.plan_role == 'importer' else 'exporter'}/subscription",
                )
                send_template(
                    db, template="subscription_renewal_failed",
                    to=user.email, subject=subject, html=html, user_id=user.id,
                    dedupe_key=f"renewal_failed:{sub.id}:{sub.renewal_failure_count}",
                )

    log.info("process_renewals: %d charged successfully (of %d due)", charged, len(rows))
    return charged


# ───────────────────────── inventory_reminders ─────────────────────────

def inventory_reminders(db: Session, stale_after_days: int = 7) -> int:
    """Email exporters whose products haven't been touched in `stale_after_days`.

    Per BRD: weekly inventory confirmation requirement. Products with stale
    inventory drop in search ranking.
    """
    from collections import defaultdict

    from .config import get_settings
    from .models import Product, User
    from .services.email import send_template, t_inventory_stale_reminder

    s = get_settings()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=stale_after_days)

    rows = (
        db.query(Product)
        .filter(
            Product.status == 1,
            (Product.last_inventory_update_at.is_(None)) | (Product.last_inventory_update_at < cutoff),
        )
        .all()
    )

    by_exporter = defaultdict(int)
    for p in rows:
        by_exporter[p.exporter_id] += 1

    sent = 0
    for exporter_id, count in by_exporter.items():
        user = db.get(User, exporter_id)
        if not user or not user.is_active:
            continue
        subject, html = t_inventory_stale_reminder(
            user.firstname or "there",
            count,
            f"{s.site_url}/exporter/products",
        )
        ok = send_template(
            db, template="inventory_stale",
            to=user.email, subject=subject, html=html, user_id=user.id,
            dedupe_key=f"inventory_stale:{exporter_id}:{cutoff.date()}",
        )
        if ok:
            sent += 1
    log.info("inventory_reminders: %d exporters notified", sent)
    return sent


# ───────────────────────── process_payouts ─────────────────────────

def process_payouts(db: Session) -> int:
    """Auto-dispatch seller payouts for every order eligible right now.

    Eligible = delivered + past 7-day dispute window + successful payment +
    no payout record yet. Calls the same shared `dispatch_payout` helper
    the admin /adm/payouts/{id}/send endpoint uses, so behaviour and audit
    trail are identical regardless of how the payout was triggered.

    Run nightly (e.g. via a Railway cron schedule):
        python -m app.cron process_payouts

    Returns the count of payouts successfully dispatched (status='sent').
    Failed dispatches still leave a Payout row with status='failed' so the
    admin can re-trigger from /admin/payouts.
    """
    import asyncio

    from .models import Order, Payment, Payout
    from .routers.payouts import (
        DISPUTE_WINDOW_DAYS,
        PayoutDispatchError,
        dispatch_payout,
    )

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DISPUTE_WINDOW_DAYS)
    candidates = (
        db.query(Order)
        .filter(Order.status == "delivered", Order.time_updated <= cutoff)
        .all()
    )
    dispatched = 0
    skipped = 0
    failed = 0
    for order in candidates:
        if db.query(Payout).filter(Payout.order_id == order.id).first():
            skipped += 1
            continue
        if not db.query(Payment).filter(Payment.order_id == order.id, Payment.status == "successful").first():
            skipped += 1
            continue
        try:
            payout = asyncio.run(dispatch_payout(order, db, initiated_by="cron"))
            if payout.status in ("sent", "completed"):
                dispatched += 1
            else:
                failed += 1
        except PayoutDispatchError as e:
            failed += 1
            log.warning("process_payouts: order %s - %s", order.order_number, e)
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("process_payouts: unexpected error on order %s", order.order_number)
    log.info(
        "process_payouts: %d dispatched, %d failed, %d skipped (of %d candidates)",
        dispatched, failed, skipped, len(candidates),
    )
    return dispatched


# ───────────────────────── CLI ─────────────────────────

JOBS = {
    "expire_subscriptions": expire_subscriptions,
    "renewal_reminders": renewal_reminders,
    "process_renewals": process_renewals,
    "inventory_reminders": inventory_reminders,
    "process_payouts": process_payouts,
}

SUBSCRIPTION_PERIOD_DAYS = 30  # mirrors routers.subscriptions; kept here so cron is self-contained


def run(name: str) -> int:
    if name == "all":
        with SessionLocal() as db:
            for fn in JOBS.values():
                fn(db)
        return 0
    fn = JOBS.get(name)
    if not fn:
        log.error("unknown job %r; choose from %s", name, list(JOBS) + ["all"])
        return 1
    with SessionLocal() as db:
        fn(db)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cron")
    parser.add_argument("job", choices=list(JOBS) + ["all"])
    args = parser.parse_args()
    return run(args.job)


if __name__ == "__main__":
    sys.exit(main())
