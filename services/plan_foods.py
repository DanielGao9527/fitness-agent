"""Reviewed ingredient vocabulary paired with the separate composition reference."""
import re

from model.factory import ModelError

CATALOG_VERSION = "2026-09-23.6-cn"
FOODS = {
    "rice": ("米饭", "starch", "g", "熟重，不含额外配料", 80, 250),
    "brown-rice": ("糙米饭", "starch", "g", "熟重，不含额外配料", 80, 250),
    "oats": ("原味燕麦片", "starch", "g", "干重，仅燕麦成分", 30, 80),
    "potato": ("土豆", "starch", "g", "生的可食部分，不含配料", 100, 300),
    "corn": ("玉米", "starch", "g", "熟的玉米粒净重，不含玉米芯和配料", 80, 250),
    "sweet-potato": ("红薯", "starch", "g", "生的可食部分，不含配料", 100, 300),
    "chicken": ("鸡胸肉", "protein", "g", "生的可食部分，去皮无腌料", 60, 200),
    "egg": ("鸡蛋", "protein", "个", "普通大小，不含额外配料", 1, 3),
    "tofu": ("原味豆腐", "protein", "g", "沥水后净重，核对实际配料", 100, 250),
    "beef": ("牛肉", "protein", "g", "生的可食瘦肉部分，不含骨、腌料", 60, 200),
    "shrimp": ("虾仁", "protein", "g", "生的可食部分，去头去壳无腌料", 60, 200),
    "broccoli": ("西兰花", "vegetable", "g", "生的可食部分，不含配料", 80, 250),
    "carrot": ("胡萝卜", "vegetable", "g", "生的可食部分，不含配料", 80, 250),
    "spinach": ("菠菜", "vegetable", "g", "生的可食部分，不含配料", 80, 250),
    "olive-oil": ("橄榄油", "fat", "g", "纯橄榄油，单独计入用油量", 5, 15),
    "barley": ("大麦饭", "starch", "g", "珍珠大麦煮熟净重，无配料", 80, 250),
    "buckwheat": ("荞麦米", "starch", "g", "纯荞麦粒干重，不是荞麦面", 30, 80),
    "quinoa": ("藜麦", "starch", "g", "干重，不含配料", 30, 80),
    "wholewheat-pasta": ("全麦意面", "starch", "g", "无酱料熟重，配方需核对小麦及蛋等配料", 80, 250),
    "rice-noodles": ("原味米粉", "starch", "g", "仅米与水配方煮熟净重，核对包装配料，无汤料", 80, 250),
    "pork": ("瘦猪里脊", "protein", "g", "生的可食瘦肉净重，无骨无腌料", 60, 200),
    "salmon": ("三文鱼", "protein", "g", "生的鱼肉净重，不含骨和腌料；生重不是生食建议", 60, 200),
    "cod": ("鳕鱼", "protein", "g", "生的鱼肉净重，不含骨和腌料；生重不是生食建议", 60, 200),
    "lentils": ("小扁豆", "protein", "g", "绿/棕小扁豆煮熟净重，无配料，不是鲜扁豆角", 100, 250),
    "chickpeas": ("鹰嘴豆", "protein", "g", "煮熟净重，无配料", 100, 250),
    "cabbage": ("卷心菜", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "cauliflower": ("菜花", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "cucumber": ("黄瓜", "vegetable", "g", "带皮生的可食部分，无配料", 80, 250),
    "tomato": ("番茄", "vegetable", "g", "普通番茄生的可食部分，无配料", 80, 250),
    "aubergine": ("茄子", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "green-pepper": ("青甜椒", "vegetable", "g", "生的可食部分，无配料，不是辣椒", 80, 250),
    "courgette": ("西葫芦", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "mushroom": ("白蘑菇", "vegetable", "g", "白蘑菇生的可食部分，无配料，不代理所有菌菇", 80, 250),
    "pakchoi": ("小白菜", "vegetable", "g", "蒸熟净重，无配料", 80, 250),
    "green-beans": ("四季豆", "vegetable", "g", "充分煮熟净重，无配料，不可生食", 80, 250),
    "rapeseed-oil": ("菜籽油", "fat", "g", "纯菜籽油，单独计入用油量", 5, 15),
    "sesame-oil": ("芝麻油", "fat", "g", "纯芝麻油，核对配料，单独计量", 5, 15),
    "almonds": ("杏仁", "fat", "g", "原味甜杏仁仁，不是苦杏仁，不含糖盐", 5, 25),
    "apple": ("苹果", "fruit", "g", "带皮可食净重，去核", 80, 200),
    "banana": ("香蕉", "fruit", "g", "去皮可食净重", 60, 180),
    "orange": ("橙子", "fruit", "g", "去皮可食净重", 80, 200),
    "pear": ("亚洲梨", "fruit", "g", "带皮可食净重，去核，不代理西洋梨", 80, 200),
    "strawberry": ("草莓", "fruit", "g", "去蒂可食净重", 80, 200),
    "milk": ("低脂纯牛奶", "dairy", "g", "半脱脂UHT牛奶净重，按克不是毫升，核对配料", 100, 250),
    "yogurt": ("原味低脂酸奶", "dairy", "g", "无额外糖和配料的净重，核对包装", 100, 250),
    "wholemeal-bread": ("全麦面包", "starch", "g", "成品净重；核对小麦、蛋、奶、大豆及芝麻等配料", 30, 120),
    "egg-noodles": ("鸡蛋面", "starch", "g", "不加汤料的熟面净重；含小麦与鸡蛋，核对配料", 80, 250),
    "couscous": ("库斯库斯", "starch", "g", "原味蒸粗麦粉熟重；含小麦，不是小米", 80, 250),
    "lamb": ("瘦羊肉", "protein", "g", "生的可食瘦肉净重；无骨无腌料，不代理肥羊卷", 60, 200),
    "duck": ("去皮鸭肉", "protein", "g", "去皮去骨生肉净重；不含腌料", 60, 200),
    "turkey": ("火鸡胸肉", "protein", "g", "去皮去骨生肉净重，无腌料", 60, 200),
    "tuna": ("金枪鱼", "protein", "g", "生鱼肉净重，无骨无腌料；不是生食建议，不代理罐头", 60, 200),
    "soybeans": ("熟黄豆", "protein", "g", "黄豆充分煮熟沥水净重，无配料；不是毛豆", 80, 200),
    "kidney-beans": ("熟红腰豆", "protein", "g", "充分煮熟沥水净重，无配料，不可生食", 80, 200),
    "peas": ("青豌豆", "vegetable", "g", "冷冻青豌豆煮熟沥水净重，无配料", 80, 200),
    "asparagus": ("芦笋", "vegetable", "g", "蒸熟可食净重，无配料", 80, 250),
    "beetroot": ("甜菜根", "vegetable", "g", "煮熟可食净重，无配料", 80, 200),
    "lettuce": ("生菜", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "celery": ("芹菜", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "pumpkin": ("南瓜", "vegetable", "g", "生的可食净重，去皮去籽，无配料", 80, 250),
    "radish": ("白萝卜", "vegetable", "g", "生的可食部分，无配料", 80, 250),
    "onion": ("洋葱", "vegetable", "g", "生的可食部分，无配料", 50, 150),
    "leek": ("韭葱", "vegetable", "g", "煮熟沥水净重，无配料；不是韭菜", 80, 200),
    "brussels-sprouts": ("抱子甘蓝", "vegetable", "g", "煮熟沥水净重，无配料", 80, 250),
    "peach": ("桃子", "fruit", "g", "带皮可食净重，去核", 80, 200),
    "kiwi": ("猕猴桃", "fruit", "g", "去皮可食净重", 80, 200),
    "blueberry": ("蓝莓", "fruit", "g", "鲜果可食净重，无配料", 60, 150),
    "pineapple": ("菠萝", "fruit", "g", "去皮可食果肉净重", 80, 200),
    "grapes": ("葡萄", "fruit", "g", "可食净重，去梗去籽", 80, 200),
    "walnuts": ("核桃仁", "fat", "g", "原味去壳果仁净重，无配料", 5, 25),
    "peanuts": ("花生仁", "fat", "g", "无盐原味去壳净重，无配料", 5, 25),
    "cashews": ("腰果仁", "fat", "g", "原味去壳果仁净重，无配料", 5, 25),
    "sunflower-seeds": ("葵花籽仁", "fat", "g", "原味去壳籽仁净重，无配料", 5, 25),
    "pumpkin-seeds": ("南瓜籽仁", "fat", "g", "原味去壳籽仁净重，无配料", 5, 25),
    "hazelnuts": ("榛子仁", "fat", "g", "原味去壳果仁净重，无配料", 5, 25),
}
MEAT = {"chicken", "beef", "shrimp", "pork", "salmon", "cod", "lamb", "duck", "turkey", "tuna"}
# Product availability preference, not a nutritional judgment or an allergy rule.
ON_REQUEST_FOODS = {"couscous", "turkey", "lentils", "chickpeas", "beetroot", "leek", "brussels-sprouts"}
# Bread recipes vary; without a verified package, do not promise a vegan recipe.
ANIMAL_FOODS = MEAT | {"egg", "milk", "yogurt", "egg-noodles", "wholemeal-bread"}
QUICK_MAX = {"rice": 400, "brown-rice": 400, "potato": 450, "sweet-potato": 450,
             "corn": 400, "chicken": 250, "beef": 250, "shrimp": 250, "tofu": 300,
             "barley": 400, "wholewheat-pasta": 400, "rice-noodles": 400,
             "pork": 250, "salmon": 250, "cod": 250, "lentils": 300, "chickpeas": 300,
             "lamb": 250, "duck": 250, "turkey": 250, "tuna": 250, "soybeans": 250,
             "kidney-beans": 300, "egg-noodles": 400, "couscous": 400}
# Bounds catch malformed outputs; they are not clinical portion recommendations.
ALIASES = {
    "米饭": {"rice", "brown-rice"}, "大米": {"rice", "brown-rice"}, "糙米": {"brown-rice"},
    "燕麦": {"oats"}, "燕麦片": {"oats"}, "土豆": {"potato"}, "马铃薯": {"potato"},
    "玉米": {"corn"}, "红薯": {"sweet-potato"}, "地瓜": {"sweet-potato"}, "薯类": {"potato", "sweet-potato"},
    "鸡肉": {"chicken"}, "鸡胸肉": {"chicken"}, "肉类": MEAT, "肉": MEAT,
    "牛肉": {"beef"}, "虾": {"shrimp"}, "虾肉": {"shrimp"}, "虾仁": {"shrimp"},
    "海鲜": {"shrimp", "salmon", "cod"}, "甲壳类": {"shrimp"}, "虾蟹": {"shrimp"},
    "鸡蛋": {"egg"}, "蛋类": {"egg"}, "蛋": {"egg"},
    "豆腐": {"tofu"}, "大豆": {"tofu"}, "黄豆": {"tofu"}, "豆类": {"tofu", "lentils", "chickpeas", "green-beans"}, "豆制品": {"tofu"},
    "西兰花": {"broccoli"}, "西蓝花": {"broccoli"}, "胡萝卜": {"carrot"}, "菠菜": {"spinach"},
    "橄榄油": {"olive-oil"}, "橄榄": {"olive-oil"}, "食用油": {"olive-oil", "rapeseed-oil", "sesame-oil"}, "油": {"olive-oil", "rapeseed-oil", "sesame-oil"},
    "大麦": {"barley"}, "大麦饭": {"barley"}, "荞麦": {"buckwheat"}, "荞麦米": {"buckwheat"}, "藜麦": {"quinoa"},
    "全麦意面": {"wholewheat-pasta"}, "意面": {"wholewheat-pasta"}, "小麦": {"wholewheat-pasta"},
    "麸质": {"wholewheat-pasta", "barley", "oats"}, "米粉": {"rice-noodles"}, "原味米粉": {"rice-noodles"},
    "猪肉": {"pork"}, "猪里脊": {"pork"}, "瘦猪里脊": {"pork"},
    "鱼": {"salmon", "cod"}, "鱼肉": {"salmon", "cod"}, "鱼类": {"salmon", "cod"},
    "三文鱼": {"salmon"}, "鳕鱼": {"cod"}, "小扁豆": {"lentils"}, "鹰嘴豆": {"chickpeas"},
    "卷心菜": {"cabbage"}, "包菜": {"cabbage"}, "圆白菜": {"cabbage"},
    "菜花": {"cauliflower"}, "花菜": {"cauliflower"}, "黄瓜": {"cucumber"}, "番茄": {"tomato"}, "西红柿": {"tomato"},
    "茄子": {"aubergine"}, "青甜椒": {"green-pepper"}, "青椒": {"green-pepper"}, "西葫芦": {"courgette"},
    "白蘑菇": {"mushroom"}, "蘑菇": {"mushroom"}, "菌菇": {"mushroom"}, "小白菜": {"pakchoi"}, "上海青": {"pakchoi"},
    "四季豆": {"green-beans"}, "菜籽油": {"rapeseed-oil"}, "油菜籽": {"rapeseed-oil"},
    "芝麻": {"sesame-oil"}, "芝麻油": {"sesame-oil"}, "香油": {"sesame-oil"},
    "杏仁": {"almonds"}, "巴旦木": {"almonds"}, "坚果": {"almonds"},
    "苹果": {"apple"}, "香蕉": {"banana"}, "橙子": {"orange"}, "橙": {"orange"}, "梨": {"pear"}, "亚洲梨": {"pear"}, "草莓": {"strawberry"},
    "牛奶": {"milk"}, "奶": {"milk", "yogurt"}, "奶制品": {"milk", "yogurt"}, "乳制品": {"milk", "yogurt"},
    "乳糖": {"milk", "yogurt"}, "低脂纯牛奶": {"milk"}, "纯牛奶": {"milk"}, "酸奶": {"yogurt"}, "原味低脂酸奶": {"yogurt"},
}
# Broader names must cover every newly admitted member, not just the old catalog.
ALIASES.update({
    "全麦面包": {"wholemeal-bread"}, "面包": {"wholemeal-bread"},
    "鸡蛋面": {"egg-noodles"}, "蛋面": {"egg-noodles"}, "库斯库斯": {"couscous"}, "蒸粗麦粉": {"couscous"},
    "羊肉": {"lamb"}, "瘦羊肉": {"lamb"}, "鸭肉": {"duck"}, "去皮鸭肉": {"duck"},
    "火鸡": {"turkey"}, "火鸡胸肉": {"turkey"}, "金枪鱼": {"tuna"},
    "禽肉": {"chicken", "duck", "turkey"}, "家禽": {"chicken", "duck", "turkey"},
    "熟黄豆": {"soybeans"}, "红腰豆": {"kidney-beans"}, "熟红腰豆": {"kidney-beans"},
    "青豌豆": {"peas"}, "豌豆": {"peas"}, "芦笋": {"asparagus"}, "甜菜根": {"beetroot"},
    "生菜": {"lettuce"}, "芹菜": {"celery"}, "南瓜": {"pumpkin"}, "白萝卜": {"radish"},
    "洋葱": {"onion"}, "韭葱": {"leek"}, "抱子甘蓝": {"brussels-sprouts"},
    "桃子": {"peach"}, "桃": {"peach"}, "猕猴桃": {"kiwi"}, "奇异果": {"kiwi"},
    "蓝莓": {"blueberry"}, "菠萝": {"pineapple"}, "凤梨": {"pineapple"}, "葡萄": {"grapes"},
    "核桃": {"walnuts"}, "核桃仁": {"walnuts"}, "花生": {"peanuts"}, "花生仁": {"peanuts"},
    "腰果": {"cashews"}, "腰果仁": {"cashews"}, "葵花籽": {"sunflower-seeds"}, "葵花籽仁": {"sunflower-seeds"},
    "南瓜籽": {"pumpkin-seeds"}, "南瓜籽仁": {"pumpkin-seeds"}, "榛子": {"hazelnuts"}, "榛子仁": {"hazelnuts"},
})
for word in ("鱼", "鱼肉", "鱼类", "海鲜"):
    ALIASES[word] = ALIASES[word] | {"tuna"}
for word in ("大豆", "黄豆", "豆制品"):
    ALIASES[word] = ALIASES[word] | {"soybeans"}
ALIASES["豆类"] = ALIASES["豆类"] | {"soybeans", "kidney-beans", "peas", "peanuts"}
ALIASES["坚果"] = ALIASES["坚果"] | {"walnuts", "cashews", "hazelnuts", "peanuts"}
for word in ("小麦", "麸质"):
    ALIASES[word] = ALIASES[word] | {"wholemeal-bread", "egg-noodles", "couscous"}
# Composition proxies do not prove packaging safety. Exclude uncertain recipes conservatively.
ALLERGEN_LINKS = {"egg": {"wholewheat-pasta", "egg-noodles", "wholemeal-bread"},
                  "tofu": {"soybeans", "wholemeal-bread"}, "soybeans": {"tofu", "wholemeal-bread"},
                  "sesame-oil": {"wholemeal-bread"}, "milk": {"yogurt", "wholemeal-bread"}, "yogurt": {"milk", "wholemeal-bread"},
                  "rice": {"rice-noodles"}, "brown-rice": {"rice-noodles"}}
NONE = {"", "无", "没有", "暂无", "无过敏", "没有过敏", "无特殊限制", "none", "n/a"}
SOFT = {"清淡", "口味清淡", "少油", "少盐", "喜欢辣", "常在食堂吃饭", "食堂", "自己做饭",
        "在外饮食", "经常在外吃饭", "外食", "经常外食", "外卖", "味甜系", "口味偏甜", "偏甜", "喜欢甜食", "喜欢甜", "爱吃甜食"}
MEDICAL = re.compile(r"孕|哺乳|儿童|未成年|青少年|(?<!\d)(?:1[0-8]|[0-9])\s*岁|糖尿|肾|肝病|高血压|心脏|疾病|服药|用药|处方|暴食|厌食|催吐|低体重|吞咽|乳糜泻|pregnan|diabet|child|medical|kidney", re.I)
RULE = re.compile(r"(?:我)?(?:对|不吃|不喝|不爱吃|不喜欢吃|不喜欢|避免|忌口[:：]?|过敏[:：]?|禁忌[:：]?)?(" +
                  "|".join(sorted(ALIASES, key=len, reverse=True)) + r")(?:过敏|不耐受|不能吃|禁食)?")


def catalog(*, quick=False):
    return [{"id": key, "name": value[0], "group": value[1], "unit": value[2], "basis": value[3],
             "min": value[4], "max": QUICK_MAX.get(key, value[5]) if quick else value[5]} for key, value in FOODS.items()]


def exclusions(profile, temporary=(), plant_only=False):
    if MEDICAL.search(" ".join(str(profile.get(key, "")) for key in
                               ("preferences", "food_allergies"))):
        raise ModelError("PLAN_SCOPE", "档案涉及特殊饮食管理情况，当前单餐建议不适用，请先咨询合适的专业人士。", 409)
    blocked = set(temporary)
    if any(key not in FOODS for key in blocked):
        raise ModelError("INVALID_INPUT", "本次避开的食材不在候选范围内。", 422)
    if plant_only:
        blocked.update(ANIMAL_FOODS)
    for field in ("food_allergies", "preferences"):
        raw = profile.get(field, "").strip().lower()
        if raw in NONE:
            continue
        for number, part in enumerate(re.split(r"[，,；;、\n]+|和|以及", raw), 1):
            part = part.strip().rstrip("。.")
            if not part:
                continue
            if field == "preferences" and part in SOFT:
                continue
            if part in {"纯素", "素食", "吃素", "全素"}:
                blocked.update(ANIMAL_FOODS)
                continue
            match = RULE.fullmatch(part)
            if not match:
                label = "食物过敏与禁忌" if field == "food_allergies" else "口味与就餐习惯"
                raise ModelError("PLAN_CONSTRAINT_UNRESOLVED", f"档案“{label}”第{number}项暂不能完整识别。请保留原意，分别写明具体食材或习惯，例如“牛肉过敏；鸡肉过敏”“在外饮食；口味偏甜”。复杂限制仍需进一步核对，不要删除真实限制来绕过。", 409)
            blocked.update(ALIASES[match[1]])
    for key in list(blocked):
        blocked.update(ALLERGEN_LINKS.get(key, ()))
    if any(not any(value[1] == group and key not in blocked for key, value in FOODS.items())
           for group in ("starch", "protein", "vegetable")):
        raise ModelError("PLAN_OPTIONS_INSUFFICIENT", "现有候选食材无法满足你的限制，本次不生成；需要先扩展可核对的食材。", 409)
    return sorted(blocked)
