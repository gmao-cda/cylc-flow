# THIS FILE IS PART OF THE CYLC WORKFLOW ENGINE.
# Copyright (C) NIWA & British Crown (Met Office) & Contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import logging
from pathlib import Path
import pytest
from typing import Any, Callable

from cylc.flow.exceptions import CylcError
from cylc.flow.parsec.exceptions import ParsecError
from cylc.flow.pathutil import get_cylc_run_dir, get_workflow_run_dir
from cylc.flow.scheduler import Scheduler, SchedulerStop
from cylc.flow.task_state import (
    TASK_STATUS_WAITING,
    TASK_STATUS_SUBMIT_FAILED,
    TASK_STATUS_SUBMITTED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_FAILED
)

from cylc.flow.workflow_status import AutoRestartMode

from .utils.flow_tools import _make_flow


Fixture = Any


TRACEBACK_MSG = "Traceback (most recent call last):"


async def test_is_paused_after_stop(
        one_conf: Fixture, flow: Fixture, scheduler: Fixture, run: Fixture,
        db_select: Fixture):
    """Test the paused status is unset on normal shutdown."""
    reg: str = flow(one_conf)
    schd: 'Scheduler' = scheduler(reg, paused_start=True)
    # Run
    async with run(schd):
        assert not schd.is_restart
        assert schd.is_paused
    # Stopped
    assert ('is_paused', '1') not in db_select(schd, False, 'workflow_params')
    # Restart
    schd = scheduler(reg, paused_start=None)
    async with run(schd):
        assert schd.is_restart
        assert not schd.is_paused


async def test_is_paused_after_crash(
        one_conf: Fixture, flow: Fixture, scheduler: Fixture, run: Fixture,
        db_select: Fixture):
    """Test the paused status is not unset for an interrupted workflow."""
    reg: str = flow(one_conf)
    schd: 'Scheduler' = scheduler(reg, paused_start=True)

    def ctrl_c():
        raise asyncio.CancelledError("Mock keyboard interrupt")
    # Patch this part of the main loop
    _schd_workflow_shutdown = schd.workflow_shutdown
    setattr(schd, 'workflow_shutdown', ctrl_c)

    # Run
    with pytest.raises(asyncio.CancelledError):
        async with run(schd):
            assert not schd.is_restart
            assert schd.is_paused
    # Stopped
    assert ('is_paused', '1') in db_select(schd, False, 'workflow_params')
    # Reset patched method
    setattr(schd, 'workflow_shutdown', _schd_workflow_shutdown)
    # Restart
    schd = scheduler(reg, paused_start=None)
    async with run(schd):
        assert schd.is_restart
        assert schd.is_paused


async def test_shutdown_CylcError_log(one: Scheduler, run: Callable):
    """Test that if a CylcError occurs during shutdown, it is
    logged in one line."""
    schd = one

    # patch the shutdown to raise an error
    async def mock_shutdown(*a, **k):
        raise CylcError("Error on shutdown")
    setattr(schd, '_shutdown', mock_shutdown)

    # run the workflow
    log: pytest.LogCaptureFixture
    with pytest.raises(CylcError) as exc:
        async with run(schd) as log:
            pass

    # check the log records after attempted shutdown
    assert str(exc.value) == "Error on shutdown"
    last_record = log.records[-1]
    assert last_record.message == "CylcError: Error on shutdown"
    assert last_record.levelno == logging.ERROR

    # shut down the scheduler properly
    await Scheduler._shutdown(schd, SchedulerStop('because I said so'))


async def test_shutdown_general_exception_log(one: Scheduler, run: Callable):
    """Test that if a non-CylcError occurs during shutdown, it is
    logged with traceback (but not excessive)."""
    schd = one

    # patch the shutdown to raise an error
    async def mock_shutdown(*a, **k):
        raise ValueError("Error on shutdown")
    setattr(schd, '_shutdown', mock_shutdown)

    # run the workflow
    log: pytest.LogCaptureFixture
    with pytest.raises(ValueError) as exc:
        async with run(schd) as log:
            pass

    # check the log records after attempted shutdown
    assert str(exc.value) == "Error on shutdown"
    last_record = log.records[-1]
    assert last_record.message == "Error on shutdown"
    assert last_record.levelno == logging.ERROR
    assert last_record.exc_text is not None
    assert last_record.exc_text.startswith(TRACEBACK_MSG)
    assert ("During handling of the above exception, "
            "another exception occurred") not in last_record.exc_text

    # shut down the scheduler properly
    await Scheduler._shutdown(schd, SchedulerStop('because I said so'))


async def test_holding_tasks_whilst_scheduler_paused(
    capture_submission,
    flow,
    one_conf,
    start,
    scheduler,
):
    """It should hold tasks irrespective of workflow state.

    See https://github.com/cylc/cylc-flow/issues/4278
    """
    reg = flow(one_conf)
    one = scheduler(reg, paused_start=True)

    # run the workflow
    async with start(one):
        # capture any job submissions
        submitted_tasks = capture_submission(one)
        assert submitted_tasks == set()

        # release runahead/queued tasks
        # (nothing should happen because the scheduler is paused)
        one.pool.release_runahead_tasks()
        one.release_queued_tasks()
        assert submitted_tasks == set()

        # hold all tasks & resume the workflow
        one.command_hold(['*/*'])
        one.resume_workflow()

        # release queued tasks
        # (there should be no change because the task is still held)
        one.release_queued_tasks()
        assert submitted_tasks == set()

        # release all tasks
        one.command_release(['*/*'])

        # release queued tasks
        # (the task should be submitted)
        one.release_queued_tasks()
        assert len(submitted_tasks) == 1


