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
    via the dedupe_key — same user/period only ever gets one reminder.
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


# ───────────────────────── CLI ─────────────────────────

JOBS = {
    "expire_subscriptions": expire_subscriptions,
    "renewal_reminders": renewal_reminders,
}


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
