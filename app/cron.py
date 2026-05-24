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

    Eligible = delivered + past 1-day dispute window + successful payment +
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

    # Either the buyer confirmed receipt (immediate release) or the
    # delivered timestamp has aged past the 1-day window.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DISPUTE_WINDOW_DAYS)
    candidates = (
        db.query(Order)
        .filter(
            Order.status == "delivered",
            (Order.time_updated <= cutoff) | (Order.confirmed_received_at.isnot(None)),
        )
        .all()
    )
    dispatched = 0
    skipped = 0
    failed = 0
    for order in candidates:
        if db.query(Payout).filter(Payout.order_id == order.id).first():
            skipped += 1
            continue
        payment = (
            db.query(Payment)
            .filter(Payment.order_id == order.id, Payment.status == "successful")
            .first()
        )
        if not payment:
            skipped += 1
            continue
        # Settlement gate: for international (non-NGN) collections, FLW takes
        # T+5 business days to credit our NGN wallet. Dispatching the seller
        # payout before then would either fail with insufficient balance OR
        # draw against unrelated NGN collections we'd later need to
        # reconcile. NGN charges settle T+1 (inside our 1-day dispute
        # window) so they're implicitly fine - we only gate cross-currency.
        currency = (payment.currency or "NGN").upper()
        if currency != "NGN" and payment.settlement_status != "completed":
            log.info(
                "process_payouts: skipping order %s - %s settlement is %s, waiting on FLW",
                order.order_number, currency, payment.settlement_status or "unknown",
            )
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


# ───────────────────────── poll_settlements ─────────────────────────

def poll_settlements(db: Session) -> int:
    """Refresh FLW settlement status on payments that have a settlement_id
    but haven't yet reached a terminal status.

    For international (non-NGN) collections we capture `flw_settlement_id`
    on the charge.completed webhook. FLW takes T+5 business days to actually
    credit our wallet, after which the settlement flips to `completed` and
    process_payouts can dispatch the seller's NGN payout. FLW doesn't push
    a settlement webhook, so we poll - cheap (one GET per pending row).

    Run hourly (or alongside process_payouts daily, fine for our volume).

    Returns the number of payments whose status was updated.
    """
    import asyncio

    from .models import Payment
    from .services.flutterwave import FlutterwaveError, get_settlement

    # Pull payments that still have something to learn: a settlement_id was
    # captured AND status isn't yet terminal. Don't poll completed/failed
    # rows - nothing more FLW can tell us.
    pending = (
        db.query(Payment)
        .filter(
            Payment.flw_settlement_id.isnot(None),
            Payment.settlement_status.notin_(["completed", "failed"]),
        )
        .all()
    )

    updated = 0
    errored = 0
    for p in pending:
        try:
            data = asyncio.run(get_settlement(p.flw_settlement_id))
        except FlutterwaveError as e:
            log.warning(
                "poll_settlements: FLW rejected settlement %s for payment %s - %s",
                p.flw_settlement_id, p.id, e,
            )
            errored += 1
            continue
        except Exception:  # noqa: BLE001
            log.exception("poll_settlements: unexpected error polling %s", p.flw_settlement_id)
            errored += 1
            continue

        new_status = str(data.get("status") or "").lower() or None
        if new_status and new_status != p.settlement_status:
            p.settlement_status = new_status
            updated += 1
    if updated or errored:
        db.commit()
    log.info(
        "poll_settlements: %d updated, %d errored (of %d pending)",
        updated, errored, len(pending),
    )
    return updated


# ───────────────────────── CLI ─────────────────────────

JOBS = {
    "expire_subscriptions": expire_subscriptions,
    "renewal_reminders": renewal_reminders,
    "process_renewals": process_renewals,
    "inventory_reminders": inventory_reminders,
    "process_payouts": process_payouts,
    "poll_settlements": poll_settlements,
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


def _guard_against_sqlite_fallback() -> None:
    """Refuse to run cron jobs against the dev SQLite default.

    Cron containers on Railway/Fly are deployed as a separate service from
    the web container, so they have their own env vars. If DATABASE_URL
    isn't wired up on the cron service, the app silently falls through to
    the SQLite default in app/config.py - and then every query crashes
    with "no such table: orders" because the SQLite file is fresh in the
    container. Worse, on a future change the cron could *not* crash and
    instead happily process an empty result set, missing real payouts.

    Fail loudly here with a fix-it message instead.
    """
    from .config import get_settings

    url = get_settings().database_url
    if url.startswith("sqlite"):
        log.error(
            "Refusing to run: DATABASE_URL resolved to %r, which is the "
            "SQLite dev fallback. On Railway, open the cron service's "
            "Variables tab and add DATABASE_URL referencing the Postgres "
            "plugin (the value '${{Postgres.DATABASE_URL}}' is what the "
            "web service uses). Also copy across FLW_SECRET_KEY, "
            "RESEND_API_KEY, and any other secrets the cron needs.",
            url,
        )
        return 2  # caller (main) converts to exit code


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cron")
    parser.add_argument("job", choices=list(JOBS) + ["all"])
    args = parser.parse_args()
    guard_rc = _guard_against_sqlite_fallback()
    if guard_rc:
        return guard_rc
    return run(args.job)


if __name__ == "__main__":
    sys.exit(main())
