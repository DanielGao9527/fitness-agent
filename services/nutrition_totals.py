"""Recorded nutrition totals shared by the daily summary and meal planning."""
from decimal import Decimal

from services.nutrition import NUTRIENTS


def nutrition_totals(meals):
    totals, estimates = {}, {}
    for name in NUTRIENTS:
        field = f"{name}_per_100g"
        known = [meal for meal in meals if meal[field] is not None and meal.get("grams") is not None]
        total = sum((Decimal(str(meal[field])) * Decimal(str(meal["grams"])) / 100 for meal in known), Decimal(0))
        totals[name] = {"known_total": float(round(total, 2)) if known else None, "missing_count": len(meals) - len(known)}
        estimated = [meal["nutrition_estimate"][name] for meal in meals
                     if meal.get("nutrition_estimate") and (meal[field] is None or meal.get("grams") is None)]
        estimates[name] = {
            "lower_total": float(round(sum((Decimal(str(value["lower"])) for value in estimated), Decimal(0)), 2)) if estimated else None,
            "upper_total": float(round(sum((Decimal(str(value["upper"])) for value in estimated), Decimal(0)), 2)) if estimated else None,
            "count": len(estimated), "unknown_count": len(meals) - len(known) - len(estimated),
        }
    return totals, estimates
