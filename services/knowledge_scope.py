"""Bounded general-frequency grammar shared by chat and knowledge scope checks."""
import re


TRAINING_TOPIC = re.compile(r"力量(?:训练|锻炼)|肌肉强化(?:活动|运动|训练)?|抗阻(?:锻炼|训练|运动)|阻力训练|有氧(?:运动|训练|锻炼)")
FREQUENCY = re.compile(r"每周|一周|星期|礼拜|每[七7]天|频率|间隔|隔多久")
FREQUENCY_QUESTION = re.compile(r"几(?:次|天|日)|多少(?:次|天|日)|多久|频率.*(?:多少|怎样|如何|怎么)")
PERSONAL_CONTEXT = re.compile(
    r"我|你|您|他|她|本人|个人|自己|咱|帮|给|替|结合|根据|记录|档案|今天|今晚|明天|本周|这周|下周|本次|这次|现在|接下来|剩下"
    r"|计划|方案|分化|菜单|食谱|只有|没有|仅有|只用|只能|不能|不吃|不想吃|不喜欢|不爱吃|只吃|不要|避开|别加|换成|换掉|改成|改为|减少|增加|少吃|多吃"
    r"|伤|(?:周|星期)[一二三四五六日天1-7]|\d+(?:\.\d+)?\s*(?:岁|厘米|cm|千克|公斤|kg)", re.I)
GENERAL_FREQUENCY_VERBS = {"安排", "推荐"}
GENERAL_INQUIRY_PREFIX = re.compile(r"^\s*我想(?:了解|知道|问)(?:一下)?[，,：:\s]*")


def general_training_frequency(message):
    """Recognize a frequency question, never grant personal/medical applicability.

    Other request markers and all health/profile guards are checked by callers.
    Ambiguous or broader planning language keeps the existing conservative path.
    """
    message = GENERAL_INQUIRY_PREFIX.sub("", message, count=1)
    return bool(TRAINING_TOPIC.search(message) and FREQUENCY.search(message)
                and FREQUENCY_QUESTION.search(message) and not PERSONAL_CONTEXT.search(message))


def has_personal_request(message, markers):
    matches = list(markers.finditer(message))
    if not matches:
        return False
    if not general_training_frequency(message):
        return True
    # Only the two verbs are ambiguous in this bounded grammar. Never erase a
    # different personal request merely because a frequency question is present.
    return any(match.group() not in GENERAL_FREQUENCY_VERBS for match in matches)
