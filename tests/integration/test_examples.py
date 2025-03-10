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
"""Working documentation for the integration test framework.

Here are some examples which cover a range of uses (and also provide some
useful testing in the process 😀.)

"""

import asyncio
import logging
from pathlib import Path
import pytest

from cylc.flow import __version__


async def test_create_flow(flow, run_dir):
    """Use the flow fixture to create workflows on the file system."""
    # Ensure a flow.cylc file gets written out
    reg = flow({
        'scheduler': {
            'allow implicit tasks': True
        },
        'scheduling': {
            'graph': {
                'R1': 'foo'
            }
        }
    })
    workflow_dir = run_dir / reg
    flow_file = workflow_dir / 'flow.cylc'

    assert workflow_dir.exists()
    assert flow_file.exists()


async def test_run(flow, scheduler, run, one_conf):
    """Create a workflow, initialise the scheduler and run it."""
    # Ensure the scheduler can survive for at least one second without crashing
    reg = flow(one_conf)
    schd = scheduler(reg)
    async with run(schd):
        await asyncio.sleep(1)  # this yields control to the main loop


async def test_logging(flow, scheduler, start, one_conf, log_filter):
    """We can capture log records when we run a scheduler."""
    # Ensure that the cylc version is logged on startup.
    reg = flow(one_conf)
    schd = scheduler(reg)
    async with start(schd) as log:
        # this returns a list of log records containing __version__
        assert log_filter(log, contains=__version__)


async def test_scheduler_arguments(flow, scheduler, start, one_conf):
    """We can provide options to the scheduler when we __init__ it.

    These options match their command line equivalents.

    Use the `dest` value specified in the option parser.

    """
    # Ensure the paused_start option is obeyed by the scheduler.
    reg = flow(one_conf)
    schd = scheduler(reg, paused_start=True)
    async with start(schd):
        assert schd.is_paused
    reg = flow(one_conf)
    schd = scheduler(reg, paused_start=False)
    async with start(schd):
        assert not schd.is_paused


async def test_shutdown(flow, scheduler, start, one_conf):
    """Shut down a workflow.

    The scheduler automatically shuts down once you exit the `async with`
    block, however you can manually shut it down within this block if you
    like.

    """
    # Ensure the TCP server shuts down with the scheduler.
    reg = flow(one_conf)
    schd = scheduler(reg)
    async with start(schd):
        pass
    assert schd.server.replier.socket.closed


async def test_install(flow, scheduler, one_conf, run_dir):
    """You don't have to run workflows, it's usually best not to!

    You can take the scheduler through the startup sequence as far as needed
    for your test.

    """
    # Ensure the installation of the job script is completed.
    reg = flow(one_conf)
    schd = scheduler(reg)
    await schd.install()
    assert Path(
        run_dir, schd.workflow, '.service', 'etc', 'job.sh'
    ).exists()


async def test_task_pool(one, start):
    """You don't have to run the scheduler to play with the task pool.

    There are two fixtures to start a scheduler:

    `start`
       Takes a scheduler through the startup sequence.
    `run`
       Takes a scheduler through the startup sequence, then sets the main loop
       going.

    Unless you need the Scheduler main loop running, use `start`.

    This test uses a pre-prepared Scheduler called "one".

    """
    # Ensure that the correct number of tasks get added to the task pool.
    async with start(one):
        # pump the scheduler's heart manually
        one.pool.release_runahead_tasks()
        assert len(one.pool.main_pool) == 1


async def test_exception(one, run, log_filter):
    """Through an exception into the scheduler to see how it will react.

    You have to do this from within the scheduler itself.
    The easy way is to patch the object.

    """
    class MyException(Exception):
        pass

    # replace the main loop with something that raises an exception
    def killer():
        raise MyException('mess')

    one.main_loop = killer

    # make sure that this error causes the flow to shutdown
    with pytest.raises(MyException):
        async with run(one) as log:
            # The `run` fixture's shutdown logic waits for the main loop to run
            pass

    # make sure the exception was logged
    assert len(log_filter(
        log,
        level=logging.CRITICAL,
        contains='mess'
    )) == 1

    # make sure the server socket has closed - a good indication of a
    # successful clean shutdown
    assert one.server.replier.socket.closed


@pytest.fixture(scope='module')
async def myflow(mod_flow, mod_scheduler, mod_one_conf):
    """You can save setup/teardown time by reusing fixtures

    Write a module-scoped fixture and it can be shared by all tests in the
    current module.

    The standard fixtures all have `mod_` alternatives to allow you to do
    this.

    Pytest has been configured to run all tests from the same module in the
    same xdist worker, in other words, module scoped fixtures only get
    created once per module, even when distributing tests.

    Obviously this goes with the usual warnings about not mutating the
    object you are testing in the tests.

    """
    reg = mod_flow(mod_one_conf)
    schd = mod_scheduler(reg)
    return schd


def test_module_scoped_fixture(myflow):
    """Ensure the uuid is set on __init__.

    The myflow fixture will be shared between all test functions within this
    Python module.

    """
    assert myflow.uuid_str


async def test_db_select(one, start, db_select):
    """Demonstrate and test querying the workflow database."""
    # run a workflow
    schd = one
    async with start(schd):
        # Note: can't query database here unfortunately
        pass

    # Now we can query the DB
    # Select all from workflow_params table:
    assert ('UTC_mode', '0') in db_select(schd, False, 'workflow_params')

    # Select name & status columns from task_states table:
    results = db_select(schd, False, 'task_states', 'name', 'status')
    assert results[0] == ('one', 'waiting')

    # Select all columns where name==one & status==waiting from
    # task_states table:
    results = db_select(
        schd, False, 'task_states', name='one', status='waiting')
    assert len(results) == 1
