#!/usr/bin/env bash
# THIS FILE IS PART OF THE CYLC SUITE ENGINE.
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
#-------------------------------------------------------------------------------
# Test validation fails if no graph is defined.
. "$(dirname "$0")/test_header"
#-------------------------------------------------------------------------------
set_test_number 4
#-------------------------------------------------------------------------------
TEST_NAME=${TEST_NAME_BASE}-empty-graph
cat > flow.cylc <<__END__
[scheduling]
    [[graph]]
        R1 = ""
__END__
run_fail "${TEST_NAME}" cylc validate -v flow.cylc
grep_ok "No suite dependency graph defined." "${TEST_NAME}.stderr"
#-------------------------------------------------------------------------------
TEST_NAME=${TEST_NAME_BASE}-no-graph
cat > flow.cylc <<__END__
[scheduling]
    initial cycle point = 2015
    [[graph]]
__END__
run_fail "${TEST_NAME}" cylc validate -v flow.cylc
grep_ok "No suite dependency graph defined." "${TEST_NAME}.stderr"
