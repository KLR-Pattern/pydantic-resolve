from __future__ import annotations

from asgiref.sync import sync_to_async
import pytest
from aiodataloader import DataLoader
from django.db.models import Q
from pydantic import BaseModel, ConfigDict

from pydantic_resolve.integration.django.loader import (
    create_many_to_many_loader,
    create_many_to_one_loader,
    create_one_to_many_loader,
    create_reverse_one_to_one_loader,
)

from tests.integration.django.dto import CourseDTO, SchoolDTO, StudentDTO, StudentProfileDTO
from tests.integration.django.models import CourseOrm, SchoolOrm, StudentOrm, StudentProfileOrm


async def _run_sync(fn):
    return await sync_to_async(fn, thread_sensitive=True)()


@pytest.mark.asyncio
async def test_create_many_to_one_loader(seeded_db):
    loader_kls = create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_field_name="id",
    )

    assert issubclass(loader_kls, DataLoader)

    result = await loader_kls().load_many([1, 2, 999])

    assert isinstance(result[0], SchoolOrm)
    assert result[0].id == 1
    assert result[0].name == "School-A"
    assert isinstance(result[1], SchoolOrm)
    assert result[1].id == 2
    assert result[1].name == "School-B"
    assert result[2] is None


@pytest.mark.asyncio
async def test_create_one_to_many_loader(seeded_db):
    loader_kls = create_one_to_many_loader(
        source_orm_kls=SchoolOrm,
        rel_name="students",
        target_orm_kls=StudentOrm,
        target_dto_kls=StudentDTO,
        target_relation_field_name="school_id",
    )

    result = await loader_kls().load_many([1, 2, 999])

    assert sorted(student.name for student in result[0]) == ["Alice", "Bob"]
    assert [student.name for student in result[1]] == ["Cathy"]
    assert result[2] == []


@pytest.mark.asyncio
async def test_create_many_to_many_loader(seeded_db):
    loader_kls = create_many_to_many_loader(
        source_orm_kls=StudentOrm,
        rel_name="courses",
        source_match_field_name="id",
        target_orm_kls=CourseOrm,
        target_dto_kls=CourseDTO,
    )

    result = await loader_kls().load_many([1, 2, 3, 999])

    assert sorted(course.title for course in result[0]) == ["Math", "Science"]
    assert [course.title for course in result[1]] == ["Science"]
    assert result[2] == []
    assert result[3] == []


@pytest.mark.asyncio
async def test_create_reverse_one_to_one_loader(seeded_db):
    loader_kls = create_reverse_one_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="profile",
        target_orm_kls=StudentProfileOrm,
        target_dto_kls=StudentProfileDTO,
        target_relation_field_name="student_id",
    )

    result = await loader_kls().load_many([1, 2, 3, 999])

    assert isinstance(result[0], StudentProfileOrm)
    assert result[0].id == 100
    assert result[0].student_id == 1
    assert result[0].nickname == "ali"
    assert result[1] is None
    assert isinstance(result[2], StudentProfileOrm)
    assert result[2].id == 300
    assert result[2].student_id == 3
    assert result[2].nickname == "cat"
    assert result[3] is None


@pytest.mark.asyncio
async def test_many_to_one_loader_applies_filters(seeded_db):
    await _run_sync(lambda: SchoolOrm.objects.create(id=99, name="Deleted-School", deleted=True))

    loader_kls = create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_field_name="id",
        filters=[Q(deleted=False)],
    )

    result = await loader_kls().load_many([1, 99])

    assert isinstance(result[0], SchoolOrm)
    assert result[0].id == 1
    assert result[0].name == "School-A"
    assert result[1] is None


@pytest.mark.asyncio
async def test_one_to_many_loader_applies_filters(seeded_db):
    school = await _run_sync(lambda: SchoolOrm.objects.get(id=1))
    await _run_sync(lambda: StudentOrm.objects.create(id=99, name="Ghost", school=school, deleted=True))

    loader_kls = create_one_to_many_loader(
        source_orm_kls=SchoolOrm,
        rel_name="students",
        target_orm_kls=StudentOrm,
        target_dto_kls=StudentDTO,
        target_relation_field_name="school_id",
        filters=[Q(deleted=False)],
    )

    result = await loader_kls().load_many([1])
    assert sorted(student.name for student in result[0]) == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_many_to_many_loader_applies_filters(seeded_db):
    student = await _run_sync(lambda: StudentOrm.objects.get(id=1))
    course = await _run_sync(lambda: CourseOrm.objects.create(id=99, title="Ghost", deleted=True))
    await _run_sync(lambda: student.courses.add(course))

    loader_kls = create_many_to_many_loader(
        source_orm_kls=StudentOrm,
        rel_name="courses",
        source_match_field_name="id",
        target_orm_kls=CourseOrm,
        target_dto_kls=CourseDTO,
        filters=[Q(deleted=False)],
    )

    result = await loader_kls().load_many([1])
    assert sorted(course.title for course in result[0]) == ["Math", "Science"]


def test_loader_factory_generates_unique_identity():
    m2o_loader = create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_field_name="id",
    )
    m2o_loader_alias = create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school_alias",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_field_name="id",
    )
    o2m_loader = create_one_to_many_loader(
        source_orm_kls=SchoolOrm,
        rel_name="students",
        target_orm_kls=StudentOrm,
        target_dto_kls=StudentDTO,
        target_relation_field_name="school_id",
    )

    names = {
        f"{m2o_loader.__module__}.{m2o_loader.__qualname__}",
        f"{m2o_loader_alias.__module__}.{m2o_loader_alias.__qualname__}",
        f"{o2m_loader.__module__}.{o2m_loader.__qualname__}",
    }

    assert len(names) == 3

    for loader_kls, expected_name in (
        (m2o_loader, "DJ_StudentOrm_school_M2O"),
        (m2o_loader_alias, "DJ_StudentOrm_school_alias_M2O"),
        (o2m_loader, "DJ_SchoolOrm_students_O2M"),
    ):
        assert loader_kls.__module__ == "pydantic_resolve.integration.django.loader"
        assert loader_kls.__name__ == loader_kls.__qualname__ == expected_name


@pytest.mark.asyncio
async def test_many_to_one_loader_ignores_non_orm_dto_fields(seeded_db):
    class SchoolWithExtraDTO(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        id: int
        name: str
        extra: str = "fallback"

    loader_kls = create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolWithExtraDTO,
        target_remote_field_name="id",
    )

    loader = loader_kls()
    result = await loader.load_many([1, 999])

    assert isinstance(result[0], SchoolOrm)
    assert result[0].id == 1
    assert result[0].name == "School-A"
    assert result[1] is None
    assert set(loader._effective_query_fields) == {"id", "name"}
