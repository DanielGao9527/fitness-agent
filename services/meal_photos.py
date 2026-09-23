"""Photo recognition creates an editable draft, never an eaten record."""
import hashlib
import json
import time

from fastapi import HTTPException
from pydantic import ValidationError

from config import ROOT
from model.factory import ModelError
from model.vision import normalize_photo
from schemas import MealDraft
from services.meal_drafts import MealDraftStore


class MealPhotoService(MealDraftStore):
    def parse_photo(self, draft_id, version, client_id, data, content_type, factory, model_name):
        # Check ownership before decoding untrusted image bytes.
        self.get(draft_id)
        image = normalize_photo(data, content_type)
        request_id = str(client_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._read(connection, draft_id)
            payload = json.loads(row["payload"])
            fingerprint = hashlib.sha256(image + row["text"].encode()).hexdigest()
            previous = payload.get("photo_review", {})
            if previous.get("client_id") == request_id:
                if previous.get("input_hash") != fingerprint or previous.get("base_version") != version:
                    raise HTTPException(409, "照片或补充描述已改变，请重新核对")
                if previous.get("status") == "ready":
                    return self._view(row)
                raise HTTPException(409, "这次识别正在处理或未完成，请读取最新草稿；重试须明确重新识别")
            self._editable(row, version)
            if previous.get("status") == "generating" and time.time() - previous["started_at"] < 180:
                raise HTTPException(409, "照片正在识别，请稍后读取最新草稿；超过3分钟可重新识别")
            payload["photo_review"] = {"client_id": request_id, "input_hash": fingerprint, "base_version": version,
                                       "status": "generating", "started_at": time.time()}
            connection.execute("UPDATE meal_drafts SET payload=? WHERE id=? AND user_id=?",
                               (json.dumps(payload, ensure_ascii=False), str(draft_id), self.user_id))
            text = row["text"]
        try:
            prompt = (ROOT / "prompts/meal_photo.md").read_text(encoding="utf-8")
            prompt += "\nJSON Schema:\n" + json.dumps(MealDraft.model_json_schema(), ensure_ascii=False)
            raw = factory().generate(system_prompt=prompt, message=text, image=image)
            try:
                draft = MealDraft.model_validate_json(raw)
                if draft.input_type != "photo":
                    raise ValueError
            except (ValidationError, ValueError):
                raise ModelError("MODEL_INVALID_OUTPUT", "照片识别结果未通过检查，原草稿保留；可手动补充或重新识别。") from None
            # A photograph cannot attest to the user's actual consumed quantity.
            items = [{**item.model_dump(mode="json"), "grams": None, "amount_description": "",
                      "confidence": "needs_confirmation"} for item in draft.items]
            questions = ["请核对食物名称，并填写自己实际吃的基本份量；整桌或共享菜不能默认全部吃完。"] if items else ["未识别到可确认的食物，请换照片或手动填写。"]
            questions += draft.questions[:9]
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._read(connection, draft_id)
                self._editable(row, version)
                payload = json.loads(row["payload"])
                review = payload.get("photo_review", {})
                if review.get("client_id") != request_id or review.get("status") != "generating":
                    raise HTTPException(409, "已有更新的识别请求，未覆盖新草稿")
                payload.pop("nutrition", None)
                payload.update(input_type="photo", items=items, questions=questions)
                review.update(status="ready", model=model_name)
                self._write(connection, row, payload, row["day"], row["meal_type"])
                return self._view(self._read(connection, draft_id))
        except Exception:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._read(connection, draft_id)
                payload = json.loads(row["payload"])
                review = payload.get("photo_review", {})
                if review.get("client_id") == request_id and review.get("status") == "generating":
                    review["status"] = "failed"
                    connection.execute("UPDATE meal_drafts SET payload=? WHERE id=? AND user_id=?",
                                       (json.dumps(payload, ensure_ascii=False), str(draft_id), self.user_id))
            raise
