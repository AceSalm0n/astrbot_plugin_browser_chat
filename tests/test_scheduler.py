import asyncio
import unittest
from unittest.mock import patch

from scheduler import (
    CleanupState,
    Job,
    JobPhase,
    JobResult,
    ManagerState,
    SourceRoute,
    TaskManager,
)


def job(number, *, user=None, platform="bot-a", source="group-a", config=None):
    return Job(
        task_id=str(number),
        dedup_key=(platform, "group", source, str(number)),
        user_key=(platform, user or str(number)),
        prompt=f"Question {number}",
        route=SourceRoute(platform, "group", source),
        config=config if config is not None else {},
    )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_natural_cleanup_waits_without_interrupting_it(self):
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def runner(current):
            current.phase = JobPhase.CLEANING
            current.result = JobResult.DELIVERY_UNKNOWN
            cleaning.set()
            await release.wait()
            current.cleanup = CleanupState.DELETED

        manager = await self.manager(runner)
        await manager.submit(job(1))
        await cleaning.wait()
        cancellation = asyncio.create_task(manager.cancel(("bot-a", "1")))
        await asyncio.sleep(0)
        self.assertFalse(cancellation.done())
        self.assertEqual(manager.snapshot().running, 1)
        release.set()
        await cancellation
        outcome = await manager.wait("1")
        self.assertEqual(outcome.cleanup, CleanupState.DELETED)
        self.assertEqual(outcome.result, JobResult.DELIVERY_UNKNOWN)

    async def test_dedup_ttl_and_history_bound_do_not_expire_active_messages(self):
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def runner(current):
            entered.set()
            await gate.wait()

        manager = await self.manager(runner)
        with patch("scheduler.MESSAGE_RETENTION_SECONDS", 0.02):
            original = job(1)
            await manager.submit(original)
            await entered.wait()
            await asyncio.sleep(0.03)
            duplicate = job(2)
            duplicate.dedup_key = original.dedup_key
            self.assertEqual(
                (await manager.submit(duplicate)).reason, "duplicate_message"
            )
            gate.set()
            await manager.wait("1")
            self.assertEqual(
                (await manager.submit(duplicate)).reason, "duplicate_message"
            )
            await asyncio.sleep(0.03)
            self.assertTrue((await manager.submit(duplicate)).accepted)
            await manager.wait("2")
        with patch("scheduler.HISTORY_LIMIT", 2):
            for n in range(3, 6):
                await manager.submit(job(n))
                await manager.wait(str(n))
            with self.assertRaises(KeyError):
                await manager.wait("3")
            self.assertEqual((await manager.wait("5")).result, JobResult.SUCCESS)

    async def test_close_from_runner_rejects_instead_of_self_joining(self):
        async def runner(current):
            with self.assertRaises(RuntimeError):
                await manager.close()

        manager = await self.manager(runner)
        await manager.submit(job(1))
        self.assertEqual((await manager.wait("1")).result, JobResult.SUCCESS)

    async def asyncSetUp(self):
        self.managers = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()

    async def manager(self, runner, **kwargs):
        manager = TaskManager(runner=runner, **kwargs)
        self.managers.append(manager)
        await manager.start()
        return manager

    async def test_atomic_seven_submissions_and_cross_source_user_slot(self):
        gate = asyncio.Event()
        entered = asyncio.Event()
        order = []

        async def runner(current):
            order.append(current.task_id)
            entered.set()
            await gate.wait()

        manager = await self.manager(runner)
        admissions = await asyncio.gather(*(manager.submit(job(n)) for n in range(7)))
        await entered.wait()
        self.assertEqual(
            (manager.snapshot().running, manager.snapshot().queued), (1, 5)
        )
        self.assertEqual([a.accepted for a in admissions], [True] * 6 + [False])
        self.assertEqual([a.queue_position for a in admissions[:6]], list(range(6)))
        self.assertEqual(admissions[6].reason, "queue_full")
        duplicate_user = await manager.submit(job(20, user="0", source="another-group"))
        self.assertEqual(duplicate_user.reason, "user_busy")
        private_job = job(21, user="0", source="0")
        private_job.dedup_key = ("bot-a", "private", "0", "21")
        private_job.route = SourceRoute("bot-a", "private", "0")
        self.assertEqual((await manager.submit(private_job)).reason, "user_busy")
        gate.set()
        results = await asyncio.gather(*(manager.wait(str(n)) for n in range(6)))
        self.assertTrue(all(r.result == JobResult.SUCCESS for r in results))
        self.assertEqual(order, [str(n) for n in range(6)])
        self.assertEqual(manager.snapshot().running, 0)

    async def test_zero_capacity_means_no_waiting_and_immediate_cancel_is_safe(self):
        entered = []

        async def runner(current):
            entered.append(current.task_id)
            await asyncio.Event().wait()

        manager = await self.manager(runner, capacity=0)
        self.assertTrue((await manager.submit(job(1))).accepted)
        self.assertEqual((await manager.submit(job(2))).reason, "queue_full")
        self.assertTrue(await manager.cancel(("bot-a", "1")))
        self.assertEqual((await manager.wait("1")).result, JobResult.CANCELLED)
        self.assertEqual(entered, [])
        self.assertEqual(manager.snapshot().running, 0)
        self.assertTrue((await manager.submit(job(3, user="1"))).accepted)

    async def test_queue_timeout_releases_user_while_runner_is_still_blocked(self):
        async def runner(current):
            await asyncio.Event().wait()

        manager = await self.manager(runner, queue_timeout_s=0.03)
        await manager.submit(job(1))
        await manager.submit(job(2))
        expired = await asyncio.wait_for(manager.wait("2"), 1)
        self.assertEqual(expired.result, JobResult.TIMED_OUT)
        self.assertEqual(expired.reason, "queue_timeout")
        self.assertEqual(expired.cleanup, CleanupState.NOT_CREATED)
        self.assertEqual(
            (manager.snapshot().running, manager.snapshot().queued), (1, 0)
        )
        self.assertTrue((await manager.submit(job(3, user="2"))).accepted)

    async def test_message_dedup_survives_finish_but_is_scoped_by_platform(self):
        async def runner(current):
            return

        manager = await self.manager(runner)
        first = job(1)
        await manager.submit(first)
        duplicate = job(2, user="other")
        duplicate.dedup_key = first.dedup_key
        self.assertEqual((await manager.submit(duplicate)).reason, "duplicate_message")
        await manager.wait("1")
        self.assertEqual((await manager.submit(duplicate)).reason, "duplicate_message")
        self.assertTrue((await manager.submit(job(2, platform="bot-b"))).accepted)
        self.assertTrue((await manager.submit(job(3, user="1"))).accepted)

    async def test_runner_error_and_timeout_release_slots(self):
        async def runner(current):
            if current.task_id == "1":
                raise RuntimeError("private error payload must not be retained")
            if current.task_id == "2":
                raise asyncio.TimeoutError()

        manager = await self.manager(runner)
        for n, expected in [
            (1, JobResult.FAILED),
            (2, JobResult.TIMED_OUT),
            (3, JobResult.SUCCESS),
        ]:
            await manager.submit(job(n, user="same"))
            outcome = await manager.wait(str(n))
            self.assertEqual(outcome.result, expected)
            self.assertEqual(outcome.phase, JobPhase.DONE)
            self.assertNotIn("private error payload", repr(outcome))
            self.assertEqual(manager.snapshot().running, 0)

    async def test_cancel_holds_user_and_execution_slot_until_cleanup_finishes(self):
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        order = []

        async def runner(current):
            order.append(current.task_id)
            if current.task_id == "1":
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    current.phase = JobPhase.CLEANING
                    cleaning.set()
                    await finish_cleanup.wait()
                    current.cleanup = CleanupState.DELETED

        manager = await self.manager(runner)
        await manager.submit(job(1))
        await entered.wait()
        await manager.submit(job(2))
        cancellation = asyncio.create_task(manager.cancel(("bot-a", "1")))
        await cleaning.wait()
        self.assertEqual(
            (manager.snapshot().running, manager.snapshot().queued), (1, 1)
        )
        self.assertEqual((await manager.submit(job(3, user="1"))).reason, "user_busy")
        self.assertEqual(order, ["1"])
        finish_cleanup.set()
        self.assertTrue(await cancellation)
        outcome = await manager.wait("1")
        self.assertEqual(outcome.result, JobResult.CANCELLED)
        self.assertEqual(outcome.cleanup, CleanupState.DELETED)
        await manager.wait("2")
        self.assertEqual(order, ["1", "2"])

    async def test_queued_cancel_is_terminal_and_does_not_reorder_other_jobs(self):
        gate = asyncio.Event()
        order = []

        async def runner(current):
            order.append(current.task_id)
            await gate.wait()

        manager = await self.manager(runner)
        for n in range(4):
            await manager.submit(job(n))
        self.assertTrue(await manager.cancel(("bot-a", "1")))
        self.assertFalse(await manager.cancel(("bot-a", "1")))
        self.assertEqual((await manager.wait("1")).result, JobResult.CANCELLED)
        gate.set()
        await asyncio.gather(*(manager.wait(str(n)) for n in [0, 2, 3]))
        self.assertEqual(order, ["0", "2", "3"])

    async def test_pause_inside_runner_cancels_peers_without_self_deadlock(self):
        pause_now = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        peer_entered = asyncio.Event()
        peer_cancelled = asyncio.Event()

        async def runner(current):
            if current.task_id == "1":
                await pause_now.wait()
                await manager.pause("login")
                cleaning.set()
                await release_cleanup.wait()
            else:
                try:
                    peer_entered.set()
                    await asyncio.Event().wait()
                finally:
                    peer_cancelled.set()

        manager = await self.manager(runner, concurrency=2)
        for n in [1, 2, 3]:
            await manager.submit(job(n))
        await peer_entered.wait()
        pause_now.set()
        await asyncio.wait_for(cleaning.wait(), 1)
        await asyncio.wait_for(peer_cancelled.wait(), 1)
        self.assertEqual(manager.snapshot().state, ManagerState.PAUSED)
        self.assertEqual(manager.snapshot().pause_reason, "login")
        self.assertEqual((await manager.wait("3")).reason, "paused")
        self.assertEqual((await manager.submit(job(4))).reason, "paused")
        with self.assertRaises(RuntimeError):
            await manager.resume()
        release_cleanup.set()
        await asyncio.gather(manager.wait("1"), manager.wait("2"))
        await manager.resume()
        self.assertTrue((await manager.submit(job(4))).accepted)

    async def test_config_and_route_snapshot_detaches_caller_changes(self):
        gate = asyncio.Event()
        received = []

        async def runner(current):
            await gate.wait()
            received.append(current)

        manager = await self.manager(runner)
        config = {
            "keep_conversations": False,
            "allowed_users": ["1"],
            "nested": {"value": 7},
        }
        submitted = job(1, config=config)
        await manager.submit(submitted)
        config["keep_conversations"] = True
        config["allowed_users"].append("2")
        config["nested"]["value"] = 9
        submitted.prompt = "mutated"
        submitted.route = SourceRoute("bot-b", "private", "different")
        gate.set()
        await manager.wait("1")
        copied = received[0]
        self.assertFalse(copied.config["keep_conversations"])
        self.assertEqual(copied.config["allowed_users"], ("1",))
        self.assertEqual(copied.config["nested"]["value"], 7)
        with self.assertRaises(TypeError):
            copied.config["nested"]["value"] = 4
        self.assertEqual(copied.prompt, "Question 1")
        self.assertEqual(copied.route.target_id, "group-a")

    async def test_cancelled_waiter_does_not_cancel_job_or_its_completion(self):
        gate = asyncio.Event()

        async def runner(current):
            await gate.wait()

        manager = await self.manager(runner)
        await manager.submit(job(1))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(manager.wait("1"), 0.02)
        gate.set()
        self.assertEqual((await manager.wait("1")).result, JobResult.SUCCESS)

    async def test_close_is_shared_and_does_not_interrupt_cleanup_twice(self):
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def runner(current):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish_cleanup.wait()

        manager = await self.manager(runner)
        await manager.submit(job(1))
        await entered.wait()
        await manager.submit(job(2))
        first_close = asyncio.create_task(manager.close())
        await cleaning.wait()
        second_close = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        self.assertFalse(second_close.done())
        self.assertEqual((await manager.submit(job(3))).reason, "closed")
        self.assertEqual((await manager.wait("2")).result, JobResult.CANCELLED)
        first_close.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_close
        finish_cleanup.set()
        await second_close
        self.assertEqual(manager.snapshot().state, ManagerState.CLOSED)
        self.assertEqual(manager.snapshot().running, 0)
        await manager.close()
        await manager.start()
        self.assertEqual((await manager.submit(job(3))).reason, "closed")

    async def test_lifecycle_and_constructor_validation(self):
        async def runner(current):
            return

        for kwargs in [
            {"concurrency": True},
            {"concurrency": 0},
            {"capacity": -1},
            {"capacity": False},
            {"queue_timeout_s": float("nan")},
            {"queue_timeout_s": float("inf")},
            {"queue_timeout_s": 0},
            {"queue_timeout_s": True},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TaskManager(runner=runner, **kwargs)
        manager = TaskManager(runner=runner)
        self.managers.append(manager)
        self.assertEqual((await manager.submit(job(1))).reason, "not_started")
        await manager.start()
        await manager.start()
        self.assertTrue((await manager.submit(job(1))).accepted)
        await manager.wait("1")
        self.assertEqual((await manager.submit(job(1))).reason, "duplicate_message")


if __name__ == "__main__":
    unittest.main()
