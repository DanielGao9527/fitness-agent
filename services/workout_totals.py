def workout_totals(workouts):
    completed = [row for row in workouts if row["status"] == "completed"]
    planned = [row for row in workouts if row["status"] == "planned"]
    energy = [row["calorie_estimate"]["kcal"] for row in completed if row.get("calorie_estimate")]
    return {
        "workout_count": len(workouts),
        "completed_count": len(completed),
        "planned_count": len(planned),
        "completed_minutes": sum(row["minutes"] for row in completed),
        "planned_minutes": sum(row["minutes"] for row in planned),
        "estimated_workout_calories": {
            "lower_total": round(sum(value["lower"] for value in energy), 2) if energy else None,
            "upper_total": round(sum(value["upper"] for value in energy), 2) if energy else None,
            "count": len(energy), "unknown_count": len(completed) - len(energy), "basis": "gross_activity",
        },
    }
