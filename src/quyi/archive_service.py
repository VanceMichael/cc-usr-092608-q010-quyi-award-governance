"""档案服务：作品谱系、人员资格事实与疑似重复关联。

关键边界：
- 文本、声明、演职关系等事实只追加、不改写历史版本；
- 名称相似度只生成"疑似重复"线索，关联必须由授权人员依据实质证据确认。
"""

from __future__ import annotations

from datetime import date, datetime
from difflib import SequenceMatcher

from .errors import (
    AuthorizationError,
    DuplicateError,
    DuplicateLinkError,
    NotFoundError,
)
from .models import (
    AgeProof,
    AwardCategory,
    CastAppearance,
    CrewStatus,
    DuplicateSuspect,
    OrgMembership,
    PriorAward,
    RegionalHistory,
    Role,
    TextVersion,
    WorkRecord,
    WorkRelation,
    utc_now,
)
from .store import AwardStore


class ArchiveService:
    def __init__(self, store: AwardStore):
        self.store = store

    # -- 授权 ----------------------------------------------------------------

    def _actor(self, actor_id: str):
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise AuthorizationError(f"操作者未登记：{actor_id}")
        return actor

    def _require_roles(self, actor_id: str, roles: tuple[Role, ...]):
        actor = self._actor(actor_id)
        if actor.role not in roles:
            raise AuthorizationError(f"{actor.name}无权执行该操作（需要{'/'.join(r.value for r in roles)}）")
        return actor

    # -- 作品登记 ------------------------------------------------------------

    def register_work(
        self,
        actor_id: str,
        work_id: str,
        genre: str,
        title: str,
        text_hash: str,
        submitting_org_id: str | None = None,
        *,
        recorded_at: datetime | None = None,
    ) -> WorkRecord:
        """登记参评作品及首版文本。秘书处负责材料登记。"""
        actor = self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        if work_id in self.store.works:
            raise DuplicateError(f"作品标识已存在：{work_id}")
        if not genre.strip() or not title.strip() or not text_hash.strip():
            raise ValueError("曲种、标题与文本指纹不能为空")
        if submitting_org_id and submitting_org_id not in self.store.orgs:
            raise NotFoundError(f"报送单位不存在：{submitting_org_id}")
        version = TextVersion(
            version_id=f"{work_id}-V1",
            title=title,
            text_hash=text_hash,
            note="报送首版",
            recorded_at=recorded_at or utc_now(),
            recorded_by=actor.actor_id,
        )
        work = WorkRecord(
            work_id=work_id,
            genre=genre,
            current_title=title,
            versions=[version],
            submitting_org_id=submitting_org_id,
        )
        self.store.works[work_id] = work
        self.store.log(actor_id, "登记作品", work_id, f"{genre}/{title}")
        return work

    def add_text_version(
        self,
        actor_id: str,
        work_id: str,
        title: str,
        text_hash: str,
        note: str,
    ) -> TextVersion:
        """追加文本版本（含改名）。历史版本原样保留。"""
        actor = self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        work = self.store.work(work_id)
        if work.merged_into:
            raise DuplicateLinkError(f"作品已并入谱系 {work.merged_into}，新版本应登记在主作品下")
        version_id = f"{work_id}-V{len(work.versions) + 1}"
        if any(v.text_hash == text_hash and v.title == title for v in work.versions):
            raise DuplicateError("相同标题与文本指纹的版本已存在")
        version = TextVersion(
            version_id=version_id,
            title=title,
            text_hash=text_hash,
            note=note,
            recorded_at=utc_now(),
            recorded_by=actor.actor_id,
        )
        work.versions.append(version)
        work.current_title = title
        self.store.log(actor_id, "追加文本版本", work_id, f"{version_id}/{title}")
        return version

    def declare_originality(
        self,
        actor_id: str,
        work_id: str,
        version_id: str,
        declared: bool,
        note: str = "",
    ) -> None:
        """登记原创声明。声明事实可补充与撤回声明，但历史声明内容保留在审计日志中。"""
        actor = self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        work = self.store.work(work_id)
        version = work.get_version(version_id)
        if version is None:
            raise NotFoundError(f"文本版本不存在：{version_id}")
        version.originality_declared = declared
        version.originality_note = note
        self.store.log(actor_id, "原创声明", version_id, f"{declared}:{note}")

    def declare_first_performance(
        self, actor_id: str, work_id: str, version_id: str, first_performed_at: date
    ) -> None:
        """登记首演时间。"""
        actor = self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        work = self.store.work(work_id)
        version = work.get_version(version_id)
        if version is None:
            raise NotFoundError(f"文本版本不存在：{version_id}")
        version.first_performed_at = first_performed_at
        self.store.log(actor_id, "首演登记", version_id, str(first_performed_at))

    def add_author(self, actor_id: str, work_id: str, person_id: str) -> None:
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        work = self.store.work(work_id)
        person = self.store.person(person_id)
        if person_id not in work.author_ids:
            work.author_ids.append(person_id)
        self._ensure_appearance(person_id, work_id, work.versions[0].version_id, WorkRelation.AUTHOR)
        self.store.log(actor_id, "登记作者", work_id, person_id)

    def add_rights_holder(self, actor_id: str, work_id: str, person_id: str) -> None:
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        work = self.store.work(work_id)
        self.store.person(person_id)
        if person_id not in work.rights_holder_ids:
            work.rights_holder_ids.append(person_id)
        self._ensure_appearance(person_id, work_id, work.versions[0].version_id, WorkRelation.RIGHTS_HOLDER)
        self.store.log(actor_id, "登记权利人", work_id, person_id)

    # -- 人员资格事实 --------------------------------------------------------

    def register_person(self, actor_id: str, person_id: str, name: str) -> None:
        actor = self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        if person_id in self.store.persons:
            raise DuplicateError(f"人员标识已存在：{person_id}")
        from .models import PersonRecord

        self.store.persons[person_id] = PersonRecord(person_id=person_id, name=name)
        self.store.log(actor_id, "登记人员", person_id, name)

    def record_age_proof(
        self, actor_id: str, person_id: str, birth_date: date, doc_ref: str
    ) -> AgeProof:
        """登记年龄证明。年龄证明可后补，但以证明文件记载为准。"""
        actor = self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        person = self.store.person(person_id)
        if birth_date > date.today():
            raise ValueError("出生日期不能晚于今天")
        proof = AgeProof(
            proof_id=f"AP-{person_id}-{len([1 for p in self.store.persons.values() if p.age_proof]) + 1}",
            birth_date=birth_date,
            doc_ref=doc_ref,
            recorded_at=utc_now(),
            recorded_by=actor.actor_id,
        )
        person.age_proof = proof
        self.store.log(actor_id, "年龄证明", person_id, doc_ref)
        return proof

    def add_membership(
        self,
        actor_id: str,
        person_id: str,
        org_id: str,
        started_from: date,
        ended_at: date | None = None,
        note: str = "",
    ) -> None:
        """登记所属单位关系。同一人可先后或并行隶属多个院团。"""
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        person = self.store.person(person_id)
        if org_id not in self.store.orgs:
            raise NotFoundError(f"单位不存在：{org_id}")
        if ended_at and ended_at < started_from:
            raise ValueError("关系结束时间不能早于开始时间")
        org_name = self.store.orgs[org_id].name
        person.memberships.append(
            OrgMembership(org_id, org_name, started_from, ended_at, note)
        )
        self.store.log(actor_id, "单位关系", person_id, f"{org_id} {started_from}~{ended_at}")

    def add_appearance(
        self,
        actor_id: str,
        person_id: str,
        work_id: str,
        version_id: str,
        relation: WorkRelation,
        org_id: str | None = None,
        detail: str = "",
    ) -> None:
        """登记演职关系（可随不同院团参演同一或不同作品）。"""
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        self._ensure_appearance(person_id, work_id, version_id, relation, org_id, detail)
        self.store.log(actor_id, "演职关系", work_id, f"{person_id}/{relation.value}")

    def _ensure_appearance(
        self,
        person_id: str,
        work_id: str,
        version_id: str,
        relation: WorkRelation,
        org_id: str | None = None,
        detail: str = "",
    ) -> None:
        person = self.store.person(person_id)
        work = self.store.work(work_id)
        if work.get_version(version_id) is None:
            raise NotFoundError(f"文本版本不存在：{version_id}")
        for appearance in person.appearances:
            if (
                appearance.work_id == work_id
                and appearance.version_id == version_id
                and appearance.relation == relation
            ):
                return
        person.appearances.append(
            CastAppearance(work_id, version_id, relation, org_id, CrewStatus.ACTIVE, detail)
        )

    def add_regional_history(
        self, actor_id: str, person_id: str, region: str, round_id: str, year: int, advanced: bool
    ) -> None:
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        person = self.store.person(person_id)
        person.regional_histories.append(RegionalHistory(region, round_id, year, advanced))
        self.store.log(actor_id, "赛区经历", person_id, f"{region}/{advanced}")

    def add_prior_award(
        self,
        actor_id: str,
        person_id: str,
        award: str,
        category: AwardCategory,
        year: int,
        detail: str = "",
    ) -> None:
        """登记历史获奖，用于获奖限制（如新人奖、表演奖限制）。"""
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        person = self.store.person(person_id)
        person.prior_awards.append(PriorAward(award, category, year, detail))
        self.store.log(actor_id, "历史获奖", person_id, f"{award}/{category.value}/{year}")

    # -- 疑似重复 ------------------------------------------------------------

    @staticmethod
    def title_similarity(title_a: str, title_b: str) -> float:
        return SequenceMatcher(None, title_a, title_b).ratio()

    def scan_duplicate_suspects(self, actor_id: str, threshold: float = 0.6) -> list[str]:
        """秘书处可运行名称相似度扫描，但结果只是线索，绝不自动合并。

        文本指纹相同的作品直接作为高可信线索标出，仍须授权人员确认。
        """
        self._require_roles(actor_id, (Role.SECRETARIAT, Role.AUTHORIZED_OFFICER))
        ids = sorted(self.store.works)
        created: list[str] = []
        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                if id_a == id_b:
                    continue
                a, b = self.store.works[id_a], self.store.works[id_b]
                if a.merged_into or b.merged_into:
                    continue
                same_text = bool(a.all_text_hashes() & b.all_text_hashes())
                score = self.title_similarity(a.current_title, b.current_title)
                if not same_text and score < threshold:
                    continue
                basis = "文本指纹相同" if same_text else f"标题相似度{score:.2f}"
                if self._find_suspect(id_a, id_b) is not None:
                    continue
                suspect = DuplicateSuspect(
                    suspect_id=f"DUP-{len(self.store.suspects) + 1:03d}",
                    work_id_a=id_a,
                    work_id_b=id_b,
                    similarity_score=1.0 if same_text else score,
                    basis=basis,
                    created_at=utc_now(),
                )
                self.store.suspects[suspect.suspect_id] = suspect
                created.append(suspect.suspect_id)
        self.store.log(actor_id, "重复线索扫描", "", f"生成{len(created)}条线索")
        return created

    def _find_suspect(self, id_a: str, id_b: str) -> DuplicateSuspect | None:
        for suspect in self.store.suspects.values():
            pair = {suspect.work_id_a, suspect.work_id_b}
            if pair == {id_a, id_b} and not suspect.resolved:
                return suspect
        return None

    # 能证明"同一作品/同一谱系"的证据类型；名称相似、单位证明等不算实质证据
    IDENTITY_EVIDENCE_KINDS = frozenset({"文本同一性鉴定", "权利人同一性证明", "作者身份证明"})

    def confirm_duplicate(
        self,
        actor_id: str,
        suspect_id: str,
        substantiation: str,
        evidence_refs: list[dict],
    ) -> str:
        """确认疑似重复关联。

        只有授权人员可以确认；且必须提供实质证据：
        相同文本指纹、共有作者/权利人，或类型为文本/权利/作者同一性的证明材料。
        仅凭名称相似度（即使附了其他材料）一律拒绝，杜绝按名称直接合并。
        证据格式：``[{"doc_ref": "DOC-1", "kind": "文本同一性鉴定", "summary": "..."}]``。
        返回关联组标识。
        """
        actor = self._require_roles(actor_id, (Role.AUTHORIZED_OFFICER,))
        suspect = self.store.suspects.get(suspect_id)
        if suspect is None:
            raise NotFoundError(f"疑似重复线索不存在：{suspect_id}")
        if suspect.resolved:
            raise DuplicateLinkError("该线索已处理")
        a = self.store.work(suspect.work_id_a)
        b = self.store.work(suspect.work_id_b)

        same_text = bool(a.all_text_hashes() & b.all_text_hashes())
        shared_authors = set(a.author_ids) & set(b.author_ids)
        shared_rights = set(a.rights_holder_ids) & set(b.rights_holder_ids)
        identity_evidence = [
            e for e in evidence_refs
            if isinstance(e, dict) and e.get("kind") in self.IDENTITY_EVIDENCE_KINDS
        ]
        if not substantiation.strip() or not evidence_refs:
            raise DuplicateLinkError("确认关联须填写实质依据并附证明材料")
        if not (same_text or shared_authors or shared_rights or identity_evidence):
            raise DuplicateLinkError(
                "仅有名称相似度不能确认重复，须有相同文本、共有作者/权利人或同一性鉴定证据"
            )

        # 合并到既有组，或新建组（以较早作品为主作品）
        group_id = a.duplicate_group_id or b.duplicate_group_id or f"GRP-{suspect.suspect_id}"
        primary_id = min(x for x in (a.work_id, b.work_id))
        for work, other in ((a, b), (b, a)):
            work.duplicate_group_id = group_id
            if work.work_id != primary_id and work.merged_into is None:
                work.merged_into = primary_id
        link = {
            "suspect_id": suspect_id,
            "group_id": group_id,
            "primary_work_id": primary_id,
            "confirmed_by": actor.actor_id,
            "confirmed_at": utc_now(),
            "substantiation": substantiation,
            "evidence_refs": evidence_refs,
            "same_text": same_text,
            "shared_authors": sorted(shared_authors),
            "shared_rights_holders": sorted(shared_rights),
        }
        a.duplicate_links.append(link)
        b.duplicate_links.append(link)
        suspect.resolved = True
        suspect.resolution = link
        self.store.log(
            actor_id,
            "确认重复关联",
            group_id,
            f"{a.work_id}~{b.work_id}：{substantiation}",
        )
        return group_id

    def reject_suspect(self, actor_id: str, suspect_id: str, reason: str) -> None:
        """授权人员否绝线索：不同作品保持各自独立报送。"""
        actor = self._require_roles(actor_id, (Role.AUTHORIZED_OFFICER,))
        suspect = self.store.suspects.get(suspect_id)
        if suspect is None or suspect.resolved:
            raise DuplicateLinkError("线索不存在或已处理")
        suspect.resolved = True
        suspect.resolution = {"rejected_by": actor.actor_id, "reason": reason, "at": utc_now()}
        self.store.log(actor_id, "否绝重复线索", suspect_id, reason)

    # -- 关系查询（供回避、溯源使用）----------------------------------------

    def work_related_person_ids(self, work_id: str) -> set[str]:
        """与作品存在创作、演职、权利关系的全部人员。"""
        work = self.store.work(work_id)
        related: set[str] = set(work.author_ids) | set(work.rights_holder_ids)
        for person_id, person in self.store.persons.items():
            if any(
                a.work_id == work_id and a.status == CrewStatus.ACTIVE
                for a in person.appearances
            ):
                related.add(person_id)
        return related

    def work_related_org_ids(self, work_id: str) -> set[str]:
        """作品关联单位：报送院团 + 演职人员参演时所属院团。"""
        work = self.store.work(work_id)
        orgs: set[str] = set()
        if work.submitting_org_id:
            orgs.add(work.submitting_org_id)
        for person in self.store.persons.values():
            for appearance in person.appearances:
                if appearance.work_id == work_id and appearance.org_id:
                    orgs.add(appearance.org_id)
        return orgs

    def performers_for_version(self, work_id: str, version_id: str) -> list[str]:
        found: list[str] = []
        for person_id, person in self.store.persons.items():
            if any(
                a.work_id == work_id
                and a.version_id == version_id
                and a.relation == WorkRelation.PERFORMER
                and a.status == CrewStatus.ACTIVE
                for a in person.appearances
            ):
                found.append(person_id)
        return found