async def test_no_poll_waiting_tasks(
    capture_polling,
    flow,
    one_conf,
    start,
    scheduler,
):
    """Waiting tasks shouldn't be polled.

    If a waiting task previously it will have the submit number of its previous
    job, and polling would erroneously return the state of that job.

    See https://github.com/cylc/cylc-flow/issues/4658
    """
    reg: str = flow(one_conf)
    one: Scheduler = scheduler(reg, paused_start=True)

    log: pytest.LogCaptureFixture
    async with start(one) as log:

        # Test assumes start up with a waiting task.
        task = (one.pool.get_all_tasks())[0]
        assert task.state.status == TASK_STATUS_WAITING

        polled_tasks = capture_polling(one)

        # Waiting tasks should not be polled.
        one.command_poll_tasks(['*/*'])
        assert polled_tasks == set()

        # Even if they have a submit number.
        task.submit_num = 1
        one.command_poll_tasks(['*/*'])
        assert len(polled_tasks) == 0

        # But these states should be:
        for state in [
            TASK_STATUS_SUBMIT_FAILED,
            TASK_STATUS_FAILED,
            TASK_STATUS_SUBMITTED,
            TASK_STATUS_RUNNING
        ]:
            task.state.status = state
            one.command_poll_tasks(['*/*'])
            assert len(polled_tasks) == 1
            polled_tasks.clear()

        # Shut down with a running task.
        task.state.status = TASK_STATUS_RUNNING

    # For good measure, check the faked running task is reported at shutdown.
    assert "Orphaned tasks:\n* 1/one (running)" in log.messages


@pytest.mark.parametrize('reload', [False, True])
@pytest.mark.parametrize(
    'test_conf, expected_msg',
    [
        pytest.param(
            {'Alan Wake': "It's not a lake, it's an ocean"},
            "IllegalItemError: Alan Wake",
            id="illegal item"
        ),
        pytest.param(
            {
                'scheduling': {
                    'initial cycle point': "2k22",
                    'graph': {'R1': "a => b"}
                }
            },
            ("IllegalValueError: (type=cycle point) "
             "[scheduling]initial cycle point = 2k22 - (Invalid cycle point)"),
            id="illegal cycle point"
        )
    ]
)
async def test_illegal_config_load(
    test_conf: dict,
    expected_msg: str,
    reload: bool,
    flow: Callable,
    one_conf: dict,
    start: Callable,
    run: Callable,
    scheduler: Callable,
    log_filter: Callable
):
    """Test that ParsecErrors (illegal config) - that occur during config load
    when running a workflow - are displayed without traceback.

    Params:
        test_conf: Dict to update one_conf with.
        expected_msg: Expected log message at error level.
        reload: If False, test a workflow start with invalid config.
            If True, test a workflow start with valid config followed by
            reload with invalid config.
    """
    if not reload:
        one_conf.update(test_conf)
    reg: str = flow(one_conf)
    schd: Scheduler = scheduler(reg)
    log: pytest.LogCaptureFixture

    if reload:
        one_conf.update(test_conf)
        run_dir = Path(get_workflow_run_dir(reg))
        async with run(schd) as log:
            # Shouldn't be any errors at this stage:
            assert not log_filter(log, level=logging.ERROR)
            # Modify flow.cylc:
            _make_flow(get_cylc_run_dir(), run_dir, one_conf, '')
            schd.queue_command('reload_workflow', {})
        assert log_filter(
            log, level=logging.ERROR,
            exact_match=f"Command failed: reload_workflow()\n{expected_msg}"
        )
    else:
        with pytest.raises(ParsecError):
            async with start(schd) as log:
                pass
        assert log_filter(
            log,
            level=logging.ERROR,
            exact_match=f"Workflow shutting down - {expected_msg}"
        )

    assert TRACEBACK_MSG not in log.text


async def test_unexpected_ParsecError(
    one: Scheduler,
    start: Callable,
    log_filter: Callable,
    monkeypatch: pytest.MonkeyPatch
):
    """Test that ParsecErrors - that occur at any time other than config load
    when running a workflow - are displayed with traceback, because they are
    not expected."""
    log: pytest.LogCaptureFixture

    def raise_ParsecError(*a, **k):
        raise ParsecError("Mock error")

    monkeypatch.setattr(one, '_configure_contact', raise_ParsecError)

    with pytest.raises(ParsecError):
        async with start(one) as log:
            pass

    assert log_filter(
        log, level=logging.CRITICAL,
        exact_match="Workflow shutting down - Mock error"
    )
    assert TRACEBACK_MSG in log.text


async def test_error_during_auto_restart(
    one: Scheduler,
    run: Callable,
    log_filter: Callable,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that an error during auto-restart does not get swallowed"""
    log: pytest.LogCaptureFixture
    err_msg = "Mock error: sugar in water"

    def mock_auto_restart(*a, **k):
        raise RuntimeError(err_msg)

    monkeypatch.setattr(one, 'workflow_auto_restart', mock_auto_restart)
    monkeypatch.setattr(
        one, 'auto_restart_mode', AutoRestartMode.RESTART_NORMAL
    )

    with pytest.raises(RuntimeError, match=err_msg):
        async with run(one) as log:
            pass

    assert log_filter(log, level=logging.ERROR, contains=err_msg)
    assert TRACEBACK_MSG in log.text
