"""Shared constants."""

ROLE_IMPORTER = "importer"
ROLE_EXPORTER = "exporter"
ROLE_ADMIN = "admin"

USER_TYPES = (ROLE_IMPORTER, ROLE_EXPORTER, ROLE_ADMIN)

KIND_INDIVIDUAL = "individual"
KIND_BUSINESS = "business"

CURRENCY_NGN = "NGN"
CURRENCY_GBP = "GBP"

ORDER_STATUSES = (
    "pending",      # awaiting payment
    "paid",         # funds in escrow
    "confirmed",    # exporter has confirmed
    "preparing",    # exporter is preparing the shipment
    "shipped",      # logistics has picked up
    "delivered",    # buyer confirmed receipt
    "cancelled",    # cancelled before shipment
    "refunded",     # post-delivery refund issued
)

PAYMENT_STATUSES = ("pending", "successful", "failed")

SHIPPING_MODES = ("self", "logistics")
