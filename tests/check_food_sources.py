"""Read-only audit against an already downloaded official CoFID workbook.

Run using a Python environment with openpyxl; not an application dependency.
"""
import hashlib
import json
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]


def main():
    reference = json.loads((ROOT / "data/nutrition/reference.json").read_text(encoding="utf-8"))
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "artifacts/cofid-2021.xlsx"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["source"]["download_sha256"]
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = {str(row[0]): row for row in workbook[reference["source"]["sheet"]].iter_rows(values_only=True)}
    for key, item in reference["foods"].items():
        row = rows[item["code"]]
        assert row[1] == item["original_name"], key
        for name, column in {"protein": 9, "fat": 10, "carbs": 11, "kcal": 12}.items():
            value = row[column]
            trace = value == "Tr"
            assert trace == (name in item.get("trace", [])), (key, name, "trace")
            assert (0 if trace else float(value)) == item[name], (key, name)
    workbook.close()
    print(json.dumps({"matched_foods": len(reference["foods"]), "matched_nutrient_values": len(reference["foods"]) * 4,
                      "sha256_matches": True, "read_only": True}))


if __name__ == "__main__":
    main()
