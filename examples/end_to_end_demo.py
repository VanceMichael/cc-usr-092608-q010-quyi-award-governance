"""端到端流程演示（虚构数据）：从四百余件报送的档案治理到获奖溯源。

运行：``python3 examples/end_to_end_demo.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date, datetime, timedelta, timezone
from src.quyi.store import seed_store
from src.quyi.archive_service import ArchiveService
from src.quyi.round_service import RoundService
from src.quyi.jury_service import JuryService
from src.quyi.live_event_service import LiveEventService
from src.quyi.appeal_service import AppealService
from src.quyi.award_service import AwardService
from src.quyi.publicity_service import PublicityService
from src.quyi.models import *

store = seed_store()
arch = ArchiveService(store)
rnd = RoundService(store)
jury = JuryService(store, arch)
live = LiveEventService(store, arch, jury)
appeal_svc = AppealService(store)
award = AwardService(store)
pub = PublicityService(store)

SEC, ORG, REV, OFF = "sec-zhang", "org-chen", "reviewer-zhao", "officer-li"
T = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=8)))

arch.register_person(SEC, "P-004", "冯某（虚构评委）")
arch.register_person(SEC, "P-005", "陈某（虚构评委）")
arch.register_person(SEC, "P-007", "卫某（虚构作者）")
arch.register_work(SEC, "W-020", "快板", "竹林晨曲", "hash-text-zhu")
arch.declare_originality(SEC, "W-020", "W-020-V1", True, "原创")
arch.add_author(SEC, "W-020", "P-007")
arch.add_rights_holder(SEC, "W-020", "P-007")

# 1. 重复：扫描只产生线索，确认必须授权人员 + 实质证据
dup_ids = arch.scan_duplicate_suspects(SEC)
assert len(dup_ids) == 1
try:
    arch.confirm_duplicate(SEC, dup_ids[0], "文本指纹相同", [{"doc_ref": "DOC-1", "kind": "文本同一性鉴定"}])
    raise AssertionError("secretariat must not confirm")
except Exception as e:
    assert "无权" in str(e)
grp = arch.confirm_duplicate(
    OFF, dup_ids[0], "两报送件文本指纹一致且作者同一",
    [{"doc_ref": "DOC-1", "kind": "文本同一性鉴定", "summary": "指纹一致"}],
)
assert store.works["W-002"].merged_into == "W-001"

# 名称相似但无实质证据：即使附了非同一性材料也不能合并
arch.register_work(SEC, "W-010", "评书", "湖畔风声", "hash-text-other-x")
arch.scan_duplicate_suspects(SEC, threshold=0.5)
sim_suspect = next(s for s in store.suspects.values() if s.basis.startswith("标题相似度"))
try:
    arch.confirm_duplicate(
        OFF, sim_suspect.suspect_id, "名字像",
        [{"doc_ref": "DOC-2", "kind": "单位证明", "summary": "同市"}],
    )
    raise AssertionError("should reject name-only")
except Exception as e:
    assert "名称相似度" in str(e)
arch.reject_suspect(OFF, sim_suspect.suspect_id, "文本与作者均不同")

# 2. 初评封存
rnd.create_round(ORG, "R-PRE", RoundKind.PRELIMINARY, "本届初评", year=2026)
for cat in ("报名表", "文本", "原创声明", "权利人证明"):
    rnd.add_material(SEC, "R-PRE", f"doc-{cat}", f"hash-{cat}", cat)
rnd.record_advancement(SEC, "R-PRE", "C-001", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", True)
rnd.record_advancement(SEC, "R-PRE", "C-002", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True)
rnd.record_advancement(SEC, "R-PRE", "C-003", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True)
rnd.seal_round(SEC, "R-PRE")
assert rnd.verify_seal("R-PRE")
try:
    rnd.add_material(SEC, "R-PRE", "x", "y", "报名表")
    raise AssertionError("sealed mutation")
except Exception as e:
    assert "封存" in str(e)

# 3. 分赛区封存；后补证明只进复核
rnd.create_round(ORG, "R-REG-EAST", RoundKind.REGIONAL, "华东分赛区", region="华东", year=2026)
for cat in ("报名表", "文本", "原创声明", "权利人证明", "参赛确认"):
    rnd.add_material(SEC, "R-REG-EAST", f"doc-e-{cat}", f"hash-e-{cat}", cat)
rnd.record_advancement(SEC, "R-REG-EAST", "C-101", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", True)
rnd.record_advancement(SEC, "R-REG-EAST", "C-102", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True)
rnd.record_advancement(SEC, "R-REG-EAST", "C-120", AwardCategory.PROGRAM, "W-020", "W-020-V1", None, True)
rnd.seal_round(SEC, "R-REG-EAST")
eid = rnd.submit_supplement(SEC, "C-102", "R-REG-EAST", "SUP-DOC-1", "年龄证明", "2002-09-01")
rnd.review_supplement(REV, eid, True, "采信，年龄合规", "终评资格复核")
assert rnd.verify_seal("R-REG-EAST")
assert len(store.rounds["R-REG-EAST"].materials) == 5

# 4. 终评：回避 + 匿名评分
rnd.create_round(ORG, "R-FIN", RoundKind.FINAL, "现场终评第一场", year=2026)
rnd.record_advancement(SEC, "R-FIN", "C-201", AwardCategory.PERFORMANCE, "W-001", "W-001-V1", "P-002", True)
rnd.record_advancement(SEC, "R-FIN", "C-202", AwardCategory.NEWCOMER, "W-001", "W-001-V1", "P-003", True)
rnd.record_advancement(SEC, "R-FIN", "C-203", AwardCategory.PROGRAM, "W-001", "W-001-V1", None, True)

jury.register_judge(ORG, "J-1", "P-001", "钱某评委", org_id="org-a")
jury.declare_relation(ORG, "J-1", "P-002", "师生")
jury.register_judge(ORG, "J-4", "P-004", "冯某评委")
jury.register_judge(ORG, "J-5", "P-005", "陈某评委")
jury.issue_alias(ORG, "J-1", "R-FIN")
jury.issue_alias(ORG, "J-4", "R-FIN")
jury.issue_alias(ORG, "J-5", "R-FIN")
recs = jury.evaluate_recusals(ORG, "R-FIN")
for r in recs:
    print("RECUSAL", r.judge_id, r.reason.value, "|", r.detail)
assert jury.is_recused("J-1", "R-FIN", "W-001")
assert not jury.is_recused("J-4", "R-FIN", "W-001")
try:
    jury.resolve_alias(SEC, "J-R-FIN-01")
    raise AssertionError("secretariat resolve")
except Exception as e:
    assert "无权" in str(e)
assert jury.resolve_alias(ORG, "J-R-FIN-02") == "J-4"

jury.submit_score("J-R-FIN-02", "R-FIN", "W-001", "W-001-V1", 90)
jury.submit_score("J-R-FIN-03", "R-FIN", "W-001", "W-001-V1", 88)
try:
    jury.submit_score("J-R-FIN-01", "R-FIN", "W-001", "W-001-V1", 80)
    raise AssertionError("recused scored")
except Exception as e:
    assert "回避" in str(e)
ledger = jury.ledger_for_secretariat(SEC, "R-FIN")
allowed = {"score_id", "work_id", "version_id", "judge_alias", "value", "superseded", "locked_by_appeal"}
assert all(set(r) <= allowed for r in ledger)

# 5. 换角
arch.register_person(SEC, "P-006", "褚某（虚构青年演员）")
arch.record_age_proof(SEC, "P-006", date(2000, 1, 1), "虚构证件D")
arch.add_membership(SEC, "P-006", "org-a", date(2019, 1, 1))
arch.add_appearance(SEC, "P-006", "W-001", "W-001-V1", WorkRelation.OTHER_CREW, "org-a")
ev = live.record_event(ORG, "R-FIN", "W-001", EventType.CAST_CHANGE, "演员伤病换角",
                       occurred_at=T, outgoing_person_id="P-002", incoming_person_id="P-006")
new_v = ev.new_version_id
assert store.candidacies["C-201"].status == CandidacyStatus.REVOKED
new_cand = next(c for c in store.candidacies.values() if c.person_id == "P-006" and c.source_round_id == "R-FIN")
print("new candidacy after cast change:", new_cand.candidacy_id)
try:
    jury.submit_score("J-R-FIN-02", "R-FIN", "W-001", "W-001-V1", 70)
    raise AssertionError("old version scoring allowed")
except Exception as e:
    assert "取代" in str(e)
jury.submit_score("J-R-FIN-02", "R-FIN", "W-001", new_v, 92)
assert all(s.superseded for s in store.scores.values() if s.version_id == "W-001-V1")

# 6. 中止 + 重新表演
rnd.record_advancement(SEC, "R-FIN", "C-299", AwardCategory.PROGRAM, "W-020", "W-020-V1", None, True)
jury.submit_score("J-R-FIN-02", "R-FIN", "W-020", "W-020-V1", 77)
live.record_event(ORG, "R-FIN", "W-020", EventType.SUSPENSION, "设备故障中止", occurred_at=T)
assert live.is_suspended("R-FIN", "W-020")
ev2 = live.record_event(ORG, "R-FIN", "W-020", EventType.REPERFORMANCE, "恢复后重新表演",
                        occurred_at=T + timedelta(minutes=20))
assert not live.is_suspended("R-FIN", "W-020")
assert all(s.superseded for s in store.scores.values() if s.work_id == "W-020" and s.version_id == "W-020-V1")
jury.submit_score("J-R-FIN-02", "R-FIN", "W-020", ev2.new_version_id, 95)

# 7. 申诉精确锁定，无关候选照常确认
score_id = next(s.score_id for s in store.scores.values()
                if s.work_id == "W-020" and not s.superseded and s.version_id == ev2.new_version_id)
apl = appeal_svc.file_appeal(SEC, AppealTarget.SCORE, "质疑该评分异常",
                             candidacy_id="C-299", work_id="W-020", score_ids=[score_id])
try:
    jury.submit_score("J-R-FIN-02", "R-FIN", "W-020", ev2.new_version_id, 60)
    raise AssertionError("locked score modified")
except Exception as e:
    assert "锁定" in str(e)
assert award.can_confirm("C-202", date(2026, 9, 25)) == []
res = award.confirm_award(ORG, "C-202", date(2026, 9, 25))
print("confirmed:", res.category.value, res.candidacy_id)
blocked = award.can_confirm("C-299", date(2026, 9, 25))
assert any("申诉" in b or "锁定" in b for b in blocked), blocked
appeal_svc.resolve_appeal(ORG, apl, True, "评分依据不足，予以排除")
assert store.scores[score_id].excluded

# 8. 兼得规则：同一作品节目奖与文学奖不得兼得
rnd.record_advancement(SEC, "R-FIN", "C-301", AwardCategory.LITERATURE, "W-001", new_v, None, True)
award.confirm_award(ORG, "C-203", date(2026, 9, 25))
try:
    award.confirm_award(ORG, "C-301", date(2026, 9, 25))
    raise AssertionError("exclusive allowed")
except Exception as e:
    assert "兼得" in str(e)
print("quota:", award.quota_status())

# 9. 公示停机顺延，剩余期限不缩短
p0 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone(timedelta(hours=8)))
p1 = p0 + timedelta(days=7)
pub.start_publicity(ORG, "PUB-1", p0, p1)
pub.report_downtime_start(SEC, "PUB-1", p0 + timedelta(days=2), "机房迁移")
ext = pub.report_downtime_end(SEC, "PUB-1", p0 + timedelta(days=3))
assert ext == timedelta(days=1), ext
assert pub.effective_deadline("PUB-1") == p1 + timedelta(days=1)
try:
    pub.end_publicity(ORG, "PUB-1", p1 + timedelta(hours=12))
    raise AssertionError("early end allowed")
except Exception as e:
    assert "不得缩短" in str(e)
deadline = pub.end_publicity(ORG, "PUB-1", p1 + timedelta(days=1, hours=1))
assert deadline == p1 + timedelta(days=1)

# 10. 全链溯源
trace = award.trace_result("C-202")
assert trace["work_lineage"]["duplicate_group_id"] == grp
assert len(trace["work_lineage"]["versions"]) >= 2
assert any(r["reason"] for r in trace["recusals"])
assert {r["round_id"] for r in trace["sealed_rounds"]} >= {"R-PRE", "R-REG-EAST"}
# C-299 溯源能看到改变裁定的申诉
trace299 = award.trace_result("C-299")
a299 = next(a for a in trace299["appeals"] if a["appeal_id"] == apl)
assert a299["changed_this_ruling"] is True
assert any(s["state"] == "已被申诉排除" for s in trace299["scores"])
print("trace sections:", sorted(trace))
print("ALL SMOKE OK")
