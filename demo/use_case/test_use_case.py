"""Tests for demo/use_case — Service methods and FastAPI endpoints."""

from __future__ import annotations

import pytest



# ──────────────────────────────────────────────────
# Tests: Service methods (DefineSubset + AutoLoad + post_*)
# ──────────────────────────────────────────────────


class TestUserService:
    @pytest.mark.asyncio
    async def test_list_users(self):
        from demo.use_case.services import UserService

        users = await UserService.list_users()
        assert len(users) == 3
        assert users[0].name == "Alice"
        assert users[1].name == "Bob"
        assert users[2].name == "Charlie"

    @pytest.mark.asyncio
    async def test_get_user(self):
        from demo.use_case.services import UserService

        user = await UserService.get_user(user_id=1)
        assert user is not None
        assert user.id == 1
        assert user.name == "Alice"

    @pytest.mark.asyncio
    async def test_get_user_not_found(self):
        from demo.use_case.services import UserService

        user = await UserService.get_user(user_id=999)
        assert user is None


class TestTaskService:
    @pytest.mark.asyncio
    async def test_list_tasks(self):
        from demo.use_case.services import TaskService

        tasks = await TaskService.list_tasks()
        assert len(tasks) == 5
        assert tasks[0].owner_detail is not None
        assert tasks[0].owner_detail.name == "Alice"

    @pytest.mark.asyncio
    async def test_get_tasks_by_sprint(self):
        from demo.use_case.services import TaskService

        tasks = await TaskService.get_tasks_by_sprint(sprint_id=1)
        assert len(tasks) == 2
        assert tasks[0].title == "Setup project"
        assert tasks[1].title == "Implement auth"

    @pytest.mark.asyncio
    async def test_get_task(self):
        from demo.use_case.services import TaskService

        task = await TaskService.get_task(task_id=1)
        assert task is not None
        assert task.id == 1
        assert task.title == "Setup project"
        assert task.owner_detail is not None
        assert task.owner_detail.name == "Alice"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self):
        from demo.use_case.services import TaskService

        task = await TaskService.get_task(task_id=999)
        assert task is None


class TestSprintService:
    @pytest.mark.asyncio
    async def test_list_sprints(self):
        from demo.use_case.services import SprintService

        sprints = await SprintService.list_sprints()
        assert len(sprints) == 2

    @pytest.mark.asyncio
    async def test_sprint_has_tasks(self):
        from demo.use_case.services import SprintService

        sprints = await SprintService.list_sprints()
        sprint1 = next(s for s in sprints if s.id == 1)
        assert len(sprint1.task_list) == 2
        assert sprint1.task_count == 2

    @pytest.mark.asyncio
    async def test_sprint_contributor_names(self):
        from demo.use_case.services import SprintService

        sprints = await SprintService.list_sprints()
        sprint1 = next(s for s in sprints if s.id == 1)
        # contributor_names is empty since tasks are TaskEntity (no owner loaded)
        assert sprint1.contributor_names == []

    @pytest.mark.asyncio
    async def test_get_sprint(self):
        from demo.use_case.services import SprintService

        sprint = await SprintService.get_sprint(sprint_id=1)
        assert sprint is not None
        assert sprint.id == 1
        assert sprint.name == "Sprint 1"
        assert sprint.task_count == 2

    @pytest.mark.asyncio
    async def test_get_sprint_not_found(self):
        from demo.use_case.services import SprintService

        sprint = await SprintService.get_sprint(sprint_id=999)
        assert sprint is None

