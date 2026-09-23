"""Isolated browser fixture. Never use this server for real records or model validation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
import api.routes as routes
import api.workouts as workout_routes
import api.knowledge as knowledge_routes
import api.meal_plans as plan_routes
import api.training_plans as training_plan_routes
import api.coach as coach_routes
import api.meal_photos as photo_routes
from app import create_app
from config import ROOT, Settings
from model.factory import ModelError


class FixtureModel:
    def generate(self, *, system_prompt, message):
        if "火锅样本" in message:
            return json.dumps({"input_type": "text", "items": [
                {"name": "羊肉", "grams": None, "amount_description": "半盘（两人共享一盘），火锅涮煮"},
                {"name": "牛肉", "grams": None, "amount_description": "半盘，火锅涮煮"},
            ]}, ensure_ascii=False)
        if "份量样本" in message:
            return json.dumps({"input_type":"text", "items":[
                {"name":"鸡蛋", "grams":None, "amount_description":"两个"},
                {"name":"麻婆豆腐", "grams":None, "amount_description":"一盘（单人份）"},
            ]}, ensure_ascii=False)
        if "timeout" in message:
            raise ModelError("MODEL_TIMEOUT", "测试替身：模型超时，草稿已保留。", 504)
        return json.dumps({"input_type": "text", "items": [
            {"name": "鸡蛋", "grams": 100, "amount_description": "100克"},
            {"name": "牛奶", "grams": None, "amount_description": ""},
        ], "questions": ["请补充牛奶的份量。"]}, ensure_ascii=False)


class FixtureSpeech:
    def transcribe(self, data):
        return "早餐吃了两个鸡蛋。"


class FixturePhoto:
    def generate(self, *, system_prompt, message, image):
        if "timeout" in message:
            raise ModelError("MODEL_TIMEOUT", "测试替身：照片识别超时，原草稿保留。", 504)
        return json.dumps({"input_type": "photo", "items": [{"name": "鸡蛋", "grams": None},
                                                            {"name": "米饭", "grams": None}], "questions": []})


photo_routes.QwenVisionModel = lambda settings: FixturePhoto()
routes.photo_status = lambda settings: "configured_unverified"


class FixtureNutrition:
    def generate(self, *, system_prompt, message):
        items = json.loads(message)["items"]
        if any("timeout" in item["name"] for item in items):
            raise ModelError("MODEL_TIMEOUT", "测试替身：营养估算超时，草稿已保留。", 504)
        return json.dumps({"items": [{
            "index": item["index"], "status": "estimated",
            "kcal": {"lower": 140, "upper": 190}, "protein": {"lower": 11, "upper": 15},
            "carbs": {"lower": 1, "upper": 4}, "fat": {"lower": 8, "upper": 13},
            "assumptions": ["测试替身数值，不是食物营养依据", "按所填个人份量，仅核对页面流程"],
        } if "共享" not in item["name"] else {"index": item["index"], "status": "unknown", "question": "这盘菜你自己吃了多少？"} for item in items]}, ensure_ascii=False)


routes.create_meal_text_model = lambda settings: FixtureModel()
routes.meal_text_status = lambda settings: "configured_unverified"
routes.QwenSpeechModel = lambda settings: FixtureSpeech()
routes.speech_status = lambda settings: "configured_unverified"
routes.create_nutrition_model = lambda settings: FixtureNutrition()
plan_routes.create_nutrition_model = lambda settings: FixtureNutrition()
routes.nutrition_status = lambda settings: "configured_unverified"


class FixtureWorkout:
    def generate(self, *, system_prompt, message):
        if "JSON Schema" in system_prompt and "训练记录解析" in system_prompt:
            if "我练了肩20分钟" in message:
                return json.dumps({"items": [
                    {"name":"胸", "minutes":30, "status":"completed", "source_text":"我今天上午练了胸30分钟"},
                    {"name":"肩", "minutes":20, "status":"completed", "source_text":"我练了肩20分钟"},
                    {"name":"二头和三头", "minutes":50, "status":"completed", "source_text":"晚上练了二头和三头50分钟"},
                ]}, ensure_ascii=False)
            if "三头" in message:
                return json.dumps({"items": [
                    {"name":"游泳", "minutes":30, "status":"completed", "intensity":"normal", "source_text":"游泳游了30分钟"},
                    {"name":"胸和三头力量训练", "minutes":40, "status":"completed", "intensity":"normal_assumed", "details":"力竭", "source_text":"练三头胸练了40分钟"},
                ]}, ensure_ascii=False)
            return json.dumps({"items": [
                {"name":"快走", "minutes":30, "status":"completed", "intensity":"moderate", "source_text":"快走30分钟"},
                {"name":"骑车", "minutes":20, "status":"planned", "intensity":"light", "source_text":"骑车20分钟"},
            ]}, ensure_ascii=False)
        items = json.loads(message)["items"]
        if any("timeout" in item["name"] for item in items):
            raise ModelError("MODEL_TIMEOUT", "测试替身：训练估算超时", 504)
        return json.dumps({"items": [{"index":item["index"], "status":"estimated",
            "kcal":{"lower":item["minutes"]*3, "upper":item["minutes"]*5},
            "assumptions":["测试替身数值，不是运动消耗依据", "用于验证总消耗显示及编辑流程"], "question":None} for item in items]}, ensure_ascii=False)


workout_routes.create_workout_model = lambda settings: FixtureWorkout()
routes.workout_status = lambda settings: "configured_unverified"


class FixtureKnowledge:
    def generate(self, *, system_prompt, message):
        data = json.loads(message)
        if "timeout" in data["question"]:
            raise ModelError("MODEL_TIMEOUT", "测试替身：问答超时，问题已保留。", 504)
        return json.dumps({"status": "answered", "chunk_ids": [data["evidence"][0]["chunk_id"]]})


knowledge_routes.create_knowledge_model = lambda settings: FixtureKnowledge()
routes.knowledge_qa_status = lambda settings: "configured_unverified"


class FixturePlan:
    def generate(self, *, system_prompt, message):
        data = json.loads(message)
        items = list(data.get("keep_items", []))
        allowed = {food["id"]: food for food in data["allowed_foods"]}
        for limit in data.get("portion_limits", []):
            lower = allowed[limit["food_id"]]["min"]
            items.append({"food_id": limit["food_id"], "lower": lower, "upper": max(lower, limit["max_upper"] - 10)})
        used = {item["food_id"] for item in items}
        chosen = []
        groups = data.get("required_groups", {"starch": 1, "protein": 1, "vegetable": 1})
        for group in data.get("requested_groups", []):
            groups.setdefault(group, 1)
        for group, count in groups.items():
            remaining = count - sum(allowed[item["food_id"]]["group"] == group for item in items)
            for _ in range(remaining):
                food = next(food for food in data["allowed_foods"] if food["group"] == group and food["id"] not in used)
                chosen.append(food)
                used.add(food["id"])
        items.extend({"food_id": food["id"], "lower": food["min"], "upper": food["min"] + (0 if food["unit"] == "个" else 20)} for food in chosen)
        return json.dumps({"items": items,
                           "chunk_ids": [hit["chunk_id"] for hit in data["evidence"]]})


plan_routes.create_meal_plan_model = lambda settings: FixturePlan()
coach_routes.create_meal_plan_model = lambda settings: FixturePlan()
routes.meal_plan_status = lambda settings: "configured_unverified"
class FixtureTrainingPlan:
    def generate(self, *, system_prompt, message):
        data = json.loads(message)
        return json.dumps({"activity_id": data["allowed_activities"][0]["id"],
                           "main_minutes": min(10, data["max_main_minutes"]),
                           "chunk_ids": [hit["chunk_id"] for hit in data["evidence"]]})


training_plan_routes.create_training_plan_model = lambda settings: FixtureTrainingPlan()
routes.training_plan_status = lambda settings: "configured_unverified"


class FixtureCoach:
    def generate(self, *, system_prompt, message):
        data = json.loads(message)
        texts = [item["message"] for item in data["messages"]]
        if any("失败样本" in text for text in texts):
            raise ModelError("MODEL_TIMEOUT", "模拟理解超时，原话已保留。", 504)
        notes = [{"version": item["version"], "avoid": [food for food in ("胡萝卜", "西兰花") if food in item["message"]],
                  "preferences": [], "training_caution": False, "diet_caution": False, "unresolved": False} for item in data["messages"]]
        command, scope, clarification = "下一餐吃什么", "meal", "none"
        quality = {
            "我还是想试试三分化，你帮我把这一周重新排一下": {"split": "ppl", "weekly": True},
            "今天是在家练，只有哑铃，没有训练凳，帮我调整一下": {"equipment": "只有哑铃，没有训练凳"},
            "刚才的哑铃平板卧推我想改用杠铃平板卧推，其他动作保持": {"replace_from": "哑铃平板卧推", "replace_with": "杠铃平板卧推"},
            "这次先不练力量了，我想去泳池游一会儿，接下来还有30分钟": {"activity": "swim", "minutes": 30, "time_basis": "session"},
        }
        if texts[-1] in quality:
            update = quality[texts[-1]]
            aerobic = update.get("activity") == "swim"
            return json.dumps({"scope": "aerobic" if aerobic else "strength", "command": "有氧建议" if aerobic else "",
                "clarification": "none", "notes": notes, "training": {"source_text": texts[-1], **update}})
        if any("这个" in text for text in texts):
            if any("西兰花" in text for text in texts):
                command = "我不吃西兰花"
            else:
                command, clarification = "", "which_food"
        if any("肩疼" in text for text in texts):
            command, scope = "有氧建议", "aerobic"
        batches = {
            "鸡肉换成牛肉，米饭少一点": ["把鸡肉换成牛肉", "米饭少一点"],
            "米饭少一点，米饭换成玉米": ["米饭少一点", "米饭换成玉米"],
            "只换成玉米，牛肉换成虾，西兰花不要": ["米饭换成玉米", "牛肉换成虾", "我不吃西兰花"],
        }
        if texts[-1] in batches:
            return json.dumps({"scope": "meal", "command": "", "commands": batches[texts[-1]], "clarification": "none", "notes": notes})
        if texts[-1] == "今天想练胸和三头，本次40分钟，有哑铃":
            return json.dumps({"scope": "strength", "command": "", "clarification": "none", "notes": notes,
                               "training": {"source_text": texts[-1], "minutes": 40, "time_basis": "session", "focus": "胸和三头", "equipment": "哑铃"}})
        return json.dumps({"scope": scope, "command": command, "clarification": clarification, "notes": notes})


coach_routes.create_coach_model = lambda settings: FixtureCoach()
coach_mode = "--coach" in sys.argv
app = create_app(Settings(database_path=ROOT / "artifacts" / ("ui-coach-review-test.sqlite3" if coach_mode else "ui-drafts-test.sqlite3"),
                          ai_global_daily_limit=10000,  # Synthetic repeated browser runs; production budget is unchanged.
                          training_plan_enabled=True, coach_enabled=coach_mode, model_provider="qwen" if coach_mode else "disabled", qwen_api_key="synthetic-fixture" if coach_mode else ""))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8767, access_log=False)
