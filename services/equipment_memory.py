"""User-confirmed equipment scopes; private state, never public RAG knowledge."""
import re

from model.factory import ModelError
from services import business_time
from services.profile_facts import equipment_revision


def today():
    return business_time.business_today().isoformat()


def day_snapshot(connection, user_id, day):
    row = connection.execute("SELECT * FROM coach_day_equipment WHERE user_id=?", (user_id,)).fetchone()
    result = {"business_day": today(), "revision": row["revision"] if row else 0, "active": None}
    if (row and row["value"] and row["conversation_id"] and row["valid_day"] == day == result["business_day"]
            and row["profile_revision"] == equipment_revision(connection, user_id)):
        result["active"] = dict(row)
    return result


def save_day(connection, user_id, value, day, conversation_id, turn_version):
    # Caller owns the transaction and has rechecked the review fingerprint.
    source = connection.execute("SELECT 1 FROM coach_conversations c JOIN coach_turns t ON t.conversation_id=c.id "
        "WHERE c.id=? AND c.user_id=? AND c.day=? AND t.version=?",
        (conversation_id, user_id, day, turn_version)).fetchone()
    if day != today() or source is None:
        raise ModelError("COACH_REVIEW_STALE", "今天的器械只能确认到当前日期，请重新核对。", 409)
    connection.execute("INSERT INTO coach_day_equipment(user_id,value,valid_day,revision,profile_revision,conversation_id,turn_version) "
        "VALUES (?,?,?,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value,valid_day=excluded.valid_day,"
        "revision=coach_day_equipment.revision+1,profile_revision=excluded.profile_revision,"
        "conversation_id=excluded.conversation_id,turn_version=excluded.turn_version,updated_at=CURRENT_TIMESTAMP",
        (user_id, value, day, equipment_revision(connection, user_id), conversation_id, turn_version))
    return day_snapshot(connection, user_id, day)["revision"]


def revoke_day(connection, user_id, conversation_id=None):
    # Keep the counter after deleting the value: A→B→A must not resurrect a draft.
    connection.execute("UPDATE coach_day_equipment SET value='',revision=revision+1,updated_at=CURRENT_TIMESTAMP "
        "WHERE user_id=? AND value<>'' AND (? IS NULL OR conversation_id=?)",
        (user_id, conversation_id, conversation_id))


class EquipmentNeedsInput(Exception):
    """A supported clarification state, distinct from an untrusted model output."""
    def __init__(self, reason, question, clarification="which_training"):
        super().__init__(question)
        self.reason, self.question, self.clarification = reason, question, clarification


def check_equipment_context(source):
    """Limited language floors; no fact extraction and no implicit scope choice."""
    from services.training_recommendations import equipment_tokens
    if not any(word in source for word in (*equipment_tokens(), "器械", "设备")):
        return
    if re.search(r"明天|后天|昨天|上周|下周|这周|本周|周末|最近|这几天|未来|[一二三四五六七八九十两\d]+[天日周月]|\d{1,4}[-/年]\d", source):
        raise EquipmentNeedsInput("unsupported_time", "这条器械条件涉及过去、未来或多天，目前不能提前应用。请补充今天实际可用的器械；若要长期保存，请另行明确说明。")
    session = bool(re.search(r"本次|这次|这一回|接下来", source))
    daily = bool(re.search(r"今天|今日|今晚|当天", source))
    durable = bool(re.search(r"长期|以后|今后|平时|通常|一直|每次", source))
    if durable and (session or daily):
        raise EquipmentNeedsInput("mixed_scopes", "这句话同时包含长期和临时器械条件，请分开核对。先说明今天实际可用的器械，再单独说明长期条件；两项目前都未应用。", "multiple_requests")
    if re.search(r"可能|也许|大概|不确定|不清楚|没(?:有)?(?:确认|确定|核实)|尚未(?:确认|确定|核实)|如果|假如|假设", source):
        raise EquipmentNeedsInput("uncertain", "这些器械是否实际可用还不确定。请确认后补充可用和不可用的器械，以及是本次、今天还是长期条件；目前不会保存为事实。")


def candidate_from_model(candidate, scope, originals, day, update=None):
    """Exact provenance + supported equipment + conservative temporal floors.

    These checks bound the semantic model; they do not prove arbitrary language.
    The confirmation card remains required even when the scope is explicit.
    """
    if candidate is None:
        return None
    from services.training_recommendations import equipment, denied_equipment
    source = originals.get(candidate.source_version)
    if (scope not in ("aerobic", "strength") or source != candidate.source_text
            or candidate.value not in source
            or not denied_equipment(source) <= denied_equipment(candidate.value)):
        raise ModelError("COACH_INVALID_OUTPUT", "器械记忆无法完整对应原话，未应用；请保留器械及不可用条件。")
    check_equipment_context(source)
    if (equipment(candidate.value)[1]
            or update and update.equipment is not None and update.equipment != candidate.value):
        raise ModelError("COACH_INVALID_OUTPUT", "器械记忆无法完整对应原话，未应用；请保留器械及不可用条件。")
    session = bool(re.search(r"本次|这次|这一回|接下来", source))
    daily = bool(re.search(r"今天|今日|今晚|当天", source))
    durable = bool(re.search(r"长期|以后|今后|平时|通常|一直|每次", source))
    # Explicit temporal qualifiers cannot be promoted by the model. Unknown
    # scopes require a human choice rather than silent permanent storage.
    if session:
        proposed = "session"
    elif daily:
        proposed = "day"
    elif durable:
        proposed = "long_term"
    else:
        proposed = "unspecified"
    if proposed == "day" and day != today():
        raise EquipmentNeedsInput("expired_day", "这段对话的日期已经不是今天，请在今天的对话中重新说明并确认当天器械。")
    return {**candidate.model_dump(), "scope": proposed}


def scoped_evidence(binding, profile, revision):
    if binding.get("profile_revision") != revision:
        return False
    if binding.get("scope") == "long_term":
        return binding.get("value") == profile
    # Temporary conditions are useful only in their live context, not as warm
    # or cold cross-session facts. Day conditions are loaded from SQL directly.
    return False
