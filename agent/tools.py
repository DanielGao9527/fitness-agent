from datetime import date

from services.records import RecordService


class FitnessTools:
    """Bind identity in the server; the model never supplies a user identifier."""

    def __init__(self, records: RecordService):
        self.records = records

    def read_day(self, day: date) -> dict:
        return {
            "profile": self.records.get_profile(),
            "meals": self.records.list_records("meals", day),
            "workouts": self.records.list_records("workouts", day),
            "summary": self.records.summary(day),
        }
