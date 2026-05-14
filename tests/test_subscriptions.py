"""Subscription flow + cron tests."""
from datetime import datetime, timedelta, timezone

from app.cron import expire_subscriptions, renewal_reminders
from app.database import SessionLocal
from app.models import ImporterPlan, Subscription, User


def _premium_plan_id(client):
    r = client.get("/public/data/importer_plan")
    plans = r.json()["payload"]["rows"]
    return next(p["id"] for p in plans if float(p["monthly_subscription_fee"]) > 0)


def _reset_importer_to_free_tier():
    """Some tests upgrade the demo importer to Premium. For tests that need a
    clean 'fresh user' state, blow that away."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        free = db.query(ImporterPlan).filter(ImporterPlan.is_default == 1).first()
        db.query(Subscription).filter(Subscription.user_id == user.id).delete()
        user.plan_id = free.id if free else None
        user.plan_renewal_date = None
        user.plan_auto_renew = True
        db.commit()


def test_get_current_starts_on_free_tier(client, importer_token):
    _reset_importer_to_free_tier()
    r = client.get("/imp/subscription", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["current_plan"]["title"] == "Free Tier"
    assert p["subscription"] is None
    assert p["plan_renewal_date"] is None


def test_upgrade_returns_flutterwave_config(client, importer_token):
    plan_id = _premium_plan_id(client)
    r = client.post("/imp/subscription/upgrade",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"plan_id": plan_id})
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["requires_payment"] is True
    assert p["tx_ref"].startswith("JARASUB")
    assert p["meta"]["type"] == "subscription"


def test_verify_activates_subscription_and_sets_renewal(client, importer_token):
    plan_id = _premium_plan_id(client)
    r = client.post("/imp/subscription/upgrade",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"plan_id": plan_id})
    tx_ref = r.json()["payload"]["tx_ref"]

    r = client.post("/imp/subscription/verify",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"tx_ref": tx_ref})
    assert r.status_code == 200, r.text
    p = r.json()["payload"]
    assert p["status"] == "active"
    assert p["period_end"]

    r = client.get("/imp/subscription", headers={"Authorization": f"Bearer {importer_token}"})
    p = r.json()["payload"]
    assert p["current_plan"]["title"] == "Premium"
    assert p["plan_renewal_date"]


def test_cancel_keeps_premium_until_period_end(client, importer_token):
    plan_id = _premium_plan_id(client)
    r = client.post("/imp/subscription/upgrade",
                    headers={"Authorization": f"Bearer {importer_token}"},
                    data={"plan_id": plan_id})
    tx_ref = r.json()["payload"]["tx_ref"]
    client.post("/imp/subscription/verify",
                headers={"Authorization": f"Bearer {importer_token}"},
                data={"tx_ref": tx_ref})

    r = client.post("/imp/subscription/cancel", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.status_code == 200
    p = r.json()["payload"]
    assert p["status"] == "cancelled"
    assert p["cancelled_at"]

    # User still on Premium until period_end
    r = client.get("/imp/subscription", headers={"Authorization": f"Bearer {importer_token}"})
    assert r.json()["payload"]["current_plan"]["title"] == "Premium"


def test_expire_cron_downgrades_lapsed_users():
    """Manually backdate a sub to expire it, then run the cron and verify downgrade."""
    from sqlalchemy import select
    with SessionLocal() as db:
        # Pick the demo importer
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        # Find or create an active subscription with period_end in the past
        premium = db.query(ImporterPlan).filter(ImporterPlan.is_default == 0).first()
        sub = (
            db.query(Subscription)
            .filter(Subscription.user_id == user.id, Subscription.status == "active")
            .first()
        )
        if not sub:
            sub = Subscription(
                user_id=user.id, plan_id=premium.id, plan_role="importer",
                status="active", amount=premium.monthly_subscription_fee, currency=premium.currency,
            )
            db.add(sub)
        sub.status = "active"
        sub.period_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
        sub.period_end = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
        user.plan_id = premium.id
        user.plan_renewal_date = sub.period_end
        db.commit()

        downgraded = expire_subscriptions(db)
        assert downgraded >= 1

        # User should now be on the default (free) plan
        db.refresh(user)
        default = db.query(ImporterPlan).filter(ImporterPlan.is_default == 1).first()
        assert user.plan_id == default.id


def test_renewal_reminders_skips_when_auto_renew_off():
    """A cancelled sub (auto_renew=False) should not get a renewal reminder."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "importer@jaratrade.com").first()
        premium = db.query(ImporterPlan).filter(ImporterPlan.is_default == 0).first()
        sub = Subscription(
            user_id=user.id, plan_id=premium.id, plan_role="importer", status="active",
            amount=premium.monthly_subscription_fee, currency=premium.currency,
            period_start=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=27),
            period_end=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2),
        )
        db.add(sub)
        user.plan_auto_renew = False
        db.commit()
        sent = renewal_reminders(db, days_before=3)
        # Auto-renew is off so we don't bother them
        assert sent == 0
