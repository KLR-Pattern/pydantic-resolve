from __future__ import annotations

import importlib

import pytest
from aiodataloader import DataLoader

from .conftest import (
    CourseDTO,
    CourseOrm,
    SchoolDTO,
    SchoolOrm,
    StudentDTO,
    StudentOrm,
    StudentProfileDTO,
    StudentProfileOrm,
    student_course,
)


def _loader_module():
    return importlib.import_module("pydantic_resolve.integration.sqlalchemy.loader")


@pytest.mark.asyncio
async def test_create_many_to_one_loader(session_factory, seeded_db):
    loader_mod = _loader_module()

    loader_kls = loader_mod.create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_col_name="id",
        session_factory=session_factory,
    )

    assert issubclass(loader_kls, DataLoader)

    loader = loader_kls()
    result = await loader.load_many([1, 2, 999])

    assert isinstance(result[0], SchoolOrm)
    assert result[0].id == 1
    assert result[0].name == "School-A"
    assert isinstance(result[1], SchoolOrm)
    assert result[1].id == 2
    assert result[1].name == "School-B"
    assert result[2] is None


@pytest.mark.asyncio
async def test_create_one_to_many_loader(session_factory, seeded_db):
    loader_mod = _loader_module()

    loader_kls = loader_mod.create_one_to_many_loader(
        source_orm_kls=SchoolOrm,
        rel_name="students",
        target_orm_kls=StudentOrm,
        target_dto_kls=StudentDTO,
        target_fk_col_name="school_id",
        session_factory=session_factory,
    )

    loader = loader_kls()
    result = await loader.load_many([1, 2, 999])

    assert sorted(s.name for s in result[0]) == ["Alice", "Bob"]
    assert [s.name for s in result[1]] == ["Cathy"]
    assert result[2] == []


@pytest.mark.asyncio
async def test_create_many_to_many_loader(session_factory, seeded_db):
    loader_mod = _loader_module()

    loader_kls = loader_mod.create_many_to_many_loader(
        source_orm_kls=StudentOrm,
        rel_name="courses",
        target_orm_kls=CourseOrm,
        target_dto_kls=CourseDTO,
        secondary_table=student_course,
        secondary_local_col_name="student_id",
        secondary_remote_col_name="course_id",
        target_match_col_name="id",
        session_factory=session_factory,
    )

    loader = loader_kls()
    result = await loader.load_many([1, 2, 3, 999])

    assert sorted(c.title for c in result[0]) == ["Math", "Science"]
    assert [c.title for c in result[1]] == ["Science"]
    assert result[2] == []
    assert result[3] == []


@pytest.mark.asyncio
async def test_many_to_one_loader_applies_filters(
    session_factory,
    session_maker,
    seeded_db,
):
    loader_mod = _loader_module()

    async with session_maker() as session:
        async with session.begin():
            session.add(SchoolOrm(id=99, name="Deleted-School", deleted=True))

    loader_kls = loader_mod.create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_col_name="id",
        session_factory=session_factory,
        filters=[SchoolOrm.deleted.is_(False)],
    )

    result = await loader_kls().load_many([1, 99])

    assert isinstance(result[0], SchoolOrm)
    assert result[0].id == 1
    assert result[0].name == "School-A"
    assert result[1] is None


@pytest.mark.asyncio
async def test_one_to_many_loader_applies_filters(
    session_factory,
    session_maker,
    seeded_db,
):
    loader_mod = _loader_module()

    async with session_maker() as session:
        async with session.begin():
            session.add(StudentOrm(id=99, name="Ghost", school_id=1, deleted=True))

    loader_kls = loader_mod.create_one_to_many_loader(
        source_orm_kls=SchoolOrm,
        rel_name="students",
        target_orm_kls=StudentOrm,
        target_dto_kls=StudentDTO,
        target_fk_col_name="school_id",
        session_factory=session_factory,
        filters=[StudentOrm.deleted.is_(False)],
    )

    result = await loader_kls().load_many([1])
    assert sorted(s.name for s in result[0]) == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_many_to_many_loader_applies_filters(
    session_factory,
    session_maker,
    seeded_db,
):
    loader_mod = _loader_module()

    async with session_maker() as session:
        async with session.begin():
            session.add(CourseOrm(id=99, title="Ghost", deleted=True))
            await session.execute(
                student_course.insert(),
                [{"student_id": 1, "course_id": 99}],
            )

    loader_kls = loader_mod.create_many_to_many_loader(
        source_orm_kls=StudentOrm,
        rel_name="courses",
        target_orm_kls=CourseOrm,
        target_dto_kls=CourseDTO,
        secondary_table=student_course,
        secondary_local_col_name="student_id",
        secondary_remote_col_name="course_id",
        target_match_col_name="id",
        session_factory=session_factory,
        filters=[CourseOrm.deleted.is_(False)],
    )

    result = await loader_kls().load_many([1])
    assert sorted(c.title for c in result[0]) == ["Math", "Science"]


def test_loader_factory_generates_unique_identity():
    loader_mod = _loader_module()

    m2o_loader = loader_mod.create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_col_name="id",
        session_factory=lambda: None,
    )
    m2o_loader_alias = loader_mod.create_many_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="school_alias",
        target_orm_kls=SchoolOrm,
        target_dto_kls=SchoolDTO,
        target_remote_col_name="id",
        session_factory=lambda: None,
    )
    o2m_loader = loader_mod.create_one_to_many_loader(
        source_orm_kls=SchoolOrm,
        rel_name="students",
        target_orm_kls=StudentOrm,
        target_dto_kls=StudentDTO,
        target_fk_col_name="school_id",
        session_factory=lambda: None,
    )

    names = {
        f"{m2o_loader.__module__}.{m2o_loader.__qualname__}",
        f"{m2o_loader_alias.__module__}.{m2o_loader_alias.__qualname__}",
        f"{o2m_loader.__module__}.{o2m_loader.__qualname__}",
    }

    assert len(names) == 3

    for loader_kls, expected_name in (
        (m2o_loader, "SA_StudentOrm_school_M2O"),
        (m2o_loader_alias, "SA_StudentOrm_school_alias_M2O"),
        (o2m_loader, "SA_SchoolOrm_students_O2M"),
    ):
        assert loader_kls.__module__ == "pydantic_resolve.integration.sqlalchemy.loader"
        assert loader_kls.__name__ == loader_kls.__qualname__ == expected_name


@pytest.mark.asyncio
async def test_create_reverse_one_to_one_loader(session_factory, seeded_db):
    loader_mod = _loader_module()

    loader_kls = loader_mod.create_reverse_one_to_one_loader(
        source_orm_kls=StudentOrm,
        rel_name="profile",
        target_orm_kls=StudentProfileOrm,
        target_dto_kls=StudentProfileDTO,
        target_fk_col_name="student_id",
        session_factory=session_factory,
    )

    assert issubclass(loader_kls, DataLoader)

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
