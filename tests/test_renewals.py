"""Tokenized auto-recharge tests (process_renewals cron)."""
from datetime import datetime, timedelta, timezone

from app.cron import process_renewals
from app.database import SessionLocal
from app.models import ImporterPlan, Subscription, User


def _setup_active_sub_with_token(near_renewal: bool = True, failures: int = 0):
    """Drop the demo importer onto a Premium plan whose period_end is near."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        premium = db.query(ImporterPlan).filter(ImporterPlan.is_default == 0).first()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        period_end = now + timedelta(hours=12) if near_renewal else now + timedelta(days=20)

        # Reset to known state
        db.query(Subscription).filter(Subscription.user_id == user.id).delete()
        sub = Subscription(
            user_id=user.id,
            plan_id=premium.id,
            plan_role="importer",
            status="active",
            amount=premium.monthly_subscription_fee,
            currency=premium.currency,
            period_start=now - timedelta(days=29),
            period_end=period_end,
            flw_card_token="DEV-CARD-TOKEN",
            flw_card_last4="4242",
            flw_card_brand="visa",
            renewal_failure_count=failures,
        )
        db.add(sub)
        user.plan_id = premium.id
        user.plan_renewal_date = period_end
        user.plan_auto_renew = True
        db.commit()
        return sub.id


def test_process_renewals_charges_card_and_extends_period():
    sub_id = _setup_active_sub_with_token(near_renewal=True)
    with SessionLocal() as db:
        before = db.get(Subscription, sub_id).period_end

        charged = process_renewals(db)
        assert charged == 1

        sub = db.get(Subscription, sub_id)
        assert sub.status == "active"
        assert sub.period_end > before  # extended
        assert sub.tx_ref and sub.tx_ref.startswith("JARAREN")
        assert sub.renewal_failure_count == 0


def test_process_renewals_skips_when_not_near_renewal():
    _setup_active_sub_with_token(near_renewal=False)
    with SessionLocal() as db:
        charged = process_renewals(db)
        assert charged == 0


def test_process_renewals_skips_when_auto_renew_off():
    sub_id = _setup_active_sub_with_token(near_renewal=True)
    with SessionLocal() as db:
        sub = db.get(Subscription, sub_id)
        user = db.get(User, sub.user_id)
        user.plan_auto_renew = False
        db.commit()
        charged = process_renewals(db)
        assert charged == 0


def test_process_renewals_stops_after_three_failures():
    _setup_active_sub_with_token(near_renewal=True, failures=3)
    with SessionLocal() as db:
        # 3 failures: cron should skip this sub
        charged = process_renewals(db)
        assert charged == 0
